# Supabase To Backend PostgreSQL Inventory

Issue: #98
Updated: 2026-07-18
Owner: backend operations

## Source Projects And Access Modes

| Environment | Project reference source | Connection modes | Status |
| --- | --- | --- | --- |
| Production | `NUTSNEWS_PRODUCTION_SUPABASE_PROJECT_REF` | Supabase Management API through `SUPABASE_ACCESS_TOKEN`; direct PostgreSQL through `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL`; project API through `NUTSNEWS_PRODUCTION_SUPABASE_URL` | Secret names defined; live object dump requires protected workflow approval |
| Staging | `NUTSNEWS_STAGING_SUPABASE_PROJECT_REF` default `zjatvzhppkvqcgqitevu` | Supabase CLI link through `SUPABASE_ACCESS_TOKEN`; protected staging dump workflow | Existing restore drill source |
| Backend target | `NUTSNEWS_BACKEND_HOST` default `65.75.201.18` | SSH tunnel to loopback PostgreSQL only; no public `5432` | Repo-managed by protected Ansible apply |

Direct Supabase database access must not use a pooler for logical replication.
Before any rehearsal that needs direct PostgreSQL connectivity, #102/#121 must
record whether the runner can reach Supabase over IPv6 or needs the Supabase
IPv4 add-on.

## Database Object Inventory

The current repo-owned restore drill and failover plan identify these required
NutsNews database objects. A protected direct-DB inventory run must replace this
planning inventory with live object counts before production cutover.

| Object | Type | Owner | Classification | Migration status | Validation |
| --- | --- | --- | --- | --- | --- |
| `public` | schema | backend operations | migrate | required | schema exists; grants/RLS compared by parity suite |
| `public.articles` | table | app data | migrate | required | row count, checksum, sequence checks |
| `public.article_summaries` | table | app data | migrate | required | row count and checksum |
| `public.rss_feeds` | table | worker data | migrate | required | row count, checksum, write smoke test |
| `public.worker_runs` | table | worker data | migrate | required | row count and latest timestamp |
| `public.article_ai_reviews` | table | app data | migrate | required | row count and checksum |
| `public.ai_usage_runs` | table | ops data | migrate | required | row count and latest timestamp |
| `public.feed_health` | table | ops data | migrate | required | row count and latest timestamp |
| `public.feed_quality_scores` | table | worker data | migrate | required | row count and checksum |
| `public.quota_usage_events` | table | quota system | migrate | required | row count and checksum |
| `public.runtime_feature_flags` | table | release operations | migrate | required | row count and key comparison |
| `public.release_readiness` | table | release operations | migrate | required | row count and latest timestamp |
| `public.migration_schema_contract` | table | release operations | migrate | required | migration head comparison |
| `public.public_feed_snapshot` | materialized view | app data | migrate | required | refresh succeeds; row count |
| `public.best_feeds` | view | app data | migrate | required | query smoke count |
| `public.bad_feeds` | view | app data | migrate | required | query smoke count |
| `public.search_articles` | function or view | app data | migrate | required | critical search smoke query |
| `pg_trgm` | extension | backend operations | migrate | required | extension version compatible |

## Supabase-Managed Areas

| Area | Owner | Classification | Migration status | Notes |
| --- | --- | --- | --- | --- |
| `auth` schema | app auth owner | keep_on_supabase_temporarily | compatibility decision pending #106/#111 | Current backend plan says admin auth uses NextAuth Google/JWT; restore drill shims only `auth.users` when public foreign keys require it. |
| `storage` schema and buckets | app/storage owner | keep_on_supabase_temporarily | decision pending #106 | Supabase database backups include metadata but not restored object bytes. No app storage client dependency has been identified in this repo. |
| `realtime` schema/publications | app/runtime owner | exclude_with_reason | decision pending #106/#109 | No realtime client dependency identified in this repo; logical replication publication is separate migration plumbing. |
| `supabase_migrations` | release operations | migrate | required when available | Migration history must be dumped or equivalently compared before cutover. |
| Edge Functions | app/runtime owner | exclude_with_reason | decision pending #106 | No repo-owned Edge Function dependency identified here. |
| PostgREST/Data API | app/API owner | replace | hard blocker #111 | Bare PostgreSQL is not a `supabase-js`/PostgREST replacement. |
| Supabase API keys | platform owner | replace_or_rotate | hard blocker #117/#114 | Service-role style access must become least-privilege backend credentials before cutover. |

## Client Inventory

| Client class | Owner repo | Supabase dependency | Classification | Follow-up |
| --- | --- | --- | --- | --- |
| Web app/API | `ramideltoro/nutsnews` | `@supabase/supabase-js`, project URL, anon key, service-role key, PostgREST/RPC assumptions | replace | #111 and #117 |
| Workers/cron | `ramideltoro/nutsnews-worker` or app repo if colocated | Direct Supabase client/service-role usage must be confirmed | replace | #105, #111, #117 |
| Backend ops workflows | `ramideltoro/nutsnews-backend` | Supabase CLI token and project refs for dump/restore | migrate | #102, #103, #104 |
| Dashboards/admin | `ramideltoro/nutsnews-backend` | Safe metadata only; no row data or secrets | replace | #110, #113 |

## Named Secrets Only

The migration requires these secret names; values must stay in protected GitHub
Environment secrets or a local secret store:

- `SUPABASE_ACCESS_TOKEN`
- `NUTSNEWS_PRODUCTION_SUPABASE_PROJECT_REF`
- `NUTSNEWS_PRODUCTION_SUPABASE_URL`
- `NUTSNEWS_PRODUCTION_SUPABASE_ANON_KEY`
- `NUTSNEWS_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY`
- `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL`
- `NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_RESTORE_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_VALIDATION_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_REPLICATION_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_APP_REHEARSAL_PASSWORD`

## Explicit Unknowns And Tracking

| Unknown | Blocking issue |
| --- | --- |
| Live production object inventory for schemas, roles, grants, RLS, functions, triggers, indexes, sequences, publications, policies, and extensions | #107 parity validation suite |
| Production direct DB connectivity path, including IPv4 add-on versus IPv6 support | #102 and #121 |
| Supabase Auth/Storage/Realtime/Edge Function production dependency status outside this backend repo | #106 |
| App/worker Supabase API call inventory and replacement implementation | #111 and #117 |
| Production dump/restore, replication, staging rehearsal, rollback, and cutover evidence | #103 through #119 |

## References Checked 2026-07-18

- https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore
- https://supabase.com/docs/guides/database/postgres/setup-replication-external
- https://supabase.com/docs/guides/database/replication
- https://supabase.com/docs/guides/platform/backups
