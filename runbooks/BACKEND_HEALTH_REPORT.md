# Backend Health Report

This runbook covers backend issue #38 for recurring read-only health reporting.

## What It Does

The `Backend Health Report` GitHub Actions workflow runs daily at `12:17 UTC` and can also be started manually.

The workflow:

- uses repository secrets for read-only SSH to `65.75.201.18`;
- runs `scripts/backend_health_report.py`;
- collects a fixed set of read-only host, service, backup, timer, listener, update, and recent-error signals;
- writes a sanitized JSON report artifact and GitHub step summary;
- loads the previous completed report artifact to maintain alert fingerprints, cooldown state, suppression counts, and recovery notices;
- sends email through SMTP when reporting credentials are configured and there are unsuppressed alert notifications.

It does not run arbitrary remote commands, restart services, change packages, edit files, or call the protected Ansible apply workflow.

## Required Repository Secrets

These secrets are required for unattended scheduled reporting:

| Secret | Purpose |
| --- | --- |
| `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY` | SSH key for the read-only `rami` backend audit session |
| `NUTSNEWS_BACKEND_KNOWN_HOSTS` | Verified known_hosts entry for `65.75.201.18` |

These secrets enable email delivery:

| Secret | Purpose |
| --- | --- |
| `NUTSNEWS_REPORT_SMTP_HOST` | SMTP host |
| `NUTSNEWS_REPORT_SMTP_USERNAME` | SMTP username |
| `NUTSNEWS_REPORT_SMTP_PASSWORD` | SMTP password or provider token |
| `NUTSNEWS_REPORT_EMAIL_FROM` | Report sender address |
| `NUTSNEWS_REPORT_EMAIL_TO` | Comma-separated report recipient addresses |

Optional repository or environment variables:

| Variable | Default |
| --- | --- |
| `NUTSNEWS_BACKEND_HOST` | `65.75.201.18` |
| `NUTSNEWS_REPORT_SMTP_PORT` | `587` |
| `NUTSNEWS_REPORT_SMTP_STARTTLS` | `true` |
| `NUTSNEWS_REPORT_SUBJECT_PREFIX` | `[NutsNews backend]` |

The workflow prints only credential names and delivery status. It must not print secret values.

## Report States

The JSON report includes:

- `last_report_run_at`
- `next_report_run_at`
- `last_report_success_at`
- `last_error`
- delivery status
- `alerting.summary.active_alert_count`
- `alerting.summary.notification_count`
- `alerting.summary.suppressed_count`
- `alerting.summary.suppressed_total_count`
- `alerting.summary.recovered_count`
- `alerting.summary.last_sent_at`
- `alerting.summary.last_error`
- `alerting.notifications`
- `alerting.suppressed`
- `alert_state.alerts`
- cleanup last-run status when `/var/lib/nutsnews/cleanup/last-cleanup.json`
  exists
- recovery last-run status when `/var/lib/nutsnews/recovery/last-recovery.json`
  exists
- fixed-command SSH evidence
- classified checks for host resources, failed units, reboot/update state, core services, backup tooling, backup freshness, backup verification, restore drill status, storage quota status, recovery status, and sudo readiness

Statuses are:

- `healthy`
- `warning`
- `critical`
- `unknown`
- `not_configured`

Missing SMTP credentials degrade to `not_configured`. Missing SSH credentials are a workflow configuration error because the report cannot inspect the host.

## Alert Deduplication

The report turns every warning, critical, or unknown check into an alert candidate. Healthy and `not_configured` checks are not alert candidates.

Alert fingerprints are based on:

- source;
- service/check name;
- severity;
- failure class;
- normalized message text with volatile timestamps, long hex IDs, and numbers replaced.

Repeated identical alert fingerprints are suppressed for `24` hours. Suppressed alerts remain auditable in `alerting.suppressed`, and each active alert record carries a cumulative `suppressed_count`.

Recovery notifications are emitted when a previously active fingerprint is absent from the current report. Recovery records use `severity=recovered` and `status=recovered`.

The workflow downloads the previous completed `backend-health-report` artifact from `main`. This intentionally includes failed delivery runs when an artifact exists, so cooldown state survives transient SMTP or workflow failures.

Email behavior:

- unsuppressed new, cooldown-expired, severity-changed, or recovered notifications send email;
- repeated identical alerts inside the cooldown window skip email with a `no unsuppressed notifications` delivery status;
- missing SMTP credentials still record alert state but do not send mail.

## Manual Validation

Generate a local JSON report without email:

```bash
python3 scripts/backend_health_report.py \
  --ssh-host 65.75.201.18 \
  --ssh-user rami \
  --ssh-key ~/.ssh/servercheap_65_75_201_18 \
  --known-hosts ~/.ssh/known_hosts \
  --output /tmp/backend-health-report.json
```

Run unit tests:

```bash
python3 -m unittest tests.test_backend_health_report
```

Run full backend validation:

```bash
python3 scripts/validate_no_secret_files.py
python3 -m unittest discover -s tests
```

## Rollback

Disable or revert the workflow through a pull request if reports become noisy or delivery fails.

Rotate SMTP or SSH credentials in GitHub secrets if a secret is suspected to be exposed. Do not commit report artifacts that contain live host evidence.
