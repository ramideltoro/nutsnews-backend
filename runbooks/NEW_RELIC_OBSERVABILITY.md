# New Relic Backend Observability

Issues: #134, #135, #136, #137, #139

## Taxonomy

The canonical New Relic service name is `nutsnews-backend-production`.
Use the taxonomy in `docs/newrelic-observability-taxonomy.json` for dashboard,
alert, workload, host, database, log, and deployment naming.

Required tags:

- `service.name=nutsnews-backend-production`
- `environment=production`
- `owner=nutsnews`
- `team=solo-ops`
- `repository=ramideltoro/nutsnews-backend`
- `host.name=backend.nutsnews.com`

Dashboard names must start with `NutsNews Backend - ` and use a 24 hour default
query window unless a dashboard states a narrower operational reason.

## Credentials

Required environment variables:

- `NEW_RELIC_LICENSE_KEY`: ingest key for PHP APM, infrastructure, logs, and
  custom telemetry.
- `NEW_RELIC_USER_KEY`: NerdGraph user key for configuration and validation.
- `NEW_RELIC_ACCOUNT_ID`: New Relic account id for NRQL and dashboard CRUD.
- `NEW_RELIC_REGION`: `us`, `eu`, or `jp`; defaults to `us`.
- `NEW_RELIC_APP_NAME`: defaults to `nutsnews-backend-production`.

Never commit key values, GraphQL responses containing account data, exported
logs, or dashboard artifacts that include private host or user data. Store
runtime values only in the protected GitHub Environment or the approved local
credential store.

New Relic documents license keys as ingest keys and user keys as querying and
configuration keys. NerdGraph uses region-specific GraphQL endpoints:
`api.newrelic.com/graphql`, `api.eu.newrelic.com/graphql`, or
`api.jp.newrelic.com/graphql`.

## Provision Dashboards

Validate dashboard definitions without credentials:

```bash
python3 scripts/provision_newrelic_dashboards.py --check
```

Provision or update dashboards with NerdGraph:

```bash
NEW_RELIC_USER_KEY=... \
NEW_RELIC_ACCOUNT_ID=... \
NEW_RELIC_REGION=us \
python3 scripts/provision_newrelic_dashboards.py
```

The provisioner reads `docs/newrelic/dashboards/*.json`, searches for an
existing dashboard by name, updates it when found, and creates it otherwise.
Missing credentials fail closed before any network call.

## Dashboard Catalog

Versioned dashboard definitions currently managed by this repo:

- `backend-executive-service-overview`: high-level health overview for #135.
- `backend-error-rate-exception-diagnostics`: failure and exception triage for #138.
- `backend-transaction-slow-path`: slow request investigation for #140.
- `backend-php-apm-throughput-latency`: request volume and latency analysis for #141.
- `backend-external-dependencies`: outbound dependency diagnosis for #142.
- `backend-php-fpm-runtime-pool-health`: PHP-FPM capacity and pool saturation for #144.
- `backend-caddy-request-traffic`: Caddy request volume, status, latency, and bounded facets for #146.
- `backend-host-infrastructure`: backend host CPU, memory, disk, network, and process health for #147.
- `backend-systemd-service-health`: critical service state, restarts, and service logs for #149.
- `backend-postgresql-health`: PostgreSQL availability, connections, transaction load, cache, checkpoints, and storage for #151.
- `backend-apm-postgresql-correlation`: APM/database latency correlation and datastore spans for #152.
- `backend-postgresql-query-performance`: query monitoring, query IDs, execution time, reads, writes, and plan rows for #153.
- `backend-postgresql-operations`: locks, blocking sessions, vacuum, table bloat, storage, and optional replication metrics for #154.
- `backend-data-ingest-free-tier-usage`: account ingest usage, monthly run rate, noisy sources, and reduction actions for #157.
- `backend-logs-diagnostics`: structured log severity, route, status, deployment, exception, and trace-context views for #158.
- `backend-sli-slo`: availability, latency, error-free, freshness, burn, and SLO history for #161.
- `backend-golden-metrics-apdex`: Apdex, throughput, latency, errors, saturation, and target rationale for #175.
- `backend-anomaly-baseline`: current-versus-baseline throughput, latency, errors, and log-volume views for #173.
- `backend-release-health-regression`: deploy markers, before/after health, new exceptions, logs, and rollback signals for #166.
- `backend-trace-diagnostics`: trace count, slow traces, error traces, database spans, outbound spans, and log correlation for #148.

Each dashboard keeps a 24 hour default query window and uses the
`NutsNews Backend - ` naming prefix. Live dashboard URLs are printed by
`scripts/provision_newrelic_dashboards.py` after New Relic credentials are
available in the runtime environment.

## Runtime And Host Data Sources

The runtime and host dashboards expect low-cardinality New Relic data from:

- PHP-FPM pool metrics under `php_fpm.*` or an equivalent custom metric source.
- Caddy structured logs with parsed `http.statusCode`, `request.path`,
  `request.method`, `request.durationMs`, and `userAgent.name` fields.
- New Relic infrastructure samples for host, storage, network, and process
  data.
- Systemd service state metrics under `systemd.service.*` or equivalent
  service-health events, plus service-scoped logs.

This dashboard pack does not expose a public PHP-FPM status endpoint and does
not change Caddy routing. If a PHP-FPM status endpoint is later approved, keep
it local-only or otherwise access-controlled and update the dashboard queries to
match the approved metric names.

## PostgreSQL Data Sources

The PostgreSQL dashboards expect the New Relic on-host PostgreSQL integration
and use these event types:

- `PostgresqlDatabaseSample` for connections, transactions, cache, deadlocks,
  conflicts, and database-level I/O.
- `PostgresqlInstanceSample` for checkpoint and background writer behavior.
- `PostgresqlTableSample` for table size, table bloat, dead rows, and vacuum
  signals.
- `PostgresSlowQueries`, `PostgresIndividualQueries`,
  `PostgresWaitEvents`, `PostgresBlockingSessions`, and
  `PostgresExecutionPlanMetrics` when query monitoring is enabled.

For table and index metrics, the New Relic PostgreSQL role needs `SELECT` on
the collected tables. For query-level metrics, enable `pg_stat_statements` and
grant `pg_read_all_stats` to the integration role. Dashboard widgets avoid raw
SQL display and facet query diagnostics by query id, statement type, database,
schema, wait category, or plan operation.

## Log Policy And Ingest Guardrails

The structured logging policy lives in `docs/newrelic-log-policy.json`.
It defines required parsed fields, redaction rules, retained categories,
dropped categories, and expected daily log ingest volume for the free-tier
budget. The logs diagnostics dashboard uses parsed fields such as
`request.id`, `trace.id`, `route`, `http.statusCode`, `duration.ms`,
`deployment.version`, `exception.class`, and `message.safe`.

Live New Relic parsing, obfuscation, and drop-filter rules require New Relic
account access and may depend on account features. Until those credentials are
available, the repo policy and dashboard queries are the source of truth for the
intended implementation.

## Service Quality And Release Triage

The service-level and golden-metric policy lives in
`docs/newrelic-service-levels.json`. Initial production targets:

- Availability SLO: 99.5 percent successful backend requests over 30 days.
- Latency SLO: 95 percent of valid requests at or below 750 ms over 30 days.
- Error-free SLO: 99.5 percent non-error requests over 30 days.
- Freshness SLO: 99 percent of feed refresh samples at or below 15 minutes over
  30 days.
- Apdex target: 0.5 seconds for the backend API.

Common triage flow:

1. Start in `backend-executive-service-overview` for broad health.
2. Use `backend-sli-slo` for reliability target status and burn.
3. Use `backend-golden-metrics-apdex` for service quality and golden metrics.
4. Use `backend-release-health-regression` after deploys or suspected changes.
5. Use `backend-anomaly-baseline` when behavior is unusual but static
   thresholds have not fired.
6. Drill into APM, PostgreSQL, logs, host, Caddy, or PHP-FPM dashboards based on
   the first degraded signal.

New Relic service-level, Apdex, anomaly-alert, and change-tracking mutations
require New Relic account credentials and permissions. Until those are
available, this repo stores the intended configuration and dashboard evidence.

## Trace, Cache/Queue, And Privacy Reviews

Trace diagnostics are versioned in `backend-trace-diagnostics`, and live
verification now includes a PHP distributed tracing configuration check in
`scripts/backend_newrelic_observability_check.py --enforce`. Real trace
existence still requires New Relic account access and backend request traffic.

The cache and queue decision lives in `docs/newrelic-cache-queue-decision.json`.
No cache or queue dashboard is created while there is no backend-owned Redis,
Valkey, Memcached, or durable queue workload.

The telemetry privacy review lives in
`docs/newrelic-telemetry-privacy-review.json`. It covers logs, APM attributes,
custom events, query attributes, and synthetics with explicit allowlist and
denylist fields.

## Validate Reporting

Offline CI guard:

```bash
python3 scripts/backend_newrelic_observability_check.py --offline
```

Live backend verification after deploys or key rotation:

```bash
python3 scripts/backend_newrelic_observability_check.py --enforce
```

The live check verifies PHP extension loading, app-name configuration with
license redaction, infrastructure and daemon services, recent agent logs, and
an optional NerdGraph account query when `NEW_RELIC_USER_KEY` and
`NEW_RELIC_ACCOUNT_ID` are present.

## License Key Rotation

1. Create a replacement New Relic license key in New Relic.
2. Update the protected backend secret that feeds PHP APM, infrastructure,
   logs, and PostgreSQL telemetry.
3. Run the protected backend apply workflow.
4. Restart or verify PHP-FPM, `newrelic-daemon`, and `newrelic-infra` through
   the repo-managed workflow path.
5. Run `python3 scripts/backend_newrelic_observability_check.py --enforce`.
6. Confirm PHP APM, infrastructure, logs, and PostgreSQL telemetry report in
   New Relic.
7. Revoke the old key only after new telemetry is visible.

## Sources

- New Relic NerdGraph dashboard CRUD:
  https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-dashboards/
- New Relic dashboard widget schema:
  https://docs.newrelic.com/docs/apis/nerdgraph/examples/create-widgets-dashboards-api/
- New Relic API keys:
  https://docs.newrelic.com/docs/apis/intro-apis/new-relic-api-keys/
- New Relic NerdGraph endpoints:
  https://docs.newrelic.com/docs/apis/nerdgraph/get-started/introduction-new-relic-nerdgraph/
- New Relic PostgreSQL integration:
  https://docs.newrelic.com/install/postgresql/
- New Relic usage queries:
  https://docs.newrelic.com/docs/accounts/accounts-billing/new-relic-one-pricing-billing/usage-queries-alerts/
- New Relic log parsing:
  https://docs.newrelic.com/docs/logs/ui-data/parsing/
- New Relic drop filter rules:
  https://docs.newrelic.com/docs/logs/ui-data/drop-data-drop-filter-rules/
- New Relic log security and privacy:
  https://docs.newrelic.com/docs/logs/get-started/new-relics-log-management-security-privacy/
- New Relic service levels:
  https://docs.newrelic.com/docs/service-level-management/create-slm/
- New Relic change tracking:
  https://docs.newrelic.com/docs/change-tracking/overview/
- New Relic anomaly alerting:
  https://docs.newrelic.com/docs/alerts/create-alert/set-thresholds/anomaly-detection/
