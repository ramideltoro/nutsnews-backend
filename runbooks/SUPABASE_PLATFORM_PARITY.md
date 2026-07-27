# Supabase Platform Parity Decision

Issue: #106

## Decision

Backend PostgreSQL promotion does not require moving Supabase Auth, Storage,
Realtime, or Edge Functions for the current NutsNews workload. The hard blocker
is Supabase Data API/PostgREST compatibility for the web app and worker.

Machine-readable record:

```text
docs/supabase-platform-parity-decision.json
```

Validator:

```bash
python3 scripts/validate_supabase_platform_parity_decision.py
```

## Feature Decisions

| Feature | Decision | Owner | Cutover impact |
| --- | --- | --- | --- |
| Auth | Keep current app auth model; do not migrate Supabase Auth now. | `ramideltoro/nutsnews` | Not a blocker unless new `supabase.auth` usage appears. |
| Storage | No object-byte migration now. | `ramideltoro/nutsnews` | Protected inventory must still confirm bucket count before cutover. |
| Realtime | Remove from cutover scope. | `ramideltoro/nutsnews` | Backend logical replication remains separate migration plumbing. |
| Edge Functions | Confirmed unused in known projects. | `ramideltoro/nutsnews` | No Edge Function runtime is moved to backend now. |
| Data API/PostgREST | Replace before cutover. | `ramideltoro/nutsnews-backend` plus app/worker repos | Blocks #119 until #111 and #117 pass. |
| API keys | Replace or rotate production writer credentials only. | `ramideltoro/nutsnews` | Supabase writer credentials must not remain in normal app/worker write paths, but standby credentials and the existing production Supabase hot-standby path are retained after #505/#506. |

## Evidence

- `ramideltoro/nutsnews` targeted scan found `@supabase/supabase-js` and
  `.from(...)` data access in the web app, but no direct Storage, Realtime, or
  Functions client usage.
- `ramideltoro/nutsnews-worker` targeted scan found `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, PostgREST table calls, and RPC refresh calls.
- Production Storage bucket metadata returned zero buckets.
- Production Auth admin metadata returned zero users.
- Supabase CLI listed zero Edge Functions for the production and staging project refs.
- Supabase docs state database backups include Storage metadata, not Storage
  object bytes; custom `auth` and `storage` schema changes require separate
  handling; and Realtime is separate from logical replication used for database
  copy workflows.

## Companion Issues

- App/API replacement: https://github.com/ramideltoro/nutsnews/issues/255
- Worker replacement: https://github.com/ramideltoro/nutsnews-worker/issues/27

## Cutover Gate

Production cutover stays blocked until:

1. #111 chooses and implements the backend PostgreSQL compatibility path;
2. #117 adds provider modes for Supabase primary, backend shadow, and backend
   primary;
3. staging rehearsal proves app and worker behavior without Supabase writes;
4. #114 or a later approved cleanup issue rotates or removes only obsolete
   migration/writer-path Supabase keys. It must not remove `supabase-standby`
   credentials or backend-to-Supabase standby relay credentials after #505/#506.

## Pre-Cutover Recheck

Before #119:

```bash
python3 scripts/validate_supabase_platform_parity_decision.py
```

Also re-run protected metadata checks for:

- Supabase Auth user count;
- Storage bucket count and object migration need;
- Edge Function list for the production project ref;
- app and worker scans for new Storage, Realtime, Functions, or Auth client usage.

## Rollback

This decision makes no runtime change. Rollback is to revert the decision record
and keep Supabase as the production writer.

If later compatibility work starts but fails, keep `supabase_primary` as the
active provider mode, do not run #119, and leave Supabase keys in place until a
new replacement path is approved.

## References Checked 2026-07-18

- https://supabase.com/docs/guides/platform/backups
- https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore
- https://supabase.com/docs/guides/database/replication
