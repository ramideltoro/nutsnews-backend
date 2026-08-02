# Worker-Uplift Logs And Deferred Traces

This runbook covers tracking issue `ramideltoro/nutsnews-worker#88`.

## Scope

The backend is the telemetry producer for RabbitMQ and worker-uplift runtime
logs. Grafana folders, dashboards, data links, alert rules, quota guardrails,
and trace resources remain owned by `ramideltoro/nutsnews-infra`.

The approved #144 policy requires structured logs, sends them to Grafana Cloud Loki,
and states that traces are deferred. The backend must not configure a
Tempo exporter, OTLP receiver, trace credential, or exemplar path until a later
reviewed policy explicitly enables it.

## Collection Path

RabbitMQ and worker-uplift service containers use the Docker `journald` logging
driver. Each container emits a bounded `CONTAINER_TAG`:

```text
nutsnews-worker-uplift-rabbitmq
nutsnews-worker-uplift-scheduler
nutsnews-worker-uplift-fetcher
nutsnews-worker-uplift-canonicalizer
nutsnews-worker-uplift-enrichment
nutsnews-worker-uplift-approval
nutsnews-worker-uplift-translation
nutsnews-worker-uplift-persistence
nutsnews-worker-uplift-publication
```

Alloy reads those journal entries through `loki.source.journal`, not through the
Docker socket. The package-managed `alloy` user remains limited to
`systemd-journal` and `adm` group read access.

## Labels And Fields

Allowed Loki stream labels for worker-uplift logs are bounded:

```text
deployment_environment, service, service_version, host, source, severity
```

Validated correlation fields remain structured metadata, never stream labels:

```text
requestId, correlationId, causationId, messageId, idempotencyKey, traceparent,
articleId, feedId, pipelineRunId, queue, outcome, revision, imageDigest
```

Alloy promotes only full-match producer-contracted identifier shapes into
structured metadata and applies explicit byte bounds before attachment.
Malformed, oversized, or opaque candidates have no fallback metadata path.
`trace_id` is derived only from a validated W3C `traceparent`. Opaque
`tracestate` is redacted from the shipped line and is not retained as
structured metadata.

Do not promote article, feed, message, idempotency, trace, span, correlation,
causation, payload, URL, path, user, IP, token, secret, prompt, or model-output
values into Loki stream labels.

## Redaction And Volume

Before Loki export, Alloy:

- drops private-key marker lines;
- drops JSON `debug` and `trace` log levels in production;
- drops log lines larger than 8 KiB;
- redacts authorization headers, cookies, tokens, passwords, API keys, query
  strings, email addresses, and `tracestate`;
- truncates retained long lines to 4 KiB;
- caps Loki streams with `backend_logs_loki_max_streams`.

Article bodies, summaries, prompts, model outputs, raw HTML, raw feed XML,
database URLs, and service-role tokens must not be logged.

## Verification

Use the protected read-only workflow, `Backend Worker-Uplift Logs Check`:

```bash
gh workflow run backend-worker-uplift-logs-check.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f require_loki_data=false
```

After protected apply, require fresh RabbitMQ container logs in Grafana Cloud
Loki:

```bash
gh workflow run backend-worker-uplift-logs-check.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f require_loki_data=true
```

The report contains safe metadata only: local Alloy status/config status,
container source count, trace-export-disabled proof, recent RabbitMQ journal log
count, and Loki result counts. It must not print log lines, credentials,
headers, article content, prompts, or provider responses.

When `require_loki_data=true`, the check requires a recent stream for every
one of the eight worker services as well as RabbitMQ. Shadow mode changes alert
ownership, not the log-source inventory.

## Rollback

If log volume or privacy guardrails fail, use a reviewed desired-state change
and the explicit production-Alloy disable confirmation, or revert the backend
PR and run protected check/apply. Do not remove or withhold protected
credentials: missing write credentials fail apply closed. This does not mutate
queues, worker state, RabbitMQ data, or Grafana resources.
