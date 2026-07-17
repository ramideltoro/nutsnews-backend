# Backend Recovery Workflows

This runbook covers backend issue #42 for fixed-purpose recovery on
`backend.nutsnews.com`.

## Operating Boundary

Recovery actions run through GitHub Actions, not through ad hoc SSH mutation.
The workflow is:

```text
.github/workflows/backend-recovery.yml
```

The runner is:

```text
scripts/backend_recovery_workflow.py
```

The workflow accepts only fixed choices. It does not accept arbitrary commands,
service names, Ansible tags, shell scripts, paths, or user supplied remote
snippets.

## Actions

Read-only actions:

| Action | Purpose |
| --- | --- |
| `diagnostics` | Collect fixed host, service, Caddy, Alloy, backup, metrics, dashboard, and recovery-status diagnostics. |
| `backup-status` | Read the service-aware backup status report. |

Mutating recovery actions:

| Action | Fixed command intent | Key precheck | Key postcheck |
| --- | --- | --- | --- |
| `trigger-backup` | Start `nutsnews-backup.service` | backup runner and unit are present | backup freshness is healthy |
| `trigger-restore-drill` | Start `nutsnews-restore-drill.service` | backup runner and unit are present | restore drill is healthy |
| `reload-caddy` | Validate Caddy config and reload `caddy` | Caddy config validates | Caddy active and `/healthz` healthy |
| `restart-caddy` | Validate Caddy config and restart `caddy` | Caddy config validates | Caddy active and `/healthz` healthy |
| `restart-alloy` | Validate Alloy config and restart `alloy` | Alloy config validates | Alloy active |
| `restart-fail2ban` | Restart `fail2ban` | SSH remains active | fail2ban active |
| `refresh-metrics` | Start `nutsnews-metrics-textfile.service` | metrics one-shot unit is present | metrics textfile exists |
| `refresh-ops-dashboard` | Start `nutsnews-ops-dashboard-collect.service` | dashboard collector unit is present | dashboard status snapshot exists |

Controlled package updates and reboots remain in
`Backend Controlled Maintenance`. Broad host configuration changes remain in
`Protected Backend Ansible Apply`.

## Approval And Confirmation

Every workflow run uses the protected `production-backend` GitHub Environment.

For any mutating action:

1. Run the same action in `mode=check`.
2. Review the uploaded `backend-recovery-report` artifact and precheck summary.
3. Run `mode=apply` only after the check mode passes.
4. Set `confirm_target=backend.nutsnews.com`.
5. Approve the protected Environment deployment.

Read-only `diagnostics` and `backup-status` do not require confirmation text,
but they still use the same fixed command set and sanitized report format.

## Last-Run Status

Approved mutating applies write:

```text
/var/lib/nutsnews/recovery/last-recovery.json
```

The file records action, mode, status, actor, workflow URL, timestamps, and a
sanitized error string when present. It does not include secrets, command
output, environment values, private keys, tokens, or database URLs.

The backend health report exposes this state as `recovery_last_run`.

## Failure Behavior

The recovery runner fails closed when:

- SSH cannot collect required precheck evidence;
- root disk or inode state is unknown or critical;
- the action-specific precheck is not healthy;
- a mutating apply omits `confirm_target=backend.nutsnews.com`;
- the fixed recovery command exits non-zero;
- an action-specific postcheck is not healthy;
- the last-run status file cannot be written after an otherwise successful
  mutating apply.

If check mode reports blockers, fix the underlying issue through a reviewed PR
and the protected apply path where possible. Use manual SSH mutation only for a
documented break-glass incident.

## Validation

Local validation:

```bash
python3 scripts/validate_recovery_workflows.py
python3 -m unittest tests.test_backend_recovery_workflow
python3 -m unittest tests.test_backend_health_report
actionlint .github/workflows/backend-recovery.yml
```

Live non-mutating validation:

```bash
python3 scripts/backend_recovery_workflow.py \
  --action diagnostics \
  --mode check \
  --ssh-host 65.75.201.18 \
  --ssh-user rami \
  --ssh-key ~/.ssh/servercheap_65_75_201_18 \
  --known-hosts ~/.ssh/known_hosts \
  --output /tmp/backend-recovery-diagnostics.json
```

## Rollback

Revert the PR that introduced or changed the recovery action, then merge and
let the normal checks pass. If a mutating recovery action worsens host health,
prefer the narrowest fixed recovery action that restores the affected service,
then reconcile any emergency change through this repository.
