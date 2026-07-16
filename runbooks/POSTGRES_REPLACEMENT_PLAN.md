# PostgreSQL Replacement Plan

This runbook covers backend issue #28 for `65.75.201.18`.

## Acceptance Criteria

- A decision record and implementation plan exist before any production PostgreSQL install.
- The plan includes RPO/RTO goals, backup/PITR/restore drill, failover behavior, and rollback.
- Supabase-specific features are mapped to replacements or kept explicitly.
- No database cutover, migration, `Protected Ansible Apply`, or production mutation is run without separate explicit approval.

## Decision

Keep Supabase until a replacement is proven in non-production.

Do not install PostgreSQL on the backend host now. Do not cut production traffic over now.

Machine-readable plan:

```text
docs/backend-postgres-replacement-plan.json
```

Validator:

```bash
python3 scripts/validate_postgres_replacement_plan.py
```

## Current Supabase Usage Inventory

Evidence from `ramideltoro/nutsnews` shows:

- the app uses `@supabase/supabase-js`;
- public reads use Supabase URL plus anon key;
- server/admin reads and writes use Supabase URL plus service-role key;
- the app relies on Supabase/PostgREST-style `.from(...)` and `.rpc(...)` calls;
- migrations define article, summary, feed, worker, AI usage, review, snapshot, quota, feature flag, release readiness, and migration-contract tables/functions;
- RLS/grant behavior is covered by regressions;
- public search uses Postgres full-text search through `public.search_articles`;
- the current REST backup helper is useful for diagnostics but is not a full database backup.

No app dependency was identified for Supabase Storage, Supabase Realtime, or Supabase Edge Functions. Admin auth currently uses NextAuth Google with JWT sessions, not a Redis or Supabase session store.

## Target Topology

- One production writer.
- No multi-writer topology.
- Optional async standby only after PITR and restore drills are proven.
- pgBouncer if app connection count or direct Postgres access requires pooling.
- Database and management dashboard private-only or behind a reviewed access boundary.
- No public database port.
- A PostgREST-compatible data API or app rewrite is required before cutover; bare PostgreSQL is not a drop-in replacement for current `supabase-js` usage.

## RPO And RTO

Target after implementation:

- RPO: 15 minutes after WAL archiving/PITR is implemented and tested.
- RTO: 4 hours for manual restore or failover after restore drills pass.

Current backend repo state does not guarantee those targets because production Supabase credentials, full dump evidence, WAL/PITR, and restore drill evidence are not available here.

## Supabase Feature Mapping

| Supabase feature | Replacement or explicit keep |
| --- | --- |
| Postgres tables/functions | Self-hosted PostgreSQL migrations and restore validation. |
| REST/PostgREST API | Deploy compatible PostgREST layer or rewrite app to server-owned database APIs. |
| anon and service-role keys | Replace with explicit JWT/RLS model or app server credentials in protected secrets. |
| RLS/grants | Port policies and run local RLS regressions against restored non-production data. |
| Auth | Keep NextAuth for admin unless a future app issue proves Supabase Auth is used. |
| Storage | Keep explicitly not used; choose R2/S3/local storage only if app usage appears. |
| Realtime | Keep explicitly not used unless a future feature requires websocket replication. |
| Edge Functions | Keep explicitly not used. |
| Full-text search | Keep Postgres full-text search and GIN indexes. |
| Backups | Replace limited REST export with encrypted full database backups plus WAL/PITR. |

## Implementation Phases

1. Inventory production Supabase project metadata, schema, extensions, grants, RLS, row counts, backup state, and secrets.
2. Restore a production-like dump to an isolated non-production Postgres instance.
3. Run `supabase/restore_validation.sql`, migration-contract tests, RLS regressions, API contract tests, and app smoke tests against the restored target.
4. Build either a PostgREST-compatible API layer or an app-owned database API rewrite.
5. Add encrypted off-box base backups, WAL archiving, PITR restore drills, backup freshness alerts, and restore reports.
6. Run read-only staging against restored data while production writes remain on Supabase.
7. Only after separate explicit approval, pause writers, take a final backup, restore or catch up replication, switch env vars, run smoke tests, and monitor.

## Rollback

Before cutover:

- discard the self-hosted target and keep Supabase.

During cutover:

- switch app and worker environment variables back to Supabase;
- resume writers only after consistency is confirmed.

After cutover:

- rollback only inside the documented rollback window unless reverse replication is proven;
- otherwise run a forward recovery plan.

## Current Blockers For Deployment

- Production Supabase credentials and project metadata are not available in this backend repo.
- No full production dump, WAL archive, PITR restore drill, or non-production rehearsal evidence exists here.
- The app currently uses Supabase/PostgREST-style APIs, so a bare PostgreSQL server is not a drop-in replacement.
- Protected backend apply and dashboard access boundaries are not yet approved for a database deployment.
