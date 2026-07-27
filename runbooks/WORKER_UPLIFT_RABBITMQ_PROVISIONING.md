# Worker-Uplift RabbitMQ Provisioning

This runbook covers tracking issues `ramideltoro/nutsnews-worker#80`,
`ramideltoro/nutsnews-worker#81`, `ramideltoro/nutsnews-worker#82`, and
`ramideltoro/nutsnews-worker#83`, `ramideltoro/nutsnews-worker#84`, and
`ramideltoro/nutsnews-worker#91`.

The backend-owned provisioning source is:

```text
ansible/roles/backend_rabbitmq
```

The reviewed, non-secret topology definition is rendered from:

```text
ansible/roles/backend_rabbitmq/templates/worker-uplift-topology.json.j2
```

Validate it with:

```bash
python3 scripts/validate_worker_uplift_rabbitmq_provisioning.py
python3 scripts/validate_worker_uplift_rabbitmq_recovery.py
python3 scripts/validate_worker_uplift_rabbitmq_operations.py
python3 scripts/validate_worker_uplift_rabbitmq_metrics.py
```

## Scope

This provisions RabbitMQ as a persistent Docker Compose service on
`backend.nutsnews.com` through the protected backend Ansible workflow. It also
bootstraps the worker-uplift vhost, exchanges, durable classic queues, retry
queues, DLQs, policies, route-scoped users, and permissions from source control.
It also creates the isolated `worker.uplift.canary.exchange.v4` exchange used
by the private AMQP canary.
It does not change the active legacy Cloudflare Worker code, schedules,
bindings, secrets, or deployment.

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

The protected apply runs RabbitMQ credential readiness by secret name before
Ansible builds runtime vars. It uploads
`backend-rabbitmq-credential-readiness.json` with the deployment safety reports.
Check mode remains non-mutating. Apply mode requires the explicit
`confirm_apply=backend.nutsnews.com` value, writes
`/var/lib/nutsnews/rabbitmq-probes/apply-metadata.json`, and records rollback
metadata for a git revert followed by protected check/apply.

Required RabbitMQ Environment names:

| Name | Type |
| --- | --- |
| `NUTSNEWS_BACKEND_RABBITMQ_ENABLED` | variable |
| `RABBITMQ_VHOST` | variable |
| `RABBITMQ_BREAK_GLASS_ADMIN_USERNAME` | variable |
| `RABBITMQ_ERLANG_COOKIE` | secret |
| `RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD` | secret |
| `RABBITMQ_MONITORING_USERNAME` | variable |
| `RABBITMQ_MONITORING_PASSWORD` | secret |
| `RABBITMQ_*_CONSUMER_USERNAME` / `RABBITMQ_*_PUBLISHER_USERNAME` | variables |
| `RABBITMQ_*_CONSUMER_PASSWORD` / `RABBITMQ_*_PUBLISHER_PASSWORD` | secrets |

The route identity names and secret names are recorded in
`docs/worker-uplift-runtime-identities.json`. Break-glass admin credentials are
written to the root-owned Compose env file. Service identity credentials are
written to `/etc/nutsnews-rabbitmq/topology.env`, which is root-only and is not
mounted into the RabbitMQ container. Secret values are not committed, printed in
Ansible output, passed as process arguments, or uploaded as workflow artifacts.
The topology bootstrap deletes the default `guest` user, and the network
security check verifies that anonymous management API requests are denied.

The protected apply path pulls the pinned RabbitMQ image before starting or
restarting the service. The systemd unit uses the already-pulled image so a host
restart does not depend on registry availability.

The broker data mount is `/var/lib/nutsnews/rabbitmq`. The role first repairs
this tree from the host to the RabbitMQ container UID/GID, then repairs the same
mount from inside the running container as `root` to `rabbitmq:rabbitmq` after
RabbitMQ diagnostics pass. Both repairs are metadata-only ownership/mode
repairs scoped to `/var/lib/rabbitmq`; they do not publish, consume, purge, or
delete production worker queues. This keeps queue files writable after restores,
partial applies, user-namespace mapping differences, or ownership drift.
Root-run probe state lives outside the broker mount in
`/var/lib/nutsnews/rabbitmq-probes`.

Protected apply metadata is stored at:

```text
/var/lib/nutsnews/rabbitmq-probes/apply-metadata.json
```

It contains the pinned image reference, non-secret managed file checksums,
managed paths, and rollback metadata. It must not contain credential values or
message contents.

Recovery evidence lives outside the broker mount in
`/var/lib/nutsnews/rabbitmq-recovery`. Normal live message-store snapshots are
excluded from Restic; see `Backend RabbitMQ Recovery` and
`runbooks/WORKER_UPLIFT_RABBITMQ_RECOVERY.md`.

## Topology Bootstrap

The role installs:

```text
/usr/local/sbin/nutsnews-rabbitmq-topology
/etc/nutsnews-rabbitmq/worker-uplift-topology.json
/etc/nutsnews-rabbitmq/topology.env
```

The bootstrap command is idempotent and non-destructive. It creates missing
resources, removes the default `guest` user, removes managed users' permissions
from non-target vhosts, and reports drift instead of deleting or recreating
queues whose immutable arguments differ.

Read-only drift check:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-topology check \
  --env /etc/nutsnews-rabbitmq/rabbitmq.env \
  --credentials-env /etc/nutsnews-rabbitmq/topology.env \
  --definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json
```

Permission matrix check:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-topology permissions \
  --env /etc/nutsnews-rabbitmq/rabbitmq.env \
  --credentials-env /etc/nutsnews-rabbitmq/topology.env \
  --definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json
```

Manual `probe-transfers` runs are fail-closed and refuse to publish probes when
any stage queue is non-empty or has active consumers. Protected applies run
`probe-transfers --skip-non-empty` so a shadow backlog or live consumer does not
get mutated by a deployment check. The JSON report records `skipped_stages`,
`skipped_queues`, and `skipped_consumers` for any route skipped due to existing
messages or consumers; empty idle routes are still probed.
The persistent data ownership repair does not notify an immediate RabbitMQ
restart before this live transfer probe; ownership repairs are instead covered by
the later durable restart probe so consumers are not disconnected during the
consumer-count skip check.
For probed routes, it publishes and cleans up probe messages, verifies retry and
DLQ routing, and verifies an unroutable retry target leaves the source message
visible until a confirmed target publish succeeds.

The topology grants the monitoring identity access only to the private canary
route:

```text
exchange: worker.uplift.canary.exchange.v4
routing key: worker.uplift.canary.v4
queue: worker.uplift.canary.queue.v4 (runtime-declared exclusive auto-delete)
```

It cannot configure production resources and cannot write to or consume from
production worker queues. The exchange-scoped read permission and queue-scoped
write permission are limited to the private canary resources because RabbitMQ
checks those permissions during private canary route setup.

## Verification

Network posture is part of every protected RabbitMQ apply. The role installs
`/usr/local/sbin/nutsnews-rabbitmq-network-check` and runs it after the broker,
topology, permissions, transfer probe, and health checks pass. The check verifies:

- host listeners for AMQP `5672`, management `15672`, and Prometheus `15692` are loopback-only;
- Docker publishes those ports only on loopback;
- the RabbitMQ container is attached to a private Docker network for colocated service containers;
- UFW is active, default-deny incoming, and has no public RabbitMQ allow rules;
- loopback AMQP, management, and Prometheus endpoints are reachable from the host;
- RabbitMQ Prometheus metrics are available for local Grafana Alloy scraping;
- unauthenticated management requests are rejected;
- the `guest` user is absent;
- service identity usernames and passwords are distinct from one another and from the break-glass admin credential, without printing secret values;
- TLS is not required while all broker connections stay inside the host/Docker trust boundary.

Read-only network check:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'sudo -n /usr/local/sbin/nutsnews-rabbitmq-network-check'
```

Public exposure scan:

```bash
for port in 5672 15672 15692; do
  nc -vz -w5 65.75.201.18 "$port"
done
```

Expected result: every RabbitMQ public scan attempt is refused or times out.
The protected deployment safety gate performs the same external check from the
GitHub runner and fails post-apply if any RabbitMQ port is open.

TLS posture: AMQP, management, and Prometheus traffic is approved only on
`127.0.0.1` and the Docker-private RabbitMQ network. If a future service needs
to cross a host trust boundary, the change must add RabbitMQ TLS listeners,
certificate rotation, and a read-only certificate-expiry check before opening
that path.

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

## Protected Drift And Smoke

The `Backend Drift Check` workflow is read-only. It now includes `rabbitmq_drift`
from:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe drift \
  --env /etc/nutsnews-rabbitmq/rabbitmq.env \
  --credentials-env /etc/nutsnews-rabbitmq/topology.env \
  --definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json \
  --metadata /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json
```

That drift report checks the pinned image digest, Compose/config/topology
checksums, topology drift, policies, user/permission metadata, listeners/network
posture, health, and backup/RabbitMQ recovery freshness.

Use the fixed `Backend RabbitMQ Smoke` workflow for isolated broker smoke:

```bash
gh workflow run backend-rabbitmq-smoke.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=smoke \
  -f confirm_target=backend.nutsnews.com
```

The smoke action requires `production-backend` approval and confirmation. It
creates only `worker.uplift.probe.smoke.*` resources, verifies publish route
confirmation, consume/manual ack, retry, DLQ, persistence across a fixed
`nutsnews-rabbitmq.service` restart, and permission denial for the monitoring
identity. It cleans the probe queues/exchanges and writes:

```text
/var/lib/nutsnews/rabbitmq-probes/last-smoke.json
```

The workflow uploads `backend-rabbitmq-smoke-report.json`. Reports contain probe
resource names and generated message IDs only; credentials, message bodies,
article payloads, and broker data are not uploaded.

Use `runbooks/WORKER_UPLIFT_RABBITMQ_CANARY.md` and the fixed
`Backend RabbitMQ Canary` workflow for the continuous private AMQP canary and
controlled failure drills. The host timer writes:

```text
/var/lib/nutsnews/rabbitmq-probes/last-canary.json
/var/lib/nutsnews/metrics/rabbitmq-canary.prom
```

## Recovery

The role installs:

```text
/usr/local/sbin/nutsnews-rabbitmq-recovery
```

Use the fixed `Backend RabbitMQ Recovery` workflow for definition export,
clean-broker rebuild drill, stopped-volume restore drill, and weekly scheduled
recovery checks. Status is reported in the protected apply summary, the backup
status command, the recurring health report, textfile metrics, and the ops
dashboard.

Normal rebuild uses pinned image/config, topology bootstrap, credential
provisioning, and PostgreSQL outbox/reconciliation replay. Normal Restic jobs
include `/var/lib/nutsnews/rabbitmq-recovery`, and live message-store snapshots are excluded. A message-store restore is supported only from a stopped/quiesced
snapshot with the same node name, same data directory layout, and same Erlang
cookie.

## Management Access

The RabbitMQ management UI is not public. Use an SSH tunnel from an approved
operator workstation:

```bash
ssh -N -L 15672:127.0.0.1:15672 -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18
```

Then open:

```text
http://127.0.0.1:15672
```

Use the `RABBITMQ_BREAK_GLASS_ADMIN_USERNAME` and
`RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD` values from the protected
`production-backend` environment only for approved operations. Close the tunnel
when the operation is complete.

Emergency revocation:

```bash
sudo -n docker exec nutsnews-rabbitmq rabbitmqctl clear_permissions -p nutsnews-worker-uplift <username>
sudo -n docker exec nutsnews-rabbitmq rabbitmqctl delete_user <username>
```

After emergency revocation, rotate the affected `production-backend` secret,
run the protected apply, and confirm the topology permission and network
security checks pass. For break-glass admin compromise, revoke the admin user
from a privileged host session, rotate `RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD`,
and restore the reviewed admin identity through the protected apply path.

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
restore or recreate topology with the source-controlled bootstrap, then replay
pending work from PostgreSQL outbox/reconciliation state.
