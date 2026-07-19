# Backend Database Provider Switch

Issue: #117

## Purpose

Make database provider switching explicit, reversible, and testable before
production cutover. The safe default remains `supabase_primary`.

Machine-readable contract:

```text
docs/backend-database-provider-switch.json
```

Validators:

```bash
python3 scripts/validate_backend_database_provider_switch.py
python3 scripts/backend_database_provider_switch_plan.py --mode supabase_primary
```

Dry-run workflow:

```bash
gh workflow run backend-database-provider-switch-dry-run.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref db-primary-migration-provider-switch \
  -f mode=backend_postgres_shadow \
  -f environment=non-production
```

## Modes

| Mode | Writer | Backend reads | Production responses |
| --- | --- | --- | --- |
| `supabase_primary` | Supabase | disabled | yes |
| `backend_postgres_shadow` | Supabase | comparison only | no |
| `backend_postgres_primary` | backend PostgreSQL | serving | yes |

`backend_postgres_primary` requires the exact confirmation
`enable-backend-postgres-primary` and a later protected cutover workflow.

## Required Environment

- `NUTSNEWS_DATABASE_PROVIDER_MODE`
- `NUTSNEWS_BACKEND_API_URL`
- `NUTSNEWS_BACKEND_API_TOKEN`

The backend API token is server/worker-only and must live in protected
environment secrets. Browser code must never receive it.

For worker shadow parity, backend issue #242 provides the backend-side route:

```text
POST https://backend.nutsnews.com/api/worker/db/<operation>
Authorization: Bearer ${NUTSNEWS_BACKEND_API_TOKEN}
```

Keep `NUTSNEWS_BACKEND_WORKER_API_WRITES_ENABLED=false` while workers run
`backend_postgres_shadow`; this makes backend writes fail closed even if a
write operation is accidentally called.

## Rollback

Rollback returns app and worker configuration to `supabase_primary` only inside
the rollback window, after writer-pause evidence and Supabase sync-point
evidence are available. After backend PostgreSQL accepts authoritative writes
beyond a verified Supabase sync point, rollback becomes forward recovery unless
a reviewed sync-back procedure exists.

## Current Status

App and worker provider modes are implemented:

- App provider tracker: https://github.com/ramideltoro/nutsnews/issues/255
- Worker provider tracker: https://github.com/ramideltoro/nutsnews-worker/issues/27
- Backend provider-shadow dry-run:
  https://github.com/ramideltoro/nutsnews-backend/actions/runs/29705168086

Remaining production blockers:

- app and worker provider-switch owner approval;
- final go/no-go owner approval;
- protected production cutover workflow approval;
- verified writer-pause and Supabase no-new-write watermark evidence;
- rollback owner coverage through the rollback window.

Do not run production `backend_postgres_primary` through the standalone
provider-switch dry-run workflow. It must continue to fail closed outside the
protected production cutover path.
