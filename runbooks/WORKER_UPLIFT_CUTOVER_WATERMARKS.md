# Worker-uplift cutover watermarks

This runbook owns the metadata-only preparation tracked by
`ramideltoro/nutsnews-worker#174`. It does not authorize #166 GO, #127
execution, production writes, an ingestion-owner change, legacy-worker
changes, DNS/failover changes, or any article/domain mutation.

## Standing authorization

The owner comment at
`https://github.com/ramideltoro/nutsnews-worker/issues/174#issuecomment-5150823795`
authorizes current and future releases without another per-release,
first-run, or routine environment-wait response only while
`docs/worker-uplift-cutover-watermark-authorization.json` validates exactly.
The immutable comment body SHA-256 is
`6b5b50b4a62a08582616195d419d9660bae11c8d24bbcf0441fb61a99e5b093b`;
the authorized scope SHA-256 is
`63715c0f2953f267cac41beb8c5f90627aee48f31a4f804304762dbee07f452b`.

An agent may approve the exact `production-backend` environment wait through
the GitHub API when the fixed workflow, target, role, typed confirmation, and
validator remain exact. Do not disable or weaken the environment protection.
Any target, SQL, role-grant, workflow, or invariant change fails closed and
requires a separately reviewed policy change.

## Exact scope

The only runtime database mutation is one row named
`cutover-boundary-v1` in each of these existing tables:

- `worker_uplift_scheduler.reconciliation_watermarks`
- `worker_uplift_fetcher.reconciliation_watermarks`
- `worker_uplift_canonicalizer.reconciliation_watermarks`
- `worker_uplift_enrichment.reconciliation_watermarks`
- `worker_uplift_approval.reconciliation_watermarks`
- `worker_uplift_translation.reconciliation_watermarks`
- `worker_uplift_persistence.reconciliation_watermarks`
- `worker_uplift_publication.reconciliation_watermarks`

The fixed operation is an idempotent `INSERT ... ON CONFLICT` upsert of exactly
eight rows. It cannot accept SQL input. A newer `capturedAtUtc` cannot be
overwritten by older evidence. The transaction deliberately aborts unless one
row is returned for every declared stage.

The dedicated `nutsnews_worker_uplift_watermark` role uses the distinct
protected `NUTSNEWS_WORKER_UPLIFT_WATERMARK_PASSWORD` credential. It can read each stage's
`inbox`, `outbox`, and `reconciliation_watermarks` tables and can insert/update
only the eight watermark tables. It cannot delete, truncate, create schemas or
roles, inherit another role, bypass row-level security, mutate another table,
or reach article/domain data. It is never injected into a worker service.

## Read-only checks

Use these checks before any dry run:

1. Confirm the source commit is current `main` and Backend Checks passed.
2. Run `Backend Worker Runtime Operations` with `action=status` and inspect the
   artifact. All eight services must be healthy, seven main queues must have
   exactly one consumer, mode must be `shadow`, and
   `production_writes_enabled` must be `false`.
3. Run current service reconciliation in dry-run mode. Approval and translation
   must select their retained confirmed-outbox candidates with
   `failed_closed=0`; no service may replay or write.
4. Inspect main, retry, and DLQ queue artifacts. Main/retry queues must be
   drained. A retained RabbitMQ DLQ depth is acceptable only when the bounded
   stability sample proves zero growth and the existing DLQ runbook owns it.

Read-only checks may report aggregate terminal `failed`/`parked` inbox counts.
Those rows are not active lag and are never replayed automatically. Artifacts
record only count and distinct reason-bucket count, not reason values or
payload references. Nonzero aggregates are retained for operator review under
the service reconciliation and DLQ runbooks.

## Value-free dry run

Dispatch `Backend Worker-Uplift Cutover Watermarks` with:

- `mode=dry-run`
- blank `confirmation`

The protected job collects two bounded runtime/queue snapshots, validates a
5–60 second stable interval, opens a loopback PostgreSQL tunnel, and uses the
dedicated role for aggregate evidence only. It fails closed unless:

- legacy remains the active ingestion owner;
- uplift remains shadow-only with writes disabled;
- every main/retry queue is drained and every required main consumer equals 1;
- DLQ counts do not grow across the sample;
- active inbox, unconfirmed outbox, retrying outbox, and dead-lettered outbox
  counts are zero for every stage;
- no unexpected watermark row exists;
- the role privilege proof is exact; and
- all input evidence is no older than 15 minutes.

Inspect `backend-worker-uplift-cutover-watermarks-dry-run` and validate
`watermark-candidate.sha256`. The candidate contains the exact eight rows,
source digests, aggregate retained-failure classification, immutable workflow
identity, and safety state. It contains no payloads, connection strings,
endpoints, credentials, or article/domain values.

## Protected apply

Dispatch the same workflow with:

- `mode=apply`
- `confirmation=refresh-worker-uplift-cutover-watermarks`

The workflow creates the exact value-free dry-run artifact before apply and
binds apply to its SHA-256. Approve only that exact protected wait under the
standing authorization. The apply runs with a 30-second statement timeout and
fails closed on stale evidence, target drift, privilege drift, a partial row
set, or any schema fingerprint change.

After apply, the workflow recollects runtime and queue evidence and proves:

- exactly the eight declared watermark rows exist and match the candidate;
- every `lag_count` is zero;
- no other watermark row or target schema changed;
- images, manifest, compose, consumers, queue counts, shadow mode, write policy,
  and legacy ownership are unchanged; and
- the workflow has no RabbitMQ mutation, DNS/failover, infrastructure, or
  article/domain mutation capability.

Inspect the candidate, apply report, post-apply proof, and
`watermark-evidence.sha256`; a successful workflow conclusion alone is not
evidence.

## Recovery

An apply failure is safe because the fixed transaction rolls back if all eight
rows are not returned. Do not run manual SQL. Correct the failed precondition,
rerun the value-free dry run, and apply a fresh exact candidate.

If a newer watermark blocks an older candidate, discard the stale artifact and
capture current evidence. If role or schema fingerprints drift, stop and use a
new reviewed PR plus the protected baseline workflow; the standing
authorization no longer applies. A watermark rollback is normally unnecessary:
newer evidence is authoritative, while the eight rows are metadata and do not
enable writes or change ingestion ownership.

## Final readiness relationship

#174 evidence is one input to the non-mutating #166 decision. #166 must still
freeze the exact production candidate, complete watermark digest, rollback
deadline, 48-hour observation window, thresholds, owners, and named GO. Even a
passing #174 apply does not authorize cutover. Only a later exact #166 GO may
authorize beginning the separately protected #127 execution.
