# Backend PostgreSQL Migration Access

Issues: #102, #121

## Approved Path

Migration workflows and operators reach backend PostgreSQL through SSH to
`backend.nutsnews.com` / `65.75.201.18`, then loopback PostgreSQL on the
backend host. PostgreSQL must remain bound to `localhost`; public `5432` is not
approved.

Approved tunnel pattern:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 -L 15432:127.0.0.1:5432 rami@65.75.201.18
```

Never paste connection strings, tunnel commands containing passwords, or
database URLs into issues, PRs, logs, screenshots, or summaries.

## Actors

| Actor | Access | Notes |
| --- | --- | --- |
| Protected GitHub Actions workflows | SSH key from `production-backend`; loopback PostgreSQL only | Requires environment approval before secrets are exposed |
| Read-only operators | SSH tunnel plus validation/read-only role | Verification only |
| Emergency break-glass operators | SSH read-only by default; mutation requires documented approval | Reconcile any manual change back into repo |
| App/worker runtime | No backend PostgreSQL production writes until #117/#119 | Supabase remains writer |
| Dashboard/admin tunnel users | Loopback-only Adminer or psql tunnel | No public dashboard or database port |

## Migration Role Boundary

| Role | Secret | Purpose | Cutover cleanup |
| --- | --- | --- | --- |
| `nutsnews_app` | `NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD` | Future production app owner role | Rotate at cutover and after any rollback |
| `nutsnews_readonly` | `NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD` | Read-only inspection and dashboard checks | Keep only if operationally needed |
| `nutsnews_migration_restore` | `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_RESTORE_PASSWORD` | Protected restore/rehearsal workflows | Revoke after rehearsal/cutover |
| `nutsnews_migration_validation` | `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD` | Parity and smoke validation; bypasses restored RLS for aggregate-only migration checks | Rotate or disable after cutover |
| `nutsnews_migration_replication` | `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD` | Backend subscription/replication setup | Drop when replication is retired |
| `nutsnews_app_rehearsal` | `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_APP_REHEARSAL_PASSWORD` | Non-production app/API cutover rehearsal | Drop after rehearsal |

## Preflight

Run safe local metadata checks:

```bash
python3 scripts/backend_postgres_migration_access_preflight.py --offline
```

Run protected live access checks from the workflow:

```bash
gh workflow run backend-postgres-migration-access-preflight.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=status
```

The live preflight must verify:

- DNS resolves to the expected backend host;
- public TCP `5432` is not reachable;
- SSH reaches the backend host using protected material;
- backend PostgreSQL listener is loopback-only;
- `pg_isready` succeeds locally on the host;
- migration roles exist without printing passwords;
- Supabase source direct-DB connectivity requirements are recorded.

After each isolated restore drill, the restore runner reapplies rehearsal
database grants for the read-only, validation, and app rehearsal roles because
the rehearsal database is dropped and recreated from the dump.

## Source Connectivity

Supabase source connectivity must be tested separately from backend target
connectivity. For dump workflows, `SUPABASE_ACCESS_TOKEN` and project refs are
required. For direct logical replication, `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL`
must be reachable from the approved runner/network path without a pooler.

Record whether the path uses IPv6 or requires the Supabase IPv4 add-on before
staging rehearsal or production replication.

## Revocation

After rehearsal or cutover:

1. remove unused migration-only GitHub Environment secrets;
2. rotate app/read-only credentials that were exposed to temporary workflows;
3. disable or drop migration-only PostgreSQL roles no longer needed;
4. remove temporary SSH known-host or tunnel material from runners;
5. record cleanup evidence in #114.
