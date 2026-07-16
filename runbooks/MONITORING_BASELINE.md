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
- installs `/usr/local/sbin/nutsnews-backend-smoke`.

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

Until the backend app exists, application status is `not_deployed`. Once deployed, `/healthz` must return a simple healthy response before public routing is enabled.

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

## Alert Delivery

Alert delivery is not configured yet. Before #7 can close, a later PR must configure and test an alert path such as GitHub Actions notification, email, uptime monitor, Grafana, Better Stack, or another documented lightweight service.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed monitoring baseline PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review log retention, sysstat, and smoke script changes.
4. Run `apply` mode after the `production-backend` approval gate.
5. Run the smoke command and review service/log state.
6. Configure and test alert delivery before closing #7.

## Rollback

Preferred rollback is a git revert followed by protected apply.

If log retention causes immediate trouble, remove or adjust `/etc/systemd/journald.conf.d/99-nutsnews-backend.conf` or `/etc/logrotate.d/nutsnews-backend` through a reviewed PR and protected apply. Record any break-glass action and reconcile it back into this repository.
