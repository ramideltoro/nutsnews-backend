# Worker-Uplift RabbitMQ Metrics

This runbook covers tracking issue `ramideltoro/nutsnews-worker#87`.

## Scope

The backend is the telemetry producer for RabbitMQ metrics. Grafana resources
remain owned by `ramideltoro/nutsnews-infra`; this repo does not create, edit,
or delete Grafana folders, dashboards, alerts, synthetics, contact points, or
quota guardrails.

## Collection Path

RabbitMQ exposes `rabbitmq_prometheus` on the loopback-only listener
`127.0.0.1:15692`. Backend Grafana Alloy scrapes that private endpoint and
remote-writes to Grafana Cloud Prometheus using only telemetry write
credentials.

The backend Alloy config uses two RabbitMQ scrape jobs:

- `/metrics` for aggregate broker, node, connection/channel, alarm, resource,
  uptime, and scrape-health metrics;
- `/metrics/detailed` for bounded per-queue metrics.

The detailed endpoint is requested with only the approved queue families:

```text
queue_coarse_metrics
queue_consumer_count
queue_delivery_metrics
queue_exchange_metrics
```

The request includes the worker-uplift vhost and a source-controlled queue regex
covering the 7 main queues, 21 retry queues, and 7 terminal DLQs.

## Cardinality Guardrails

Approved metric labels are bounded to:

```text
environment, host, instance, job, service_namespace, rabbitmq_endpoint, node,
cluster, vhost, queue, exchange
```

Article, feed, message, idempotency, trace, span, correlation, causation,
payload, URL, path, user, IP, token, secret, connection, and channel identifiers
must not be metric labels.

Alloy sets a RabbitMQ scrape sample limit and label limit, and relabeling keeps
only the approved RabbitMQ metric names and declared queues. Alloy self metrics
are scraped so scrape failures, remote-write pressure, dropped samples, and
pipeline health are observable.

## Verification

Use the fixed read-only workflow, **Backend RabbitMQ Metrics Check**:

```bash
gh workflow run backend-rabbitmq-metrics-check.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f require_grafana_data=false
```

Set `require_grafana_data=true` after a protected apply when enough scrape time
has passed for Grafana Cloud Prometheus to receive fresh RabbitMQ samples.

Local validators:

```bash
python3 scripts/validate_worker_uplift_rabbitmq_metrics.py
python3 -m unittest tests.test_backend_metrics
```

After protected apply, verify:

```bash
curl -fsS http://127.0.0.1:15692/metrics >/tmp/rabbitmq-aggregate.prom
curl -fsS 'http://127.0.0.1:15692/metrics/detailed?family=queue_coarse_metrics&family=queue_consumer_count&family=queue_delivery_metrics&family=queue_exchange_metrics&vhost=nutsnews-worker-uplift&queue=^nutsnews\\.worker\\.' >/tmp/rabbitmq-detailed.prom
sudo -n alloy validate /etc/alloy/config.alloy
systemctl is-active alloy
```

Grafana Cloud verification should query the existing Prometheus datasource for:

```promql
up{job=~"nutsnews-rabbitmq|nutsnews-rabbitmq-queues", environment="production"}
scrape_samples_post_metric_relabeling{job=~"nutsnews-rabbitmq|nutsnews-rabbitmq-queues"}
rabbitmq_detailed_queue_messages{queue=~"nutsnews\\.worker\\..+"}
rabbitmq_detailed_queue_consumers{queue=~"nutsnews\\.worker\\..+"}
rabbitmq_detailed_queue_exchange_messages_published_total{queue=~"nutsnews\\.worker\\..+"}
```

The #91 private AMQP canary writes low-cardinality textfile metrics into
`/var/lib/nutsnews/metrics/rabbitmq-canary.prom`. Query these in Grafana Cloud
with the backend host Prometheus datasource:

```promql
nutsnews_backend_rabbitmq_canary_success
nutsnews_backend_rabbitmq_canary_latency_seconds
nutsnews_backend_rabbitmq_canary_message_age_seconds
nutsnews_backend_rabbitmq_canary_failure_fixture
```

Measured active-series and ingestion usage must stay within the #144 approved
budget and live `grafanacloud_*_usage` / `grafanacloud_*_limits` guardrails.

## Rollback

If RabbitMQ metrics exceed privacy or budget limits, set
`backend_metrics_rabbitmq_enabled=false` through a reviewed PR and protected
apply. This removes the RabbitMQ scrape jobs from Alloy without touching
RabbitMQ, worker services, or Grafana resources.
