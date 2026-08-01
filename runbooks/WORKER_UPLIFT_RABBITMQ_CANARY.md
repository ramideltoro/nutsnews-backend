# Worker-Uplift RabbitMQ Canary

This runbook covers `ramideltoro/nutsnews-worker#91`.

## Scope

The private canary proves the loopback AMQP path before worker-uplift traffic
depends on it. It uses the existing `RABBITMQ_MONITORING_USERNAME` identity as
a least-privilege canary principal. That identity can only write the dedicated
`worker.uplift.canary.exchange.v4` exchange and dedicated
`worker.uplift.canary.queue.v4.<uuid>` runtime queues. It can configure only
UUID-suffixed queues under that prefix, can read only that exchange and those
queues, and cannot publish to or consume from production worker queues.

The canary does not use Grafana Cloud Synthetic Monitoring for private AMQP and
does not expose a public RabbitMQ listener.

## Runtime

Protected backend apply installs:

```text
/etc/systemd/system/nutsnews-rabbitmq-canary.service
/etc/systemd/system/nutsnews-rabbitmq-canary.timer
```

The service runs:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe canary \
  --env /etc/nutsnews-rabbitmq/rabbitmq.env \
  --credentials-env /etc/nutsnews-rabbitmq/topology.env \
  --definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json \
  --output /var/lib/nutsnews/rabbitmq-probes/last-canary.json \
  --metrics-output /var/lib/nutsnews/metrics/rabbitmq-canary.prom
```

The AMQP canary publishes one persistent JSON probe message with publisher
confirms enabled, consumes it from the canary queue, validates the generated
message id, manually acks it, and records latency and message age. Credentials
and message bodies are not emitted.

## Evidence

Machine-readable state:

```text
/var/lib/nutsnews/rabbitmq-probes/last-canary.json
/var/lib/nutsnews/rabbitmq-probes/last-canary-drill.json
/var/lib/nutsnews/metrics/rabbitmq-canary.prom
```

Prometheus textfile metrics:

```text
nutsnews_backend_rabbitmq_canary_success
nutsnews_backend_rabbitmq_canary_status
nutsnews_backend_rabbitmq_canary_failure_fixture
nutsnews_backend_rabbitmq_canary_cleanup_success
nutsnews_backend_rabbitmq_canary_last_run_timestamp_seconds
nutsnews_backend_rabbitmq_canary_latency_seconds
nutsnews_backend_rabbitmq_canary_message_age_seconds
```

The metrics use only bounded labels: `status`, `failure_mode`, and
`failure_class`.

## Workflow

Use the fixed `Backend RabbitMQ Canary` workflow:

```bash
gh workflow run backend-rabbitmq-canary.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=status
```

Run one live canary:

```bash
gh workflow run backend-rabbitmq-canary.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=canary \
  -f confirm_target=backend.nutsnews.com
```

Run a fixed drill:

```bash
gh workflow run backend-rabbitmq-canary.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=drill \
  -f drill=invalid-credentials \
  -f confirm_target=backend.nutsnews.com
```

Allowed drills are:

```text
restart
consumer-loss
network-interruption
disk-watermark
invalid-credentials
unroutable
full-queue
poison-message
grafana-connectivity-loss
```

`restart` performs a broker restart through the fixed probe helper. The other
drills either exercise the canary route with deliberately broken AMQP behavior
or emit a bounded failure fixture for alert tests without changing host disk
state or disabling Grafana Alloy remote write.

## Failure Procedures

Broker down:
Check `systemctl status nutsnews-rabbitmq.service`, Docker container state, and
`/var/lib/nutsnews/rabbitmq-probes/last-canary.json`. Recover through protected
apply or the RabbitMQ recovery runbook; do not expose AMQP publicly.

Disk or memory alarm:
Use the RabbitMQ dashboards and `rabbitmq-diagnostics alarms`. Free disk or
memory pressure at the host level before replaying queues. The canary
`disk-watermark` drill emits the alert fixture without forcing real disk
pressure.

Queue backlog or no consumers:
Inspect the canary metrics, RabbitMQ queue metrics, and worker service state.
Do not purge production queues. For canary backlog, run the canary once to
consume the dedicated probe queue.
The protected apply canary has three bounded attempts with a short delay so a
single transient AMQP or management queue timeout does not fail an otherwise
healthy apply; all attempts must still succeed before the workflow can pass.
The scheduled workflow supplies a source-controlled `consumer-loss` drill
selector fallback because `workflow_dispatch` input defaults are absent on
schedule events. The scheduled action remains `canary` and does not execute a
drill; the fallback only keeps the shared fixed-input validator from rejecting
the scheduled probe before SSH.
Protected apply ensures only the dedicated durable canary exchange immediately
before the canary. The canary then declares a per-run exclusive auto-delete queue
under the `worker.uplift.canary.queue.v4.<uuid>` prefix at runtime, binds it to
the canary exchange, drains a bounded number of stale canary messages over AMQP,
and publishes its own transient probe message. It never deletes or recreates
production worker queues.
Before topology or canary work, protected apply also repairs RabbitMQ broker data
ownership/mode metadata from inside the running container. That repair is scoped
to `/var/lib/rabbitmq` and does not publish, consume, purge, or delete production
worker queue messages.

Poison message or DLQ replay:
Use the production DLQ replay procedure only after preserving the DLQ evidence.
The canary `poison-message` drill validates alerting behavior on the isolated
canary route and acks its fixture message during cleanup.

Credential rotation:
Rotate the `RABBITMQ_MONITORING_PASSWORD` secret and run protected check/apply.
The topology bootstrap updates the RabbitMQ user without printing the value.

Backup/rebuild or failed upgrade:
Use `runbooks/WORKER_UPLIFT_RABBITMQ_RECOVERY.md`. The canary should pass after
definition import, clean rebuild, stopped-volume restore, or version upgrade.

Grafana connectivity:
Do not disable Alloy to test alert wiring. Use the
`grafana-connectivity-loss` drill fixture for alert tests and separately check
Alloy remote-write health in Grafana.
