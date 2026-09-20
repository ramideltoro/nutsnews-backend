# Pause the obsolete rehearsal migration subscription

On 2026-09-20 PostgreSQL repeatedly reported a missing migration replication slot. The enabled `nutsnews_backend_migration_sub` belongs only to `nutsnews_restore_rehearsal`; production clients use the separate `nutsnews_primary_shadow` database. Recreating the absent source slot would not prove historical WAL continuity or data parity.

The protected `pause-obsolete-rehearsal-subscription.yml` workflow has a read-only `apply=false` default. After a passing preflight, `apply=true` disables only that named subscription. It neither drops nor detaches the subscription, changes its connection/slot/publication, removes data, changes credentials, restarts PostgreSQL, nor changes any publisher or standby resource. This is reversible containment of a broken rehearsal stream, not a claim that replication has recovered.

Guards require the exact rehearsal database, a separate production database, exactly one matching subscription/slot/publication, no other rehearsal client connections, no prepared transactions, and a non-recovery server. A five-second lock timeout and fifteen-second statement timeout bound the transaction. Repeated apply is idempotent. No connection URL or arbitrary SQL identifier is accepted from workflow inputs. Output is sanitized metadata only.

## Verification

Run the protected workflow first with `apply=false`, then with `apply=true`. Confirm `subenabled=false` for the rehearsal subscription, public backend readiness and production queries remain healthy, application process identities are unchanged, and no new missing-slot errors occur after the change. Existing log findings remain until a completed inspection of a later window establishes remediation. The unrelated full baseline apply must not run as part of this scoped repair.

The subscription definition and all rehearsal data remain intact. Existing production backup destinations and retention remain unchanged. No restore is performed against either live database. Before any future re-enable, create a new approved rehearsal from a verified source and establish slot continuity/schema/data parity; simply enabling the old definition repeats the known failure. The SQL reversal is `ALTER SUBSCRIPTION nutsnews_backend_migration_sub ENABLE` in the rehearsal database only, after those checks.

Obsolete-resource deletion is a separate operation governed by `SUPABASE_CLEANUP_RETENTION_POLICY.md`; this workflow removes nothing and preserves the accepted standby path.

Shared documentation is published by the trusted NutsNews merge-documentation workflow from this merged change, with recorded provenance and its normal validation gates.
