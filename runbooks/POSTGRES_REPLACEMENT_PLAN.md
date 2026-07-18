# PostgreSQL Failover Target

This runbook covers backend issue #13 for `65.75.201.18`.

## Acceptance Criteria

- A decision record and implementation plan exist before production use.
- PostgreSQL is installed only through the protected backend Ansible pipeline.
- PostgreSQL listens on loopback only; no public `5432` rule is allowed.
- The database dashboard is reachable only through an SSH tunnel.
- A non-production Supabase restore drill proves the restore path before production use.
- Production cutover remains disabled until a separate reviewed approval covers the app/API compatibility layer, writer pause, final restore/catch-up, smoke tests, and rollback window.

## Decision

Deploy a private restore-verified PostgreSQL failover target on the backend host.

Supabase remains the only production writer. The backend PostgreSQL service is not
active-active, not bidirectional, and not a public database endpoint. The first
safe operating mode is manual logical dump and restore into a private rehearsal
database using staging Supabase data.

Machine-readable plan:

```text
docs/backend-postgres-replacement-plan.json
```

Primary migration parity contract:

```text
docs/supabase-backend-postgres-parity.json
```

Validator:

```bash
python3 scripts/validate_postgres_replacement_plan.py
python3 scripts/validate_supabase_backend_postgres_parity.py
```

## Supabase Capability Check

Current Supabase documentation supports these safe paths:

- CLI logical dump and restore for migrations and backup/restore workflows;
- provider daily backups and PITR for supported plans;
- external logical replication to another PostgreSQL database when a publication
  and replication slot are explicitly configured;
- managed Supabase read replicas for read-only capacity inside Supabase.

The first backend implementation uses CLI logical dump/restore because it is
the lowest-risk path to prove self-hosted restore mechanics without introducing
write conflicts.

## Target Topology

- Production writer: Supabase only.
- Backend target: local PostgreSQL on `127.0.0.1:5432`.
- Rehearsal database: `nutsnews_restore_rehearsal`.
- Dashboard: Adminer behind Caddy/PHP-FPM on `127.0.0.1:8082`.
- Secure transport: SSH tunnel to loopback; no remote database TCP access.
- Public ports: only 22, 80, and 443.
- Continuous replication: not enabled in this issue; lag is reported as
  `not_configured`.

Migration access is documented in [DB_MIGRATION_ACCESS.md](DB_MIGRATION_ACCESS.md).
The approved path is SSH to the backend host followed by loopback PostgreSQL;
public `5432` remains forbidden.

## Protected Apply

The normal backend baseline workflow enables PostgreSQL when the
`production-backend` Environment has:

```text
NUTSNEWS_BACKEND_POSTGRES_ENABLED=true
NUTSNEWS_BACKEND_DB_DASHBOARD_ENABLED=true
NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD
NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD
NUTSNEWS_STAGING_SUPABASE_PROJECT_REF
SUPABASE_ACCESS_TOKEN
```

Run check mode first:

```bash
gh workflow run protected-backend-ansible-apply.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=check
```

Apply only after checks are reviewed:

```bash
gh workflow run protected-backend-ansible-apply.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=apply \
  -f confirm_apply=backend.nutsnews.com
```

## Dashboard Access

Adminer is not public. Access it with an SSH tunnel:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 \
  -L 8082:127.0.0.1:8082 \
  rami@65.75.201.18
```

Then open:

```text
http://127.0.0.1:8082/
```

Use the local PostgreSQL role credentials stored in the protected GitHub
Environment. Never paste database passwords into issues, PRs, screenshots,
logs, or run summaries.

## Restore Drill

Dry-run/status:

```bash
gh workflow run backend-postgres-failover-drill.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=dry-run
```

Restore staging Supabase public schema/data into the backend rehearsal database:

```bash
gh workflow run backend-postgres-failover-drill.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=restore-staging \
  -f confirm_restore=restore-staging-to-backend-postgres
```

The workflow:

1. links to the staging Supabase project using `SUPABASE_ACCESS_TOKEN`;
2. dumps only the `public` schema and data;
3. copies the dump over SSH to the backend host;
4. recreates `nutsnews_restore_rehearsal`;
5. restores schema and data as the local `postgres` system user;
6. validates required NutsNews tables/views and row counts;
7. writes `/var/lib/nutsnews/postgres/status.json`;
8. refreshes the ops dashboard and textfile metrics.

## Observability

PostgreSQL readiness appears in:

- `/var/lib/nutsnews/postgres/status.json`;
- the loopback ops dashboard at `127.0.0.1:8081`;
- textfile metrics:
  - `nutsnews_backend_postgres_failover_ready`;
  - `nutsnews_backend_postgres_restore_drill_healthy`;
  - `nutsnews_backend_postgres_replication_lag_configured`;
- recurring backend health reports as `postgres_restore_readiness`.

Backup status remains visible through the existing backup status files and
Grafana dashboard. The restore drill is database-specific and complements
service-aware host backups.

## RPO And RTO

Current tested state:

- RPO: the age of the latest manual/approved dump.
- RTO: target 4 hours after restore drills pass.

Future target:

- RPO: 15 minutes after PITR/WAL or reviewed continuous replication is
  implemented and tested.
- RTO: 4 hours for controlled restore and app failover.

## Failover

Failover is not automatic.

Before any production failover:

1. declare the incident and pause production writers if possible;
2. choose the authoritative source of truth;
3. run an approved production dump/restore or reviewed replication catch-up;
4. verify migration head, required tables/views, row counts, and public feed
   queries;
5. switch app/worker environment only after the app/API compatibility layer is
   ready;
6. smoke-test public reads and protected admin operations;
7. monitor PostgreSQL, backups, and app errors.

Bare PostgreSQL is not a drop-in replacement for the current Supabase
PostgREST data API. A production cutover requires a reviewed PostgREST-compatible
layer or app-owned API change.

## Failback

Sync-back to Supabase is not supported yet.

Bidirectional writes are explicitly forbidden because conflict resolution and
split-brain prevention are not proven. The safe recovery path is forward-only:
pause writers, compare evidence, choose one authoritative database, then restore
or migrate once through a reviewed production procedure.

## Rollback

Before production cutover:

- disable `NUTSNEWS_BACKEND_POSTGRES_ENABLED` and run protected apply; or
- leave PostgreSQL installed but unused and keep Supabase as the writer.

During cutover:

- switch app and worker environment variables back to Supabase;
- resume writers only after consistency is confirmed.

After cutover:

- rollback only inside the documented rollback window unless reverse replication
  is separately proven.
