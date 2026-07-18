# New Relic Observability Demo

Use this walkthrough to review the backend New Relic setup with another
engineer. Live dashboard URLs come from
`scripts/provision_newrelic_dashboards.py` after credentials are available.

## Setup

1. Run `python3 scripts/validate_newrelic_observability.py`.
2. Run `python3 scripts/provision_newrelic_dashboards.py --check`.
3. Run `python3 scripts/backend_newrelic_observability_check.py --offline`.
4. If live New Relic credentials are available, run the same validation with
   `--enforce` after the protected backend apply path.

## Latency Walkthrough

1. Open `backend-executive-service-overview`.
2. Check request rate, latency percentiles, and Apdex.
3. Open `backend-transaction-slow-path`.
4. Compare application, database, and external time.
5. Drill into `backend-trace-diagnostics` for slow spans.

## Error Walkthrough

1. Open `backend-error-rate-exception-diagnostics`.
2. Compare transaction error rate, exception classes, and failing transactions.
3. Open `backend-logs-diagnostics`.
4. Pivot by `request.id` or `trace.id` when present.
5. Use Errors Inbox after live New Relic account configuration is complete.

## Database Walkthrough

1. Open `backend-postgresql-health`.
2. Check connections, transaction rate, cache behavior, deadlocks, and
   checkpoint pressure.
3. Open `backend-apm-postgresql-correlation` for app/database latency split.
4. Open `backend-postgresql-query-performance` for slow query IDs and plan
   signals.
5. Open `backend-postgresql-operations` for locks, blocking, vacuum, bloat, and
   optional replication lag.

## Host Walkthrough

1. Open `backend-host-infrastructure`.
2. Check CPU, load, memory, disk, network, and top processes.
3. Open `backend-systemd-service-health`.
4. Drill into `backend-php-fpm-runtime-pool-health` when request latency and
   PHP-FPM saturation move together.

## Log Walkthrough

1. Open `backend-logs-diagnostics`.
2. Review warning and error volume by level.
3. Use route, status, deployment, host, unit, and exception facets.
4. Open `backend-data-ingest-free-tier-usage` to confirm log volume stays
   within budget.

## Release Walkthrough

1. Open `backend-release-health-regression`.
2. Confirm recent deployment markers.
3. Compare latency, throughput, errors, and logs against the prior day.
4. Use rollback signals before deciding whether to revert or continue
   investigation.

## Production Readiness Checklist

- Dashboard definitions validate locally and in Backend Checks.
- Dashboard provisioning check mode is deterministic and credential-free.
- New Relic credentials are stored outside git.
- PHP extension, app configuration, and distributed tracing are verified by
  `backend_newrelic_observability_check.py --enforce`.
- Logs follow `docs/newrelic-log-policy.json`.
- SLO and Apdex targets follow `docs/newrelic-service-levels.json`.
- Privacy review allowlist and denylist are current.
- Cache/queue dashboard absence matches `docs/newrelic-cache-queue-decision.json`.
- Free-tier ingest dashboard shows current month and 24 hour run rate.
- Alerts, synthetics, notification workflows, workload grouping, Errors Inbox,
  and live drop filters are configured only after New Relic account access is
  available.

## Known Gaps

- Live dashboard URLs and screenshots are unavailable until New Relic
  credentials are present.
- Live SLO, Apdex, alert, synthetics, workload, Errors Inbox, deployment marker,
  and log drop-filter mutations require account access.
- Real trace existence requires backend request traffic after the PHP agent is
  installed and configured.
- App and worker custom instrumentation belongs in the app or worker repos when
  those runtime surfaces are changed.
