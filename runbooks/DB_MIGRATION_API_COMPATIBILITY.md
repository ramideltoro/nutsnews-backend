# Backend API Compatibility Contract

Issue: #111

## Decision

Use app-owned backend APIs for browser/admin paths and worker data APIs for
worker paths. Do not point `supabase-js` or worker PostgREST callers at bare
PostgreSQL.

Machine-readable contract:

```text
docs/backend-api-compatibility-contract.json
```

Validator:

```bash
python3 scripts/validate_backend_api_compatibility_contract.py
```

Offline app API smoke:

```bash
python3 scripts/backend_app_db_api_smoke.py --offline
```

## Provider Modes

| Mode | Writer | Production responses | Notes |
| --- | --- | --- | --- |
| `supabase_primary` | Supabase | yes | Safe default. |
| `backend_postgres_shadow` | Supabase | no | Backend PostgreSQL reads only for comparison. |
| `backend_postgres_primary` | backend PostgreSQL | yes | Requires protected cutover approval. |

## Required Companion Work

- App: https://github.com/ramideltoro/nutsnews/issues/255
- Worker: https://github.com/ramideltoro/nutsnews-worker/issues/27
- Worker API endpoint/token provisioning: https://github.com/ramideltoro/nutsnews-backend/issues/242
- App API endpoint/token provisioning: https://github.com/ramideltoro/nutsnews-backend/issues/247

Backend cutover remains blocked until those repos can run the smoke-test
capabilities against backend PostgreSQL in non-production.

## App And Worker Database API

The backend database compatibility API is disabled by default and, when enabled,
is bound on loopback behind Caddy at:

```text
https://backend.nutsnews.com/api/app/db/*
https://backend.nutsnews.com/api/worker/db/*
```

Protected apply inputs:

- `NUTSNEWS_BACKEND_WORKER_API_ENABLED=true` enables the loopback service and
  Caddy routes.
- `NUTSNEWS_BACKEND_API_TOKEN` is the bearer token accepted by the backend API
  and sent by the app server runtime and Worker runtime.
- `NUTSNEWS_BACKEND_WORKER_API_WRITES_ENABLED=false` keeps writes fail-closed
  for shadow parity.

The initial shadow configuration uses the backend PostgreSQL read-only role
against `nutsnews_primary_shadow`. Backend writes remain disabled until the
production cutover issue explicitly approves `backend_postgres_primary`.

App operations are allow-listed separately from worker operations. Current app
coverage includes public feed/article/search/sitemap reads, runtime feature
flags, readiness schema-contract reads, bounded admin dashboard read snapshots,
quota usage writes, article engagement writes, and runtime feature flag writes.
Write operations must return `409` in `backend_postgres_shadow` and `403` when
`backend_postgres_primary` is selected but deployment writes are still disabled.

## Authorization Rules

- Browser code must not receive database credentials or service-role style
  tokens.
- Admin APIs must keep existing admin session checks before using backend
  database credentials.
- Worker credentials must be least-privilege and scoped separately from app
  credentials.
- Supabase RLS behavior must be replaced by backend API authorization plus
  focused SQL grants before `backend_postgres_primary`.

## Cutover Gate

Production cutover is blocked unless:

- app and worker companion issues are implemented;
- `backend_postgres_shadow` runs without serving production responses;
- smoke tests pass against backend PostgreSQL;
- service-role Supabase access is absent from backend-primary paths;
- rollback to `supabase_primary` is documented and tested.
