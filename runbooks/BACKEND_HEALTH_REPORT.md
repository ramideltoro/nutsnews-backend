# Backend Health Report

This runbook covers backend issue #38 for recurring read-only health reporting.

## What It Does

The `Backend Health Report` GitHub Actions workflow runs daily at `12:17 UTC` and can also be started manually.

The workflow:

- uses repository secrets for read-only SSH to `65.75.201.18`;
- runs `scripts/backend_health_report.py`;
- collects a fixed set of read-only host, service, backup, RabbitMQ drift,
  smoke, canary status, timer, listener, update, and recent-error signals;
- writes a sanitized JSON report artifact and GitHub step summary;
- loads the previous completed report artifact to maintain alert fingerprints, cooldown state, suppression counts, and recovery notices;
- reduces the report to a closed, safe-metadata-only health-audit event;
- optionally sends that bounded event to one fixed root-owned state writer so
  Alloy can expose workflow conclusion, last-success age, and consecutive
  failures;
- sends email through SMTP when reporting credentials are configured and there are unsuppressed alert notifications.

Report collection does not run arbitrary remote commands, restart services,
change packages, run RabbitMQ smoke, or call the protected Ansible apply
workflow. When bounded state publication is explicitly enabled, the only
additional remote command is the literal
`sudo -n /usr/local/sbin/nutsnews-health-audit-state write`. That root-owned
program accepts strict JSON on standard input and can replace only
`/var/lib/nutsnews/health-audit/last-run.json`. A validated sudoers drop-in
authorizes the `rami` account for that exact executable and `write` argument;
the event-production mode also refuses elevated execution. RabbitMQ smoke
remains a separate protected workflow because it creates isolated probe
resources and restarts the broker.

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
| `NUTSNEWS_HEALTH_AUDIT_REMOTE_PUBLISH_ENABLED` | unset/`false`; remote publication is skipped |

The workflow must not print secret values. The bounded publication path logs
only whether state changed and its closed-set conclusion.

`NUTSNEWS_BACKEND_HOST` is fail-closed to the canonical `65.75.201.18` target,
and the SSH user is fail-closed to the `rami` audit account. Neither value can
select an arbitrary publication target.

## Report States

The JSON report includes:

- `last_report_run_at`
- `next_report_run_at`
- `last_report_success_at`
- `conclusion` (`success` or `failure`)
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
- RabbitMQ recovery status, `rabbitmq_drift`, and the last protected smoke
  report from `/var/lib/nutsnews/rabbitmq-probes/last-smoke.json`
- the last private AMQP canary report from
  `/var/lib/nutsnews/rabbitmq-probes/last-canary.json`
- `supabase_sync_relay_health` from the backend-to-Supabase standby relay timer
  and `/var/lib/nutsnews/supabase-sync-relay/last-run.json`
- fixed-command SSH evidence
- classified checks for host resources, failed units, reboot/update state, core services, backup tooling, backup freshness, backup verification, restore drill status, storage quota status, recovery status, standby relay health, and sudo readiness

The standby relay health check reports only safe metadata:

- relay timer state, service state, and last service result;
- `last_applied_at_utc` and the latest validated success timestamp;
- `lag_seconds`;
- `failed_table_count`;
- generic `last_error` code;
- `standby_failover_blocked`.

The snapshot relay is intentionally suspended after its full-table parity scan
was shown to exhaust the shared Supabase production database. While suspended,
relay health reports `not_configured`, `expected_active=false`, and
`reason=disabled_by_configuration`; Supabase failover remains blocked. This
distinguishes a reviewed safe state from an operational outage without claiming
a current standby. Production availability takes precedence over standby
freshness. Do not re-enable the timer until reviewed incremental replication
replaces snapshot polling.

When the relay is expected active, lag over `180 seconds`, missing relay status,
invalid relay status, a stopped timer, failed relay result, unknown failed-table
count, or any failed replicated table marks `supabase_sync_relay_health` as
`critical`. That critical check
creates an alert candidate and blocks Supabase failover decision-making until a
fresh healthy relay report exists.

Statuses are:

- `healthy`
- `warning`
- `critical`
- `unknown`
- `not_configured`

Missing SMTP credentials degrade to `not_configured`. Missing SSH credentials are a workflow configuration error because the report cannot inspect the host.

Any `critical` check makes the report conclusion `failure`. A failed report
does not advance `last_report_success_at`, and the generator exits nonzero only
after writing the JSON artifact and step summary. The artifact upload uses
`always()`, so critical runs retain evidence. Warnings and known
`not_configured` states do not by themselves fail the workflow.

## Bounded Metrics State

The local event producer selects only these fields from the full artifact:

- fixed schema version, source, availability, and safe-metadata marker;
- `success` or `failure` conclusion;
- canonical UTC run timestamp;
- bounded critical-check count;
- fixed `86400` second expected interval.

It never publishes host output, check summaries, errors, identifiers, paths,
credentials, or email fields. Input size, exact keys, value types, timestamp
freshness, integer ranges, duplicate keys, and non-finite JSON values are
validated both before SSH and by the installed writer.

The writer holds a fixed-directory lock and performs a file-and-directory
`fsync` around an atomic replace. A failure preserves the prior
`last_success_at_utc` and increments `consecutive_failures`; a success updates
last success and resets the counter. Older events, conflicting same-timestamp
events, malformed prior state, symbolic-link targets, and values outside the
closed schema are rejected without replacing durable truth. An exact replay is
idempotent.

## Rollout Order After Incident Freeze

The generation-5 production incident freeze was explicitly lifted by the
operator on 2026-08-22 for production alert remediation. Automated validation
remains required. Merges to `main` may now run the backend baseline and related
production workflows without a manual approval or confirmation step.

The authoritative worker mode remains `shadow_comparison`. Lifting the incident
freeze does not authorize the database or worker-uplift cutover; those state
transitions still require their declared automated health gates.

After a qualifying merge:

1. The Backend Ansible Apply workflow automatically runs the full baseline in
   apply mode against `backend.nutsnews.com`.
2. Verify the installed writer checksum, metrics state directory, service
   health, and exported `nutsnews_backend_health_audit_*` metrics.
3. Dispatch one health report and confirm a successful bounded publication.
4. Exercise critical-check fixtures only through the controlled failure-drill
   workflow; confirm the report fails while its artifact remains available and
   last success does not advance.

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
python3 -m unittest tests.test_backend_health_audit_state
```

Run full backend validation:

```bash
python3 scripts/validate_no_secret_files.py
python3 -m unittest discover -s tests
```

## Rollback

Set `NUTSNEWS_HEALTH_AUDIT_REMOTE_PUBLISH_ENABLED=false` before reverting the
workflow or removing the installed writer. Disabling the variable immediately
stops remote state mutation while leaving report generation and artifacts
intact. Revert source through a pull request and remove the writer only through
the protected Ansible apply path after the current freeze is lifted.

Rotate SMTP or SSH credentials in GitHub secrets if a secret is suspected to be exposed. Do not commit report artifacts that contain live host evidence.

## Semantic one-shot failure handling

The generic `failed_systemd_units` check excludes
`nutsnews-postgres-replication-health.service` and
`nutsnews-newrelic-job-metrics.service`. Those one-shot units intentionally
fail when their domain check is unhealthy, so counting them again as generic
host failures creates duplicate critical alerts.

Their underlying state remains visible through
`postgres_replication_health` and `newrelic_job_metrics_delivery`.
New Relic metric delivery failures are warnings because Grafana remains the
primary backend alerting path.

## Cutover-aware failover readiness

The PostgreSQL failover-ready metric always requires a healthy database status
and a healthy restore drill. Before cutover, when replication evidence declares
`expected_active=false`, inactive replication does not make restore readiness
false. Once `expected_active=true`, readiness is fail-closed unless replication
evidence is fresh, lag is healthy, and there are no blockers.
