# Worker-Uplift RabbitMQ Capacity And Security Decision

This runbook covers tracking issue `ramideltoro/nutsnews-worker#79`.

Issue #79 approved the RabbitMQ capacity and security envelope; it did not
provision the broker by itself. Later protected backend bootstrap issues may
move Docker Engine, Docker Compose, and RabbitMQ out of the service baseline's
`not_deployed` list only when the listeners stay loopback-only and this
decision remains the governing capacity record.

The machine-readable source of truth is:

```text
docs/worker-uplift-rabbitmq-capacity-security-decision.json
```

Validate it with:

```bash
python3 scripts/validate_worker_uplift_rabbitmq_capacity_security.py
```

Run the disposable broker benchmark with the `Backend Checks` workflow
`rabbitmq_benchmark` job, or locally against a disposable broker with:

```bash
python3 scripts/worker_uplift_rabbitmq_capacity_benchmark.py --messages-per-queue 50 --message-bytes 1024 --output rabbitmq-capacity-benchmark.json
```

## Decision

Use RabbitMQ `4.3.3-management-alpine`, pinned by the linux/amd64 digest
recorded in the decision JSON. The initial worker-uplift topology uses durable
classic queues with publisher confirms and application-confirmed retry/DLQ
transfer.

A single backend RabbitMQ node is durable transport only. It is not high
availability, and single-replica quorum queues do not change that. PostgreSQL
stage inbox, outbox, attempt, reconciliation, and watermark tables remain the
authoritative recovery source.

## Hard Limits

Initial bootstrap must keep these limits unless this decision is revised:

| Area | Limit |
| --- | --- |
| Broker memory | 1 GiB container cap, 512 MiB RabbitMQ memory watermark |
| Broker disk | 20 GiB absolute free-disk alarm |
| File descriptors | `65536` soft and hard nofile |
| Queue count | 7 main queues, 21 retry queues, 7 DLQs |
| Main queues | `x-max-length=2000`, `x-max-length-bytes=268435456` |
| Retry queues | `x-max-length=1000`, `x-max-length-bytes=134217728` |
| DLQs | `x-max-length=2000`, `x-max-length-bytes=268435456`, 14 day review window |
| Overflow | `reject-publish`; do not use `drop-head` |
| Message body | 64 KiB hard maximum, ID-only payloads |
| Flow | heartbeat 30s, prefetch 10, 128 in-flight confirms per channel |

Resource pressure must block or nack publishers. It must not silently discard
oldest work.

## Access Boundary

Do not expose AMQP, management, or Prometheus ports publicly.

- AMQP `5672`: private Docker network only unless a later protected maintenance
  workflow approves loopback binding.
- Management `15672`: loopback or private maintenance network only, accessed
  through SSH tunnel or protected workflow.
- Prometheus `15692`: loopback only for backend Alloy scrape.

Worker services use the route-scoped identities from
`docs/worker-uplift-runtime-identities.json`. They must not receive break-glass
admin credentials. The default `guest` user must be deleted or disabled for the
worker vhost during bootstrap.

## Recovery

Broker definitions may be exported after topology changes, but queue contents
are not the only backup. If the broker is lost:

1. Pause scheduler and worker producers.
2. Recreate vhost, exchanges, policies, queues, users, and permissions from
   backend source control.
3. Start consumers in drain-safe mode.
4. Replay pending stage outbox rows with publisher confirms.
5. Run reconciliation watermarks and shadow parity checks before production
   writes resume.

Move to managed RabbitMQ or a reviewed three-node design before any requirement
needs broker availability through loss of the backend VPS, or when the measured
queue, memory, disk, connection, or recovery triggers in the decision JSON are
met.
