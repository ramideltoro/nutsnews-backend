# Backend PostgreSQL Migration Access

Issues: #102, #121

## Approved Path

Migration workflows and operators reach backend PostgreSQL through SSH to
`backend.nutsnews.com` / `65.75.201.18`, then loopback PostgreSQL on the
backend host. PostgreSQL must remain bound to `localhost`; public `5432` is not
approved.

The active production-primary database retains its historical name
`nutsnews_primary_shadow`. It is kept separate from the restore rehearsal and
backup-restore proof databases. Backend PostgreSQL is the production primary;
Supabase is the hot standby. Release-schema maintenance must not change that
ownership.

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
| App runtime | Backend PostgreSQL production-primary API | Release migrations must preserve provider ownership |
| Split worker runtime | Shadow-only until its separate protected cutover | Do not use release migration as worker-cutover authority |
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
| `authenticator` | None; `NOLOGIN` compatibility role | Allows exact reviewed Supabase-origin migrations that alter PostgREST role defaults to run unchanged | Keep while the shared migration history requires it; it has no login or grants |

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
- `nutsnews_primary_shadow` exists with `nutsnews_migration_restore` as owner;
- required migration roles have database-level `CONNECT` on the shadow target;
- Supabase source direct-DB connectivity requirements are recorded.

After each isolated restore drill, the restore runner reapplies rehearsal
database grants for the read-only, validation, and app rehearsal roles because
the rehearsal database is dropped and recreated from the dump.

## Protected application release migrations

Application migrations for the active backend primary use the fixed workflow:

```text
Backend PostgreSQL Release Migration
```

The workflow accepts only a full app commit reachable from
`ramideltoro/nutsnews` `main`, the exact compiled migration head, a fresh
successful `Backend PostgreSQL Backup Restore Proof` run for `primary-shadow`,
and this confirmation:

```text
apply-backend-postgres-release-migrations
```

Run the backup/restore proof first and retain its run ID:

```bash
gh workflow run backend-postgres-backup-restore-proof.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=run-proof \
  -f source_database=primary-shadow \
  -f confirm_restore=prove-backend-postgres-backup-restore
```

Then run `backend-postgres-release-migration.yml` in `plan` mode. The offline
job checks main-history reachability, the app's compiled migration contract,
the continuous forward chain, and every SQL SHA-256 without receiving backend
credentials. Run `apply` with the same immutable inputs only after the plan
succeeds and while the backup proof is under two hours old.

The protected apply:

- targets only loopback PostgreSQL database `nutsnews_primary_shadow`;
- accepts only migrations listed in
  `config/backend-postgres-release-migrations.json` with exact hashes;
- validates the existing schema contract and every required PostgreSQL role
  before mutation;
- applies all remaining files in one transaction under the fixed
  `nutsnews:backend-release-migration` advisory lock;
- verifies head, legacy marker, and matching fingerprints after commit;
- retains only safe metadata, including a pre-schema SHA-256. Raw schema SQL,
  database dumps, credentials, and row data are not uploaded.

The baseline provisions `authenticator` as a deliberately inert compatibility
role: `NOLOGIN`, no superuser, no database/role creation, no inheritance, no
replication, and no RLS bypass. It receives no database, schema, table,
sequence, or function grants. This lets the exact shared migration SQL retain
its `ALTER ROLE authenticator` statement without turning the backend into a
PostgREST deployment or silently rewriting the migration.

If SQL fails, PostgreSQL rolls back the transaction. If a post-commit fault is
found, prefer a reviewed forward repair. Restore the exact encrypted logical
backup identified by the approved proof run only through the protected restore
path; do not run an ad hoc reverse migration or switch production ownership to
Supabase as a release shortcut.

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
