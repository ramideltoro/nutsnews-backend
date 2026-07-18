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

## Rollback

Rollback returns app and worker configuration to `supabase_primary` only inside
the rollback window, after writer-pause evidence and Supabase sync-point
evidence are available. After backend PostgreSQL accepts authoritative writes
beyond a verified Supabase sync point, rollback becomes forward recovery unless
a reviewed sync-back procedure exists.

## Current Blockers

- App provider modes: https://github.com/ramideltoro/nutsnews/issues/255
- Worker provider modes: https://github.com/ramideltoro/nutsnews-worker/issues/27
- Staging rehearsal must prove shadow mode, primary mode, and rollback.
