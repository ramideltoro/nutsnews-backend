# Supabase Cleanup Retention Policy

Issue #506 changes post-cutover cleanup policy after #505 accepted the existing
production Supabase database as the hot standby backup target.

Supabase is retained as hot standby, not blindly retired. Backend PostgreSQL
remains the normal production read/write primary until a separate approved
failover path changes that state.

## Allowed Cleanup

Cleanup may remove only obsolete pre-cutover Supabase-to-backend migration
resources after all required evidence is recorded:

- resource names use the `nutsnews_backend_migration_` prefix;
- protected teardown dry-run evidence passes;
- the source replication slot is inactive;
- the backend subscription is detached or absent;
- #505 standby acceptance evidence is linked;
- #506 retention policy is linked;
- #114 or a later owner-approved cleanup issue explicitly approves apply.

## Retained Resources

Do not remove without a new owner-approved issue:

- the existing production Supabase project or database;
- the `supabase-standby` GitHub Environment;
- `NUTSNEWS_STANDBY_SUPABASE_*` secrets;
- backend-to-Supabase standby sync relay service, timer, environment file,
  contract, or last-run report;
- standby readiness, reconciliation, lag, parity, schema, sequence,
  writer-pause, split-brain, promotion, failover, recovery, staging drill, or
  production acceptance guardrails;
- existing production Supabase schema, sequence, function, or credential
  material needed by the accepted standby path.

## Validation

Run before cleanup policy changes:

```bash
python3 scripts/validate_backend_supabase_cleanup_retention_policy.py
python3 -m unittest tests.test_backend_supabase_cleanup_retention_policy
python3 scripts/validate_backend_postgres_logical_replication_plan.py
python3 scripts/validate_backend_database_provider_switch.py
python3 scripts/validate_backend_postgres_future_primary_topology.py
```

The protected logical-replication teardown workflow emits safe metadata fields
for `cleanup_scope`, `allowed_cleanup_resource_prefix`, and
`preserved_hot_standby_resources`. Any cleanup evidence that does not preserve
the standby path is not acceptable for #506 or #223.
