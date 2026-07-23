# Backup And Restore Baseline

This runbook covers backend backup and restore for `65.75.201.18`.

## Current State

The backend host has no deployed app runtime, upload storage, or other
production backend app state yet. It does have host baseline, Caddy, dashboard,
backup-status state, and a private PostgreSQL restore/failover target that must
be covered.

The service-aware matrix is:

```text
docs/backend-backup-service-matrix.json
```

Backend PostgreSQL restore proof metadata example:

```text
docs/backend-postgres-backup-restore-proof.example.json
```

Validate it with:

```bash
python3 scripts/validate_backup_service_matrix.py
python3 scripts/validate_backend_postgres_backup_proof.py
```

## Policy

Backups must survive loss of the VPS.

Provider snapshots are supplemental only. They are useful for fast whole-server recovery, but they are not the only recovery mechanism for application data, database state, credentials, or operational evidence.

## Backup Scope

| Data class | Initial owner | Backup requirement |
| --- | --- | --- |
| Backend app code/config | GitHub repositories | Git remotes are source of truth; generated checkouts are not primary backup state |
| Backend app runtime env | GitHub Environment secrets or documented secret store | Secret names documented in repo; values are excluded from restic |
| Reverse proxy config/state | Caddy and this repo | Back up `/etc/caddy` and `/var/lib/caddy`; restore then reconcile with protected apply |
| Host baseline config | This repo through Ansible | Back up selected host config for evidence; protected apply remains preferred restore |
| Ops dashboard snapshots | Generated on host | Back up `status.json` for incident evidence; collectors can regenerate |
| Backup status metadata | Backup runner | Back up `/var/lib/nutsnews/backups` status JSON |
| RabbitMQ topology recovery | Backend issue #83 | Back up non-secret config and `/var/lib/nutsnews/rabbitmq-recovery`; live message-store snapshots are excluded from normal Restic jobs |
| Application uploads or local files | Future backend issue | Must use off-server backups before production use |
| PostgreSQL data | Backend issues #13 and #113 | Host state is covered by encrypted off-server restic paths; database readiness for primary use requires a backend-produced backup restore proof artifact, not only a Supabase dump restore |
| Logs | Host log retention plus future off-server policy | Back up `/var/log/nutsnews` only; avoid unbounded raw logs |

## Off-Server Target

The first approved backend backup implementation should use encrypted off-server storage. Acceptable options include:

- restic to a dedicated rclone remote;
- another encrypted backup store documented in this repo;
- managed database backups when the state is not hosted on this VPS.

Backup credentials live in the `production-backend` GitHub Environment. Do not
commit backup credentials, repository passwords, provider keys, database dumps,
or restore artifacts.

Required secret names:

- `RESTIC_REPOSITORY`
- `RESTIC_PASSWORD`
- provider credentials for `NUTSNEWS_BACKEND_RESTIC_PROVIDER`, currently
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for `s3`

The protected apply writes a root-only systemd environment file. Status JSON is
world-readable for observability, but it contains no secret values.

## Initial Retention Baseline

Until a workload-specific issue chooses different values:

| Backup type | Retention |
| --- | --- |
| Daily | 14 |
| Weekly | 8 |
| Monthly | 12 |
| Yearly | 2 |

Retention must be revisited before PostgreSQL stores production-writer data or uploads become production dependencies.

## Restore Test Gate

Before production traffic or production data depends on this backend host, run and record a restore test.

Minimum restore test:

1. Restore the latest backup to an isolated path or non-production host.
2. Verify file ownership and permissions.
3. For database backups, run `backend-postgres-failover-drill.yml` in `restore-staging` mode and verify `/var/lib/nutsnews/postgres/status.json`.
4. Confirm the restored data can satisfy the documented RPO/RTO for that workload.
5. Record the backup snapshot ID, restore target, validation commands, and result in the relevant issue or PR.

No production database cutover, upload store, or other stateful production
service should depend on this backend host until this gate is satisfied with the
relevant production-like data.

For backend PostgreSQL primary promotion, #113 remains blocked until the proof
artifact validates with:

```bash
python3 scripts/validate_backend_postgres_backup_proof.py path/to/live-proof.json
```

The live proof must identify the backend PostgreSQL restic snapshot id, isolated
restore target, measured RPO/RTO, parity validation status, backup freshness
status, restore health status, and status artifact paths. It must not include
connection strings, passwords, row data, dump paths, or tokens.

Protected proof workflow:

```bash
gh workflow run backend-postgres-backup-restore-proof.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=run-proof \
  -f confirm_restore=prove-backend-postgres-backup-restore
```

The workflow creates a backend-produced logical PostgreSQL dump from the
rehearsal database, writes that dump into the encrypted off-server restic
repository, restores the restic snapshot into the isolated
`nutsnews_backup_restore_proof` database, runs the backend PostgreSQL restore
validation SQL, and writes safe metadata to:

- `/var/lib/nutsnews/postgres/backup-restore-proof.json`
- `/var/lib/nutsnews/postgres/status.json`

Status-only inspection:

```bash
gh workflow run backend-postgres-backup-restore-proof.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=status
```

## GitOps Components

The protected Ansible baseline installs:

- `/usr/local/sbin/nutsnews-backup`
- `/etc/nutsnews-backup/service-matrix.json`
- `/etc/nutsnews-backup/restic.env` with mode `0600`
- `/var/lib/nutsnews/backups/` with mode `0755`
- `nutsnews-backup.service` and `.timer`
- `nutsnews-backup-verify.service` and `.timer`
- `nutsnews-restore-drill.service` and `.timer`

Manual backup operations use the fixed workflow:

```text
Backend Backup Maintenance
```

Allowed actions:

- `status`
- `backup`
- `verify`
- `restore-drill`

The mutating actions require `confirm_target=backend.nutsnews.com` and
`production-backend` approval.

## Status Files

The runner writes:

| File | Meaning |
| --- | --- |
| `/var/lib/nutsnews/backups/last-backup.json` | latest backup freshness, snapshot id, included paths, quota status |
| `/var/lib/nutsnews/backups/last-verification.json` | latest restic check result |
| `/var/lib/nutsnews/backups/last-restore-verification.json` | lightweight restore-drill result |

The health report and ops dashboard expose these statuses separately:

- backup failure;
- stale backup;
- unverified latest snapshot;
- storage/quota warning.

RabbitMQ recovery evidence is also surfaced through
`/usr/local/sbin/nutsnews-backup status` under `rabbitmq_recovery`. Definition
exports and drill status live in `/var/lib/nutsnews/rabbitmq-recovery`.
Restoring live RabbitMQ queue files is not part of normal Restic backup/restore;
use the worker-uplift RabbitMQ recovery runbook for the constrained
stopped/quiesced message-store path.

## Verification Commands

Current read-only host context:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && df -h / && findmnt /'
```

Future backup implementations must add service-specific commands such as:

```bash
restic snapshots
restic check
systemctl status <backup-timer-or-service> --no-pager
sudo /usr/local/sbin/nutsnews-backup status
```

## Recovery Order

1. Provision or recover the host.
2. Apply baseline configuration through the protected backend workflow.
3. Restore secrets through the documented secret store.
4. Restore stateful data from encrypted off-server backups.
5. Run workload-specific integrity checks.
6. Verify health endpoints, dashboard status, and logs.
7. Route traffic only after verification passes.

## Rollback

If a backup implementation causes failures:

- disable the backup timer or service through a reviewed repo change and protected apply;
- preserve logs and the last known-good backup snapshot;
- do not delete older backups until restore health is confirmed;
- document the incident and reconcile any manual break-glass changes.
