# Backend PostgreSQL Smoke Tests

Issue: #105

## Purpose

The smoke suite proves that a restored backend PostgreSQL rehearsal database can
serve critical NutsNews query paths before staging rehearsal or production
cutover continues.

It emits safe metadata only: query id, category, state, aggregate counts,
latency, `EXPLAIN` status, and rollback-only write check state. It must not
print row contents, credentials, database URLs, or production Supabase data.

## Covered Paths

| Category | Coverage |
| --- | --- |
| Public feed | `public.public_feed_snapshot` count and feed query plan. |
| Article | `public.articles` detail lookup count and plan. |
| Search | `public.search_articles` function execution and plan. |
| Worker | `public.worker_runs` count and rollback insert. |
| Admin | `public.article_ai_reviews` count and plan. |
| Quota | `public.quota_usage_events` count plus rollback insert/update/delete. |
| Release readiness | `public.release_readiness` singleton check. |
| Dashboard | `public.feed_health` count and plan. |

## Local Offline Check

```bash
python3 scripts/backend_postgres_smoke_tests.py --offline
```

Offline mode validates report shape and CI wiring without requiring database
credentials.

## Protected Staging Validation

```bash
gh workflow run backend-postgres-smoke-tests.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref db-primary-migration-query-smoke-tests \
  -f mode=validate-staging
```

The live workflow:

1. requires the `production-backend` protected environment;
2. opens an SSH tunnel to backend PostgreSQL loopback;
3. builds a masked connection URL for the rehearsal database;
4. runs read-path counts and `EXPLAIN` checks;
5. runs write-path checks inside transactions that roll back.

## Cutover Gate

Staging rehearsal and production cutover remain blocked if any required smoke
check fails. Failures include the query id and category so the owning app,
worker, or backend issue can be routed without exposing row data.

Smoke tests do not replace full parity validation. Run them after restore and
parity validation.

## Rollback

No runtime rollback is required for offline validation.

For protected live validation, failed checks should leave no committed data
because write checks run inside explicit rollback transactions. If a workflow
fails before rollback can complete, keep the rehearsal database isolated and
rebuild it through the restore workflow before continuing.
