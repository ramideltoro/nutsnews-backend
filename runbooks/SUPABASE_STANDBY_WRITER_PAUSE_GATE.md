# Supabase Standby Writer Pause Gate

Issue #526 turns the production writer inventory into a protected, machine-
readable gate for pausing backend PostgreSQL writers and proving a quiet window
before any later combined standby decision.

## Boundary

The gate:

- uses the versioned writer inventory in
  `docs/backend-supabase-writer-pause-gate.json`;
- runs only from `main` behind the `production-backend` environment;
- requires the `pause-and-prove` action confirmation
  `pause-backend-writers-for-supabase-standby`;
- requires the operator freeze confirmation
  `no-manual-backend-postgres-writes`;
- verifies no protected writer workflows are queued or running for the same
  maintenance window;
- runs the fixed host-side `/usr/local/sbin/nutsnews-writer-pause pause`
  command to force backend Worker DB API write flags off and scale worker
  runtime services to zero;
- captures a backend PostgreSQL write-position fingerprint after the pause,
  waits the configured quiet window, and captures a second fingerprint;
- emits `PASS` only when all writer classes are paused, the quiet window is
  long enough, and the write-position fingerprint is unchanged;
- writes safe metadata only.

The gate does not create a Supabase project, create a `nutsnews-standby`
database, write to Supabase from app or worker code, switch providers, approve
failover, or resume writers after a successful provider switch.

Safety statement: backend PostgreSQL remains the primary read/write database;
the target is the existing production Supabase project; there is no new Supabase project and no `nutsnews-standby` database.

This gate does not validate lag, row parity, schema compatibility, sequence
safety, or split-brain fencing. Those are separate #521 gates.

## Writer Inventory

The writer inventory names these classes:

- backend Worker DB API write boundary for web/app, admin, and worker routes;
- worker-uplift runtime services, including scheduler and stage containers;
- protected backend mutation workflows for admin, migration, and maintenance
  jobs;
- manual or break-glass backend PostgreSQL access, guarded by explicit operator
  freeze confirmation;
- backend-to-Supabase sync relay, documented as a source reader and standby
  writer that is not paused by this gate.

Any writer evidence outside that inventory fails closed as `unknown_writer`.

## Result

The gate artifact is `backend-supabase-standby-writer-pause-gate.json` and
includes:

- `failover_attempt_id`;
- `repository_revision`;
- `writer_inventory_fingerprint`;
- `source_fingerprint`;
- `target_fingerprint`;
- `pause_started_at_utc`;
- `measured_at_utc`;
- `expires_at_utc`;
- `quiet_window_seconds`;
- `actual_quiet_window_seconds`;
- required, paused, and failed writer-class counts;
- per-writer class, kind, production-write-path flag, safe pause status, and
  blockers;
- first and second write-position timestamps;
- stable write-position fingerprint;
- overall `status` as `PASS` or `FAIL`;
- `safe_metadata_only=true`.

The artifact must not contain database URLs, passwords, SQL text, row data,
Supabase project refs, Supabase keys, private keys, backend host metadata, or
raw PostgreSQL errors.

## Manual Run

```bash
gh workflow run backend-supabase-standby-writer-pause-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=pause-and-prove \
  -f failover_attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ) \
  -f quiet_window_seconds=120 \
  -f drain_timeout_seconds=120 \
  -f confirmation=pause-backend-writers-for-supabase-standby \
  -f manual_freeze_confirmation=no-manual-backend-postgres-writes
```

The workflow enforces the final gate result. Any `FAIL` blocks later failover
decision consumers.

## Abort Recovery

If the failover attempt aborts before provider switch, run the protected
recovery action for the same attempt:

```bash
gh workflow run backend-supabase-standby-writer-pause-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=resume-aborted-attempt \
  -f failover_attempt_id=<same-attempt-id> \
  -f quiet_window_seconds=120 \
  -f drain_timeout_seconds=120 \
  -f confirmation=resume-backend-writers-after-aborted-failover
```

This recovery action resumes only the writer state recorded by the fixed pause
manager. The resume report includes a safe `resume_verification` block that
compares observed post-resume state with the recorded pre-pause writer state.
It is not a provider switch and is not used after a completed failover.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_writer_pause_gate.py
python3 -m unittest tests.test_backend_supabase_standby_writer_pause_gate
python3 -m unittest tests.test_backend_writer_pause_manager
python3 -m unittest tests.test_backend_postgres_write_position
```

Required fixture coverage:

- complete pause plus stable write-position quiet window passes;
- active-writer, unknown-writer, failed-pause, drain-timeout, resumed-writer,
  and observed-write fixtures fail;
- incomplete inventory, active writer workflow, missing manual freeze, and stale
  evidence fail;
- the fixed manager can resume original writers after an aborted attempt and
  fails closed when recorded runtime replicas are not restored;
- artifact output remains safe metadata only.

## Acceptance Evidence

For #526, record:

- merged backend PR and main checks;
- protected backend apply evidence installing `/usr/local/sbin/nutsnews-writer-pause`;
- a main-branch `Backend Supabase Standby Writer Pause Gate` run URL;
- artifact status, inventory fingerprint, writer-class counts, quiet window,
  write-position stable status, and blockers;
- a leak scan showing no database URLs, private keys, Supabase tokens, password
  fields, backend host metadata, Supabase host/project metadata, raw row data,
  or SQL text.

If the live result is `FAIL`, failover remains blocked until a later run
produces passing writer-pause and quiescence evidence.
