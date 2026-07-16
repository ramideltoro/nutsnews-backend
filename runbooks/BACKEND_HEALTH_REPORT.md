# Backend Health Report

This runbook covers backend issue #38 for recurring read-only health reporting.

## What It Does

The `Backend Health Report` GitHub Actions workflow runs daily at `12:17 UTC` and can also be started manually.

The workflow:

- uses repository secrets for read-only SSH to `65.75.201.18`;
- runs `scripts/backend_health_report.py`;
- collects a fixed set of read-only host, service, backup, timer, listener, update, and recent-error signals;
- writes a sanitized JSON report artifact and GitHub step summary;
- sends email through SMTP when reporting credentials are configured.

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
- fixed-command SSH evidence
- classified checks for host resources, failed units, reboot/update state, core services, backup tooling, and sudo readiness

Statuses are:

- `healthy`
- `warning`
- `critical`
- `unknown`
- `not_configured`

Missing SMTP credentials degrade to `not_configured`. Missing SSH credentials are a workflow configuration error because the report cannot inspect the host.

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
