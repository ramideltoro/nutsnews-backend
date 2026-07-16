# Backup And Restore Baseline

This runbook covers backend issue #6 for `65.75.201.18`.

## Current State

The backend host has no deployed app runtime, database service, upload storage, or other production backend state yet.

This runbook defines the backup baseline that must be implemented before any production backend state depends on this host.

## Policy

Backups must survive loss of the VPS.

Provider snapshots are supplemental only. They are useful for fast whole-server recovery, but they are not the only recovery mechanism for application data, database state, credentials, or operational evidence.

## Backup Scope

| Data class | Initial owner | Backup requirement |
| --- | --- | --- |
| Backend app code/config | GitHub repositories | Git remotes are source of truth; do not back up generated checkouts as primary state |
| Backend app runtime env | GitHub Environment secrets or documented secret store | Secret names documented in repo; values stored outside git and backed up by the secret-store owner |
| Reverse proxy config | This repo through Ansible | Recreate from repo and protected apply |
| Host baseline config | This repo through Ansible | Recreate from repo and protected apply |
| Ops dashboard snapshots | Generated on host | Recreate from collectors unless explicitly needed for incident evidence |
| Application uploads or local files | Future backend issue | Must use off-server backups before production use |
| PostgreSQL data | Future database issue | Must use off-server encrypted backups plus restore drills before production use |
| Logs | Host log retention plus future off-server policy | Retain enough for troubleshooting without storing secrets or unbounded raw logs |

## Off-Server Target

The first approved backend backup implementation should use encrypted off-server storage. Acceptable options include:

- restic to a dedicated rclone remote;
- another encrypted backup store documented in this repo;
- managed database backups when the state is not hosted on this VPS.

Backup credentials must live in the `production-backend` GitHub Environment or a documented secret store. Do not commit backup credentials, repository passwords, rclone config, database dumps, or restore artifacts.

## Initial Retention Baseline

Until a workload-specific issue chooses different values:

| Backup type | Retention |
| --- | --- |
| Daily | 14 |
| Weekly | 8 |
| Monthly | 12 |
| Yearly | 2 |

Retention must be revisited before PostgreSQL or uploads become production dependencies.

## Restore Test Gate

Before production traffic or production data depends on this backend host, run and record a restore test.

Minimum restore test:

1. Restore the latest backup to an isolated path or non-production host.
2. Verify file ownership and permissions.
3. For database backups, start a non-production PostgreSQL instance and run integrity checks.
4. Confirm the restored data can satisfy the documented RPO/RTO for that workload.
5. Record the backup snapshot ID, restore target, validation commands, and result in the relevant issue or PR.

No backend database, upload store, or other stateful production service should be enabled until this gate is satisfied.

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
