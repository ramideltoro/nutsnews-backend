# Supabase Standby Split-Brain Fence Gate

Issue #527 turns provider lease/fence evidence into an independent,
machine-readable gate for the existing production Supabase standby.

## Boundary

The gate:

- consumes a successful #526 writer-pause gate artifact for the same
  failover attempt;
- requires an explicit fence epoch;
- requires backend PostgreSQL write eligibility to be revoked or guarded before
  Supabase write eligibility is accepted;
- requires stale backend writer processes and provider/epoch mismatches to
  reject writes;
- requires the transition to be idempotent and safe to retry;
- requires exactly one provider to be write-eligible;
- emits `PASS` only when the target write-eligible provider is the existing
  production Supabase standby and backend PostgreSQL is fenced;
- writes safe metadata only.

The gate does not create a Supabase project, create a `nutsnews-standby`
database, expose Supabase service-role credentials to app or worker traffic
before approved failover, run SQL from GitHub Actions, switch providers, or
replace the combined go/no-go decision.

backend PostgreSQL remains the normal primary until approved failover. This
gate supplies the split-brain fence proof that a later #528 decision and #502
provider switch must consume.

## Result

The gate artifact is `backend-supabase-standby-split-brain-fence-gate.json`
and includes:

- `failover_attempt_id`;
- `fence_epoch`;
- `repository_revision`;
- `source_fingerprint`;
- `target_fingerprint`;
- writer-pause and fence-evidence safe fingerprints;
- writer-pause and fence-evidence measurement times and ages;
- provider write-eligibility status for backend PostgreSQL and existing
  production Supabase;
- required fence control statuses;
- `write_eligible_provider_count`;
- `eligible_provider`;
- overall `status` as `PASS` or `FAIL`;
- `safe_metadata_only=true`.

The artifact must not contain database URLs, passwords, SQL, row values,
backend hostnames, Supabase project refs, raw PostgreSQL errors, or row data.

## Manual Run

Run this only after a fresh #526 writer-pause gate has passed for the same
attempt. Use the writer-pause workflow run id as `writer_pause_gate_run_id`.

```bash
attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ)
fence_epoch=epoch-$(date -u +%Y%m%dT%H%M%SZ)

gh workflow run backend-supabase-standby-split-brain-fence-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id="$attempt_id" \
  -f fence_epoch="$fence_epoch" \
  -f writer_pause_gate_run_id=<writer-pause-run-id> \
  -f confirmation=evaluate-split-brain-fence-gate \
  -f backend_fence_confirmation=backend-postgres-writes-fenced \
  -f stale_writer_confirmation=stale-backend-writers-rejected \
  -f supabase_eligibility_confirmation=supabase-write-eligibility-after-backend-fence \
  -f enforce=false
```

Promotion consumers should use `enforce=true` or call the evaluator with
`--enforce`, where any `FAIL` result returns non-zero.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_split_brain_fence_gate.py
python3 -m unittest tests.test_backend_supabase_standby_split_brain_fence_gate
```

Required fixture coverage:

- complete backend fence plus single target write eligibility passes;
- enabling Supabase writes before backend PostgreSQL write revocation fails;
- simultaneous-write, stale-process, stale-epoch, partial-failure, retry, and
  verification-unavailable cases fail safely;
- a stale backend writer cannot resume backend PostgreSQL writes after the
  epoch changes;
- stale or missing writer-pause evidence fails;
- target mismatch and malformed evidence fail closed;
- artifact output remains safe metadata only.

## Acceptance Evidence

For #527, record:

- merged backend PR and main checks;
- a main-branch `Backend Supabase Standby Split-Brain Fence Gate` run URL;
- the writer-pause gate run id consumed by the split-brain gate;
- artifact status, fence epoch, eligible provider, write-eligible provider
  count, and blockers;
- confirmation that the target is existing production Supabase;
- confirmation that no new Supabase project and no `nutsnews-standby` database
  were created;
- a leak scan showing no database URLs, private keys, Supabase tokens,
  password fields, backend host metadata, Supabase host/project metadata, raw
  SQL, or raw row data.

If the live result is `FAIL`, failover remains blocked until a later run
produces passing split-brain fence evidence.
