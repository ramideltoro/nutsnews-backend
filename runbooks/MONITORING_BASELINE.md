# Monitoring And Log Retention Baseline

This runbook covers backend issue #7 for `65.75.201.18`.

## Acceptance Criteria

- A documented health check exists for the backend application and host.
- Disk, memory, service failure, and endpoint availability have alert thresholds.
- Logs are retained with a clear rotation policy.
- A read-only smoke command can verify the host and app after deploy or reboot.
- Alert delivery is tested at least once.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The monitoring baseline:

- installs `logrotate` and `sysstat`;
- creates `/var/log/nutsnews`;
- configures persistent journald with `SystemMaxUse=512M` and `MaxRetentionSec=14day`;
- rotates `/var/log/nutsnews/*.log` daily with 14 compressed rotations;
- enables sysstat collection;
- installs `/usr/local/sbin/nutsnews-backend-smoke`;
- includes a local Caddy `/healthz` probe in the smoke output when Caddy is active.

## Health Checks

Host smoke command:

```bash
sudo /usr/local/sbin/nutsnews-backend-smoke
```

Read-only remote smoke command after apply:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && systemctl --failed --no-pager && ss -tulpen 2>/dev/null || ss -tulpn'
```

Application health endpoint:

```text
/healthz
```

Until the backend app exists, application status is `infrastructure_health_only`.
The Caddy-managed `/healthz` endpoint returns `ok` to prove the origin and
routing are ready; all other public paths return `404` until a reviewed backend
app deployment owns them.

The smoke command and scheduled health report verify that endpoint locally with
SNI pinned to loopback:

```bash
curl -fsS --resolve backend.nutsnews.com:443:127.0.0.1 https://backend.nutsnews.com/healthz
```

## Alert Thresholds

Initial host thresholds:

| Signal | Warning | Critical |
| --- | --- | --- |
| Root disk used | 80% | 90% |
| Memory used | 80% | 90% |
| Load average per CPU | 1.5 | 2.5 |
| Failed systemd units | any failed unit | any failed unit after one recheck |
| Pending reboot | present after maintenance window | present after approved reboot window |
| Backend endpoint | one failed check | two consecutive failed checks |
| Backup freshness | workload-specific | beyond restore policy RPO |

## Log Retention

- System journal: persistent, capped at 512 MiB, retained up to 14 days.
- Backend app logs: `/var/log/nutsnews/*.log`, daily rotation, 14 retained rotations, compressed.
- Logs must not contain secrets, tokens, private keys, database dumps, or full environment output.

## Grafana Cloud Logs

Backend issue #36 adds a repo-managed Loki shipping path through Grafana Alloy.
The same protected Ansible apply that manages metrics renders
`/etc/alloy/config.alloy` with Loki blocks only when all of these
`production-backend` environment secrets are present:

- `GRAFANA_CLOUD_LOKI_URL`
- `GRAFANA_CLOUD_LOKI_USERNAME`
- `GRAFANA_CLOUD_LOKI_PASSWORD`

Collected sources:

- filtered systemd journal units for Caddy, Alloy, backup/restore verification,
  NutsNews timers, SSH, UFW, fail2ban, unattended-upgrades, and apt timers;
- `/var/log/auth.log` and `/var/log/fail2ban.log` when readable through the
  host `adm` group;
- `/var/log/caddy/access.log`, `/var/log/caddy/error.log`, and
  `/var/log/nutsnews/*.log` when those files exist.

Alloy runs as the package-managed `alloy` user and is added only to
`systemd-journal` and `adm` for read access. It is not given Docker socket
access. Docker/Compose container logs remain intentionally excluded until the
backend app has containers and a reviewed least-privilege container log path.

Before logs are shipped, Alloy drops private-key markers and oversized lines,
redacts authorization headers, cookies, token/password/API-key style values,
query strings, and email addresses, truncates long lines, and keeps only stable
labels:

```text
environment, host, source, service, unit, severity, filename, job
```

The managed Grafana folder is `NutsNews Backend Ops` (`nutsnews-backend-ops`).
The logs dashboard is `NutsNews Backend Logs` (`nutsnews-backend-logs`) and uses
the Grafana Loki datasource discovered by `scripts/provision_grafana_metrics.py`.

## Alert Delivery

Alert delivery uses `.github/workflows/backend-health-report.yml`. The manual
and scheduled workflow runs a fixed set of read-only SSH checks, writes a JSON
artifact, and can send the report by SMTP when `send_email=true`.

The delivery result is recorded in the report artifact as
`delivery.status=sent`, `skipped`, `not_configured`, or `error`.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed monitoring baseline PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review log retention, sysstat, and smoke script changes.
4. Run `apply` mode after the `production-backend` approval gate.
5. Run the smoke command and review service/log state.
6. Run `Backend Health Report` with `send_email=true` and confirm
   `delivery.status=sent` in the artifact before closing #7.

## Rollback

Preferred rollback is a git revert followed by protected apply.

If log retention causes immediate trouble, remove or adjust `/etc/systemd/journald.conf.d/99-nutsnews-backend.conf` or `/etc/logrotate.d/nutsnews-backend` through a reviewed PR and protected apply. Record any break-glass action and reconcile it back into this repository.
