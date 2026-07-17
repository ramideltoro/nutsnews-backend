# Backend Synthetic Monitoring

This runbook covers backend issue #30 for off-box checks of public NutsNews
surfaces.

## Source

Workflow: `.github/workflows/backend-synthetic-monitor.yml`

The monitor runs from GitHub-hosted runners, not from the backend VPS. It runs
hourly and can also be dispatched manually.

## Checks

The workflow runs unauthenticated `GET` checks only:

- `frontend_www_home`: `https://www.nutsnews.com/`, expects `200`.
- `frontend_apex_redirect`: `https://nutsnews.com/`, expects redirect to
  `https://www.nutsnews.com/`.
- `backend_healthz`: `https://backend.nutsnews.com/healthz`, expects `200` and
  body `ok`.
- `backend_tls_known_404`: `https://backend.nutsnews.com/`, expects the current
  known `404` while the backend application is not deployed.
- `supabase_platform_status`: Supabase public status API, used as the
  auth-provider availability signal without project secrets.

The checks do not authenticate, submit forms, mutate production, or exercise
admin flows.

## Reporting And Alerts

Each run uploads `backend-synthetic-report.json`, writes a GitHub step summary,
and records:

- endpoint name;
- status and failure class;
- HTTP status;
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
python3 scripts/backend_synthetic_monitor.py --output /tmp/backend-synthetic-report.json
actionlint .github/workflows/backend-synthetic-monitor.yml
git diff --check
```
