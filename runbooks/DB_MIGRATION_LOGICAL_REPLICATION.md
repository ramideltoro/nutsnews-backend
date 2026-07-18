# Backend PostgreSQL Logical Replication Plan

Issue: #109

## Status

Logical replication setup is planned but not live. Production setup remains
blocked until source direct database credentials are corrected, staging access
exists, and the `production-backend` protected environment approves the run.

Machine-readable plan:

```text
docs/backend-postgres-logical-replication-plan.json
```

Validator:

```bash
python3 scripts/validate_backend_postgres_logical_replication_plan.py
```

## Required Shape

- Supabase source uses a direct database connection, not a pooler.
- Backend target is reached through SSH tunnel to loopback PostgreSQL.
- Publication, slot, and subscription names use the `nutsnews_backend_migration_` prefix.
- Publication tables must exactly match the parity manifest's required public tables.
- Replica identity must be verified before replication starts.
- Staging rehearsal must prove insert, update, delete, and truncate propagation.
- WAL retention, slot activity, subscription state, and backend disk pressure must be monitored before production use.

## Planned Names

| Object | Name |
| --- | --- |
| Publication | `nutsnews_backend_migration_pub` |
| Slot | `nutsnews_backend_migration_slot` |
| Subscription | `nutsnews_backend_migration_sub` |

## Protected Setup

Status-only source inspection uses direct database URL secrets. Pooler
connections are rejected before any setup mutation.

Staging status-only source inspection:

```bash
gh workflow run backend-postgres-logical-replication.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f environment_name=staging \
  -f operation=status
```

Staging setup:

```bash
gh workflow run backend-postgres-logical-replication.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f environment_name=staging \
  -f operation=setup \
  -f confirmation=setup-staging-logical-replication
```

Production setup uses the same workflow with `environment_name=production` and
confirmation `setup-production-logical-replication`. Production setup is fixed
to `nutsnews_primary_shadow`, uses
`NUTSNEWS_PRODUCTION_SUPABASE_DB_DIRECT_URL`, creates a backend subscription
with `copy_data=false`, and does not permit app or worker writes to the backend.

Do not run production setup until #211 provisioning, #212 restore, and #215
behavior parity have been proven and recorded.

## Refresh Procedure

When the parity manifest adds or removes required tables:

1. update `docs/backend-postgres-logical-replication-plan.json`;
2. update the Supabase publication through protected setup;
3. run `ALTER SUBSCRIPTION ... REFRESH PUBLICATION` on the backend target;
4. verify slot lag and subscription health;
5. re-run smoke and parity checks.

## Rollback

1. Disable or drop the backend subscription.
2. Drop the Supabase replication slot after confirming no subscriber needs it.
3. Drop the Supabase publication only after no migration path depends on it.
4. Remove or rotate migration-only replication credentials.

## Current Blockers

- #211 protected provisioning evidence for `nutsnews_primary_shadow` is missing.
- #212 protected production restore evidence is missing.
- #215 production-shadow behavior parity evidence is missing.
- Live setup requires `production-backend` protected environment approval.

## References Checked 2026-07-18

- https://supabase.com/docs/guides/database/postgres/setup-replication-external
- https://supabase.com/docs/guides/database/replication
- https://www.postgresql.org/docs/current/sql-altersubscription.html
