# Worker-Uplift RabbitMQ Recovery

This runbook covers tracking issue `ramideltoro/nutsnews-worker#83`.

Validate the implementation with:

```bash
python3 scripts/validate_worker_uplift_rabbitmq_recovery.py
```

## Policy

RabbitMQ is durable transport for worker-uplift, not the system of record.
Authoritative stage state, outbox rows, and reconciliation data live in backend
PostgreSQL. If broker state is lost, the normal recovery path is:

1. deploy the pinned RabbitMQ image and reviewed config;
2. bootstrap topology from source control;
3. provision protected credentials from the `production-backend` environment;
4. replay or reconcile pending work from PostgreSQL outbox/reconciliation state.

Do not hot-copy the live `/var/lib/nutsnews/rabbitmq` message store. RabbitMQ
documents that running-node message-store copies can be inconsistent. Normal
Restic jobs therefore exclude the live broker data directory and include only
non-secret config plus `/var/lib/nutsnews/rabbitmq-recovery` evidence.

References:

- https://www.rabbitmq.com/docs/backup
- https://www.rabbitmq.com/docs/definitions
- https://www.rabbitmq.com/docs/upgrade
- https://www.rabbitmq.com/docs/rolling-upgrade

## Definition Exports

Use the host helper:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery export-definitions
```

The helper runs `rabbitmqctl export_definitions` against the live broker,
copies the raw JSON to a temporary root-only path, redacts `password_hash`,
`password_hashing_algorithm`, `password`, and `*_hash` fields, then writes:

```text
/var/lib/nutsnews/rabbitmq-recovery/definitions.sanitized.json
/var/lib/nutsnews/rabbitmq-recovery/last-definition-export.json
```

Raw exports and password hashes are not retained, committed, or uploaded as
workflow artifacts.

## Recovery Drills

Clean empty-broker rebuild drill:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery clean-rebuild-drill
```

This starts a disposable broker with throwaway credentials, bootstraps the
source-controlled topology, verifies drift and least-privilege permissions, and
runs retry/DLQ transfer probes. It proves the preferred rebuild path without
touching production queue contents.

Stopped-volume restore drill:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery stopped-volume-restore-drill
```

This drill uses disposable broker data only. It stops the source drill broker,
copies the stopped data directory, restarts a second drill broker with the same node name and same Erlang cookie, then verifies topology and permissions.

The stopped-volume path is supported only when a real incident snapshot is
taken after the broker is stopped or otherwise quiesced. Preserve the node name,
data directory layout, and Erlang cookie. Encrypt any retained snapshot
off-host. The RPO gap is every message published after that snapshot; replay
that gap from PostgreSQL outbox/reconciliation state.

## Workflow

Use the fixed `Backend RabbitMQ Recovery` workflow:

```bash
gh workflow run backend-rabbitmq-recovery.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=export-definitions \
  -f confirm_target=backend.nutsnews.com
```

Allowed actions:

- `status`
- `export-definitions`
- `clean-rebuild-drill`
- `stopped-volume-restore-drill`
- `scheduled-check`

Mutating actions require `confirm_target=backend.nutsnews.com` and
`production-backend` approval. The weekly scheduled action runs
`scheduled-check`, which exports sanitized definitions and runs the clean
rebuild drill. Run the stopped-volume drill separately after material broker
changes or before a cutover gate that requires message-store restore evidence.

The workflow uploads status JSON only:

```text
backend-rabbitmq-recovery-report.json
backend-rabbitmq-recovery-status.json
```

It does not upload raw definitions, sanitized definitions, broker data,
password hashes, or credential files.

## Upgrade Procedure

Forward upgrades only:

1. Review RabbitMQ and Erlang compatibility for the current and target versions.
2. Review release notes and feature-flag requirements.
3. Run `export-definitions`, `clean-rebuild-drill`, and
   `stopped-volume-restore-drill` before the maintenance window.
4. Confirm backend PostgreSQL outbox/reconciliation health.
5. Update the pinned image digest in a reviewed PR.
6. Run protected Ansible check mode, then apply mode after approval.
7. Verify broker health, topology drift, permissions, transfer probe, network
   security, and recovery status.
8. Enable stable feature flags only after the upgraded broker is healthy.

Unsupported downgrade promises are not made. If an upgrade fails after data has
been written by the new broker version, prefer forward recovery using a fixed
target image, sanitized definitions, topology bootstrap, protected credentials,
and PostgreSQL replay.

## Health Signals

RabbitMQ recovery evidence is exposed in:

- `/usr/local/sbin/nutsnews-backup status` under `rabbitmq_recovery`;
- the recurring backend health report;
- textfile metrics as `nutsnews_backend_rabbitmq_recovery_stage_healthy` and
  `nutsnews_backend_rabbitmq_definition_export_age_seconds`;
- the loopback-only ops dashboard status snapshot.

Healthy readiness for broker recovery requires a fresh sanitized definition
export plus passing clean rebuild drill. Stopped-volume evidence is required
only when choosing the constrained message-store restore path.
