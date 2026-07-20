# Backend PostgreSQL future-primary topology

Issue: #210

Tracking issue: #120

This topology defines the backend PostgreSQL database that was prepared to become the primary NutsNews database. The source of truth is the machine-readable manifest at `docs/backend-postgres-future-primary-topology.json`.

Current production status: #119 completed on 2026-07-20 through the protected workflow path. Backend PostgreSQL now serves as production primary for public app and worker data paths. Supabase is retained only for #114 rollback-window, reconciliation, and retirement work until explicit cleanup approval is recorded.

## Decision

The backend primary candidate is a separate database named `nutsnews_primary_shadow` on the backend PostgreSQL host. It is distinct from the existing restore rehearsal and backup-restore proof databases.

#119 was explicitly approved and executed through the protected workflow boundary. The backend PostgreSQL target is now the production primary. The former Supabase primary remains available during the #114 observation and retirement window; no Supabase credential removal or replication-resource cleanup may proceed until that issue records owner approval and rollback coverage.

## Guardrails

- No active-active writes.
- No bidirectional replication.
- No rollback to Supabase without writer-pause evidence, a reviewed sync point, and explicit rollback owner coverage.
- No public PostgreSQL listener or public 5432 exposure.
- No manual production cutover outside protected workflows.
- No Supabase write credential retirement or logical replication cleanup before #114 approval.
- Evidence must be safe metadata only: workflow links, artifact IDs, aggregate counts, checksums, hashes, durations, RPO/RTO, and watermarks.
- Evidence must never include database URLs, passwords, service-role keys, dumps, or row data.

## Target

`nutsnews_primary_shadow` is the backend PostgreSQL primary target. It is private to the backend host and reached only through the approved loopback/SSH-tunnel path. After #119, production app and worker paths use this backend PostgreSQL target through the approved backend API/provider-switch boundary.

The shadow target must be provisioned with the roles required to replay Supabase-compatible schema behavior and validate parity:

- `postgres` for protected workflow administration.
- `nutsnews_migration_restore` for restore ownership and protected restore operations.
- `nutsnews_migration_validation` for metadata and aggregate validation.
- `nutsnews_migration_replication` for backend-side replication setup during the migration window.
- `nutsnews_readonly` for operator inspection through approved access paths.
- `nutsnews_app` as the future application role, with writes disabled before cutover.
- `anon`, `authenticated`, and `service_role` as compatibility role names for restored grants and RLS behavior only.

## Dependency gates

#210 is the topology gate. The remaining backend-only work is intentionally split into independent units:

- #211 provisions `nutsnews_primary_shadow`, roles, grants, and loopback-only access evidence.
- #212 restores a production Supabase snapshot into `nutsnews_primary_shadow`.
- #215 validates Supabase schema and behavior parity against the shadow target.
- #213 configures one-way production logical replication to the shadow target.
- #214 proves replication health and production-to-shadow parity with safe metadata.
- #216 proves backup and restore for the shadow database into an isolated restore target.
- #217 monitors the shadow database, replication, alerts, backup freshness, and failure handling.
- #119 is complete; final cutover evidence is recorded on the issue.
- #114 is active; remaining work is post-cutover reconciliation, Supabase write credential retirement, logical replication cleanup, and Supabase archive/retirement decision after rollback-window owner approval.

## Validation

Run:

```bash
python3 scripts/validate_backend_postgres_future_primary_topology.py
```

The backend checks workflow runs the same validator.
