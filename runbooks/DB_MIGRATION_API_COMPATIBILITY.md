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

Live app API smoke after protected apply:

```bash
NUTSNEWS_BACKEND_API_URL=https://backend.nutsnews.com/api/app/db \
  python3 scripts/backend_app_db_api_smoke.py
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

Current evidence:

- App provider guardrails are merged in
  `ramideltoro/nutsnews#260`.
- Worker provider guardrails are merged for
  `ramideltoro/nutsnews-worker#27`.
- Backend app route provisioning is merged in
  `ramideltoro/nutsnews-backend#248`.
- Protected backend check run `29693574619` passed.
- Protected backend apply run `29693776534` passed.
- Live app API smoke passed against
  `https://backend.nutsnews.com/api/app/db`: provider smoke returned 200,
  public snapshot read returned rows, shadow writes returned 409, and
  backend-primary writes returned 403 while writes are disabled.
- The app helper shadow smoke passed against the same route without Supabase
  writes.

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

Production readiness uses the read-only app operation
`load-admin-production-readiness`. A tokened POST to
`https://backend.nutsnews.com/api/app/db/load-admin-production-readiness` with
`providerMode=backend_postgres_primary` must return one dashboard snapshot row
containing article counts, public feed snapshot count, recent published
articles, the latest Worker run, recent growth counts, and translation summary
coverage. The operation must use backend PostgreSQL least-privilege read access
and must not require Supabase service-role access in backend-primary mode.

Article review dashboards use the read-only app operation
`load-admin-article-reviews`. A tokened POST to
`https://backend.nutsnews.com/api/app/db/load-admin-article-reviews` with
`providerMode=backend_postgres_primary` must return one dashboard snapshot row
containing source/category filter options, recent published article rows and
their review lookup rows, AI decision version report rows or a nullable report
error, filtered review rows, matching published articles, a total match count,
and a nullable review error. Filters and pagination must be bounded and
parameterized, and the operation must not require Supabase service-role access
in backend-primary mode.

Article engagement dashboards use the read-only app operation
`load-admin-article-engagement`. A tokened POST to
`https://backend.nutsnews.com/api/app/db/load-admin-article-engagement` with
`providerMode=backend_postgres_primary` must return one dashboard snapshot row
containing source/category aggregate rows, a nullable source/category error,
article aggregate rows, and a nullable article error. Source/category and
article limits must be bounded and parameterized, and the operation must not
require Supabase service-role access in backend-primary mode.

AI usage dashboards use the read-only app operation `load-admin-ai-usage`. A
tokened POST to `https://backend.nutsnews.com/api/app/db/load-admin-ai-usage`
with `providerMode=backend_postgres_primary` must return one dashboard snapshot
row containing bounded `usageRunRows` from `public.ai_usage_runs` filtered by a
validated `since` timestamp. The row fields must preserve the app contract for
run metadata, OpenAI/local AI model telemetry, review and translation counts,
token totals, estimated costs and savings, acceptance/rejection counts,
cost-protection flags, save status, and duration. The operation must use
backend PostgreSQL least-privilege read access and must not require Supabase
service-role access in backend-primary mode.

Local AI dashboards use the read-only app operation `load-admin-local-ai`. A
tokened POST to `https://backend.nutsnews.com/api/app/db/load-admin-local-ai`
with `providerMode=backend_postgres_primary` must return one dashboard snapshot
row containing bounded local AI `usageRunRows` from `public.ai_usage_runs` and
bounded `recentReviewRows` from `public.article_ai_reviews`. Usage runs must be
filtered by a validated `since` timestamp plus local-provider or positive local
call-count criteria; recent reviews must be filtered to local-provider review
rows. The selected fields must preserve the app contract for runtime status,
model, fallback OpenAI calls, token totals, local AI review duration, accepted
and rejected counts, recent review metadata, and review duration. The operation
must use backend PostgreSQL least-privilege read access and must not require
Supabase service-role access in backend-primary mode.

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
