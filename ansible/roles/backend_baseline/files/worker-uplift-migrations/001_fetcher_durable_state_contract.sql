-- Backend-owned additive migration for the feed-fetcher durable state adapter.
--
-- This migration is intentionally safe to re-run. The protected Ansible apply
-- executes it as the local postgres OS user after the base worker-uplift model.
-- Existing rows retain their current state; new ownership fields are nullable
-- so an older fetcher image can be rolled back without reversing this schema.
-- Backup, restore, and pre-apply verification use the existing backend
-- PostgreSQL runbooks and protected pipeline gates.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(
  hashtextextended('worker_uplift_fetcher:state-contract:v1', 0)
);

ALTER TABLE worker_uplift_fetcher.inbox
  ADD COLUMN IF NOT EXISTS claim_owner_message_id text,
  ADD COLUMN IF NOT EXISTS claim_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE worker_uplift_fetcher.outbox
  ADD COLUMN IF NOT EXISTS claim_owner_key text,
  ADD COLUMN IF NOT EXISTS claim_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS publication_command jsonb,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

DO $fetcher_outbox_publication_command_constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'worker_uplift_fetcher_outbox_publication_command_object'
      AND conrelid = 'worker_uplift_fetcher.outbox'::regclass
  ) THEN
    ALTER TABLE worker_uplift_fetcher.outbox
      ADD CONSTRAINT worker_uplift_fetcher_outbox_publication_command_object
      CHECK (
        publication_command IS NULL
        OR jsonb_typeof(publication_command) = 'object'
      );
  END IF;
END
$fetcher_outbox_publication_command_constraint$;

ALTER TABLE worker_uplift_fetcher.fetch_versions
  ADD COLUMN IF NOT EXISTS feed_id text,
  ADD COLUMN IF NOT EXISTS content_fingerprint text;

-- This ledger contains only bounded result metadata and no fetched payload or
-- raw content. It still carries the standard worker-uplift retention marker so
-- backend cleanup can bound history growth.
CREATE TABLE IF NOT EXISTS worker_uplift_fetcher.fetch_outcomes (
  id bigserial PRIMARY KEY,
  feed_id text NOT NULL,
  feed_url text NOT NULL,
  fetch_status text NOT NULL CHECK (
    fetch_status IN (
      'success',
      'unchanged',
      'transient_failure',
      'permanent_failure'
    )
  ),
  fetched_at timestamptz NOT NULL,
  http_status integer CHECK (
    http_status IS NULL OR http_status BETWEEN 100 AND 599
  ),
  body_bytes integer NOT NULL CHECK (body_bytes >= 0),
  item_count integer NOT NULL CHECK (item_count >= 0),
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),
  failure_class text,
  failure_code text,
  retryable boolean,
  diagnostic_metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
    jsonb_typeof(diagnostic_metadata) = 'object'
  ),
  created_at timestamptz NOT NULL DEFAULT now(),
  redact_after timestamptz NOT NULL DEFAULT (now() + interval '30 days')
);

ALTER TABLE worker_uplift_fetcher.fetch_outcomes
  ADD COLUMN IF NOT EXISTS redact_after timestamptz NOT NULL
    DEFAULT (now() + interval '30 days');

CREATE TABLE IF NOT EXISTS worker_uplift_fetcher.state_contract (
  component text PRIMARY KEY,
  contract_version integer NOT NULL CHECK (contract_version > 0),
  migrated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS worker_uplift_fetcher_outbox_candidate_idx
  ON worker_uplift_fetcher.outbox (entity_id, created_at DESC)
  WHERE entity_kind = 'candidate';

CREATE INDEX IF NOT EXISTS worker_uplift_fetcher_fetch_state_idx
  ON worker_uplift_fetcher.fetch_versions (feed_id, fetched_at DESC, id DESC)
  WHERE feed_id IS NOT NULL
    AND status IN ('fetched', 'not_modified');

CREATE INDEX IF NOT EXISTS worker_uplift_fetcher_fetch_outcomes_feed_idx
  ON worker_uplift_fetcher.fetch_outcomes (feed_id, fetched_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS worker_uplift_fetcher_fetch_outcomes_redact_idx
  ON worker_uplift_fetcher.fetch_outcomes (redact_after, id);

CREATE INDEX IF NOT EXISTS worker_uplift_fetcher_feed_health_latest_idx
  ON worker_uplift_fetcher.feed_health_projections (
    feed_url,
    projection_version DESC
  );

-- Publish readiness only after every contract object in this transaction has
-- been created successfully. A failed statement rolls back the complete v1
-- migration and cannot leave a healthy-looking contract marker behind.
INSERT INTO worker_uplift_fetcher.state_contract (
  component,
  contract_version
)
VALUES ('fetcher_state_store', 1)
ON CONFLICT (component) DO UPDATE
SET contract_version = EXCLUDED.contract_version,
    migrated_at = now()
WHERE worker_uplift_fetcher.state_contract.contract_version
      < EXCLUDED.contract_version;

COMMIT;
