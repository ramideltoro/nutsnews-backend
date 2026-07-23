# Backend Synthetic Monitoring

This runbook covers off-box checks of public NutsNews surfaces and protected
admin backend operations.

## Source

Workflow: `.github/workflows/backend-synthetic-monitor.yml`

The monitor runs from GitHub-hosted runners, not from the backend VPS. It runs
hourly and can also be dispatched manually.

## Checks

The workflow runs unauthenticated public `GET` checks:

- `frontend_www_home`: `https://www.nutsnews.com/`, expects `200`.
- `frontend_apex_redirect`: `https://nutsnews.com/`, expects redirect to
  `https://www.nutsnews.com/`.
- `backend_healthz`: `https://backend.nutsnews.com/healthz`, expects `200` and
  body `ok`.
- `backend_tls_known_404`: `https://backend.nutsnews.com/`, expects the current
  known `404` while the backend application is not deployed.
- `supabase_platform_status`: Supabase public status API, used as the
  auth-provider availability signal without project secrets.

The workflow also runs tokened, read-only `POST` checks against
`https://backend.nutsnews.com/api/app/db/*` using
`NUTSNEWS_BACKEND_API_TOKEN`. These checks exercise the required protected admin
backend operations with bounded limits:

- `load-admin-production-readiness`
- `load-admin-article-reviews`
- `load-admin-article-engagement`
- `load-admin-ai-usage`
- `load-admin-local-ai`
- `load-admin-translation-quality`
- `load-admin-guardrails`
- `load-admin-worker-shards`
- `load-admin-rss-feed-health`
- `load-admin-feed-management`
- `load-admin-audit-log`
- `load-admin-runtime-feature-flags`

The tokened checks do not submit forms, use write operations, or include
response row bodies in artifacts or email. Public `/healthz` success alone is
not backend admin compatibility success; the scheduled monitor is critical when
the protected admin operation token is missing or any required operation returns
non-2xx or an invalid response shape.

## New Relic Acceptance

Issue #172 is accepted against the existing New Relic simple synthetic monitor
`NutsNews Backend Health Ping` while scripted API synthetics are unavailable on
the current New Relic account. New Relic returned `PAYMENT_REQUIRED` for the
scripted API monitor, so the scripted behavior check remains documented as a
future upgrade in `docs/newrelic-live-configuration.json`.

## Reporting And Alerts

Each run uploads `backend-synthetic-report.json`, writes a GitHub step summary,
and records:

- endpoint or operation name;
- status and failure class;
- HTTP status;
- sanitized backend admin route and row count for protected operation checks;
- source provider and runner location;
- last successful check timestamp when a previous completed report artifact is
  available.

The workflow downloads the previous completed `backend-synthetic-report`
artifact from `main`. This includes prior failed monitor runs when their
artifacts were uploaded, which lets repeated failures share cooldown state.

When `send_email=true`, email is sent only for unsuppressed alert notifications
through the existing NutsNews reporting SMTP secret names:

- `NUTSNEWS_REPORT_SMTP_HOST`
- `NUTSNEWS_REPORT_SMTP_USERNAME`
- `NUTSNEWS_REPORT_SMTP_PASSWORD`
- `NUTSNEWS_REPORT_EMAIL_FROM`
- `NUTSNEWS_REPORT_EMAIL_TO`
- `NUTSNEWS_BACKEND_API_TOKEN`

The authoritative reporting surface is the GitHub Actions run and its uploaded
artifact.

Alert fingerprints use the synthetic source, endpoint service name, severity,
failure class, and normalized failure detail with volatile timestamps, long hex
IDs, and numbers replaced. Repeated identical failures are suppressed for `1`
hour. Suppression is visible in `alerting.suppressed` and each active alert
record carries a cumulative `suppressed_count`.

Recovered endpoint checks emit `severity=recovered` notifications when a
previously active fingerprint clears. The JSON artifact and step summary expose
active alert count, notification count, suppressed count, recovered count, last
sent timestamp, and last error.

## Validation

Run locally:

```bash
python3 -m unittest discover -s tests
NUTSNEWS_BACKEND_API_TOKEN=... python3 scripts/backend_synthetic_monitor.py --output /tmp/backend-synthetic-report.json
actionlint .github/workflows/backend-synthetic-monitor.yml
git diff --check
```
