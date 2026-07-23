# Worker-Uplift RabbitMQ Provisioning

This runbook covers tracking issue `ramideltoro/nutsnews-worker#80`.

The backend-owned provisioning source is:

```text
ansible/roles/backend_rabbitmq
```

Validate it with:

```bash
python3 scripts/validate_worker_uplift_rabbitmq_provisioning.py
```

## Scope

This provisions RabbitMQ as a persistent Docker Compose service on
`backend.nutsnews.com` through the protected backend Ansible workflow. It does
not change the active legacy Cloudflare Worker code, schedules, bindings,
secrets, or deployment.

The queue type, digest, resource limits, and access boundary come from:

```text
docs/worker-uplift-rabbitmq-capacity-security-decision.json
```

## Protected Apply

Check mode:

```bash
gh workflow run protected-backend-ansible-apply.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=check
```

Apply mode:

```bash
gh workflow run protected-backend-ansible-apply.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=apply \
  -f confirm_apply=backend.nutsnews.com
```

Apply mode requires `production-backend` approval.

Required RabbitMQ Environment names:

| Name | Type |
| --- | --- |
| `NUTSNEWS_BACKEND_RABBITMQ_ENABLED` | variable |
| `RABBITMQ_VHOST` | variable |
| `RABBITMQ_BREAK_GLASS_ADMIN_USERNAME` | variable |
| `RABBITMQ_ERLANG_COOKIE` | secret |
| `RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD` | secret |

Secret values are written only to the root-owned host environment file used by
Compose. They are not committed, printed in Ansible output, passed as process
arguments, or uploaded as workflow artifacts.

The protected apply path pulls the pinned RabbitMQ image before starting or
restarting the service. The systemd unit uses the already-pulled image so a host
restart does not depend on registry availability.

The broker data mount is `/var/lib/nutsnews/rabbitmq`. The role recursively
repairs this tree to the RabbitMQ container UID/GID before runtime probes, so
queue files remain writable after restores, partial applies, or ownership drift.
Root-run probe state lives outside the broker mount in
`/var/lib/nutsnews/rabbitmq-probes`.

## Verification

The role runs a durable probe when RabbitMQ config, Compose, unit, environment,
broker data ownership, legacy probe state, or probe code changes:

1. Publish a persistent message to `worker.uplift.probe.durable`.
2. Restart `nutsnews-rabbitmq.service`.
3. Verify and ack the same message through the loopback management API.

The probe reads credentials from `/etc/nutsnews-rabbitmq/rabbitmq.env`; do not
pass passwords on the command line.

Read-only health command:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe health --env /etc/nutsnews-rabbitmq/rabbitmq.env'
```

Host restart durable probe:

Use the repo-managed `Backend Controlled Maintenance` workflow with
`action=reboot` and `confirm_target=backend.nutsnews.com`. When RabbitMQ is
healthy before reboot, the workflow publishes a durable probe message, reboots
the host, verifies and deletes the probe after SSH returns, and records the
result in the `backend-controlled-maintenance-report` artifact.

Manual read-only verification after an approved host restart:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'systemctl is-active docker.service nutsnews-rabbitmq.service'
```

## Rollback

Rollback is a git revert of the RabbitMQ role/config change followed by the
protected backend workflow in check mode, then apply mode after
`production-backend` approval. The persistent data directory is
`/var/lib/nutsnews/rabbitmq`; do not delete it during rollback unless a reviewed
recovery issue explicitly approves data removal.

Broker queue contents are not the only recovery source. If broker state is lost,
restore or recreate topology, then replay pending work from PostgreSQL
outbox/reconciliation state.
