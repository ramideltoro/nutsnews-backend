# Backend PostgreSQL future-primary topology

Issue: #210

Tracking issue: #120

This topology defines the backend PostgreSQL database that is being prepared to become the future primary NutsNews database. The source of truth is the machine-readable manifest at `docs/backend-postgres-future-primary-topology.json`.

## Decision

The backend primary candidate is a separate database named `nutsnews_primary_shadow` on the backend PostgreSQL host. It is distinct from the existing restore rehearsal and backup-restore proof databases.

Production Supabase remains the only production writer until #119 is explicitly approved and executed through the protected `production-backend` workflow boundary. The backend shadow database is restored from a production snapshot and then kept current with one-way logical replication from production Supabase to backend PostgreSQL.

## Guardrails

- No active-active writes.
- No bidirectional replication.
- No app or worker writes to backend PostgreSQL before #119 cutover approval.
- No public PostgreSQL listener or public 5432 exposure.
- No manual production cutover outside protected workflows.
- Evidence must be safe metadata only: workflow links, artifact IDs, aggregate counts, checksums, hashes, durations, RPO/RTO, and watermarks.
- Evidence must never include database URLs, passwords, service-role keys, dumps, or row data.

## Target

`nutsnews_primary_shadow` is the future-primary backend PostgreSQL shadow target. It is private to the backend host and reached only through the approved loopback/SSH-tunnel path. It is not a runtime application database until the cutover issue is completed.

The shadow target must be provisioned with the roles required to replay Supabase-compatible schema behavior and validate parity:

- `postgres` for protected workflow administration.
- `nutsnews_migration_restore` for restore ownership and protected restore operations.
- `nutsnews_migration_validation` for metadata and aggregate validation.
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
- #119 remains blocked until every shadow gate passes and protected cutover approval is available.
- #114 remains blocked until #119 is complete and the post-cutover observation window is satisfied.

## Validation

Run:

```bash
python3 scripts/validate_backend_postgres_future_primary_topology.py
```

The backend checks workflow runs the same validator.
