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

Each dashboard keeps a 24 hour default query window and uses the
`NutsNews Backend - ` naming prefix. Live dashboard URLs are printed by
`scripts/provision_newrelic_dashboards.py` after New Relic credentials are
available in the runtime environment.

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
