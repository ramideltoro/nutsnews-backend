# Backend DB Rollback And Failback Guardrails

Issue: #118

Depends on #109, #111, and #117.

## Purpose

Prevent split-brain and define when rollback to Supabase is safe versus when
forward recovery is required.

Machine-readable guardrails:

```text
docs/backend-db-rollback-guardrails.json
```

Validators:

```bash
python3 scripts/validate_backend_db_rollback_guardrails.py
python3 scripts/backend_db_rollback_guardrails_plan.py --phase supabase_primary
```

## Single-Writer Phases

| Phase | Provider mode | Writer |
| --- | --- | --- |
| `supabase_primary` | `supabase_primary` | Supabase |
| `shadow_reads` | `backend_postgres_shadow` | Supabase |
| `final_catch_up` | `backend_postgres_shadow` | none until switch |
| `backend_primary` | `backend_postgres_primary` | backend PostgreSQL |
| `rollback_window` | `backend_postgres_primary` | backend PostgreSQL unless rollback is approved |

No phase may allow concurrent writes to Supabase and backend PostgreSQL.

## Writer Pause Verification

- Verify provider mode is not production-primary by default.
- Pause worker schedules/queues through app and worker runbooks.
- Record Supabase write watermarks before and after the pause.
- Run parity, smoke, and replication health checks after the pause.

## Rollback Boundary

Rollback to `supabase_primary` is safe only while backend PostgreSQL has not
accepted authoritative writes beyond the verified Supabase sync point. After
that boundary, failback requires a reviewed sync-back procedure; otherwise the
recovery path is forward recovery on backend PostgreSQL.

## Current Status

Staging rehearsal has completed, and app/worker pause guards are deployed. The
remaining rollback blockers are live-cutover specific:

- live writer-pause evidence across app, worker, scheduler, and admin paths;
- Supabase no-new-write watermark evidence after the pause timestamp;
- rollback owner coverage through the full rollback window;
- provider-switch owner approval and final go/no-go.

Final catch-up, backend-primary, and rollback-window guardrail plans must keep
failing closed until that live evidence is attached. If backend PostgreSQL
accepts authoritative writes beyond the verified Supabase sync point, rollback
becomes forward recovery unless a reviewed sync-back procedure exists.
