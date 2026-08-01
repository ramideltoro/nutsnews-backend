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

The common Alloy processing boundary applies this same exact-six indexed-label
contract to system, backend-host, and container logs. It does not retain
source-specific context as additional indexed stream labels.

Worker `service_version` is read from the running container's immutable Docker
label. Git `revision` and `image_digest`, plus `queue` and `outcome`, are stored
as structured metadata rather than indexed stream labels. The expected identity
triples in the backend log defaults must exactly match the worker runtime image
pins; validation fails on promotion drift.

Alloy normalizes camelCase or snake_case application fields to these
snake_case structured-metadata keys:

```text
request_id, correlation_id, causation_id, message_id, idempotency_key,
trace_id, article_id, feed_id, pipeline_run_id, traceparent, tracestate,
queue, outcome, revision, image_digest
```

Do not promote article, feed, message, idempotency, trace, span, correlation,
causation, payload, URL, path, user, IP, token, secret, prompt, or model-output
values into Loki stream labels.

## Redaction And Volume

Before Loki export, Alloy:

- drops private-key marker lines;
- drops JSON `debug` and `trace` log levels in production;
- drops log lines larger than 8 KiB;
- redacts authorization headers, cookies, tokens, passwords, API keys, query
  strings, and email addresses;
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

After protected apply, recreate each worker container with the fixed runtime
manager `deploy` action. A plain container restart does not apply new Docker
labels or journald options. Then require fresh RabbitMQ logs and fresh logs from
all eight workers with valid `revision` and `image_digest` metadata in Grafana
Cloud Loki:

```bash
gh workflow run backend-worker-uplift-logs-check.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f require_loki_data=true
```

The report contains safe metadata only: local Alloy status/config status,
container source count, trace-export-disabled proof, recent RabbitMQ journal log
count, and per-service Loki result counts. It must not print log lines,
credentials, headers, article content, prompts, or provider responses.

Worker service Loki queries can be `not_configured` during predeployment checks.
With `require_loki_data=true`, RabbitMQ plus every worker service must be
queryable, and worker results must carry valid deployed revision and image-digest
metadata.

## Rollback

If log volume or privacy guardrails fail, disable log shipping by removing or
withholding the protected Loki credentials and rerun `Protected Backend Ansible
Apply`. To roll back the source-controlled container logging path, revert the
backend PR and run protected check/apply. This does not mutate queues, worker
state, RabbitMQ data, or Grafana resources.
