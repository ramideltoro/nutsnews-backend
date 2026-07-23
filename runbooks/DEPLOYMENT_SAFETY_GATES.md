# Backend Deployment Safety Gates

This runbook covers backend issue #45 for `65.75.201.18`.

## Changing Workflows

Backend-changing workflows are:

- `Protected Backend Ansible Apply`
- `Backend Cloudflare Routing` when `run_mode` is `apply` or `rollback`
- `Backend Controlled Maintenance` when `action` is `security-upgrade` or `reboot`
- `Backend Backup Maintenance` when `action` is `backup`, `verify`, or `restore-drill`
- `Backend Recovery` when `mode` is `apply` and the selected action is mutating
- `Backend RabbitMQ Smoke` when `action` is `smoke`
- `Backend RabbitMQ Canary` when `action` is `canary` or `drill`

Read-only workflows such as `Backend Drift Check`, `Backend Health Report`, and
`Backend Credential Readiness` do not mutate the host or providers. `Backend
Recovery` is read-only only when `mode=check`, `action=diagnostics`, or
`action=backup-status`. `Backend RabbitMQ Smoke` is read-only only when
`action=status`. `Backend RabbitMQ Canary` is read-only only when
`action=status`; scheduled and manual canary runs publish and ack a private
probe message on the isolated canary route.

## Gate Model

Safety gate script:

```text
scripts/backend_deployment_safety.py
```

The gate runs fixed read-only checks only. It validates secret presence by name
only and never prints values.

Critical pre/post surfaces include:

- failed systemd units;
- running kernel and latest installed kernel;
- root disk and inode pressure;
- pending reboot state;
- Caddy service and Caddy config validation;
- Docker health when Docker is configured;
- RabbitMQ health, network security, public exposure, and `rabbitmq_drift` after broker provisioning;
- backend local health and public HTTPS health;
- backup freshness and restore verification state;
- active critical alert state;
- required secret presence by name only.

The gate emits JSON and Markdown reports. Missing or unknown critical evidence
fails closed when the workflow is enforcing the gate.

## Workflow Coverage

### Protected Backend Ansible Apply

Check mode runs the deployment gate as a non-blocking dry run. Apply mode runs
the same gate as an enforced preflight before Ansible and an enforced postcheck
after Ansible.

For RabbitMQ, protected apply also runs credential readiness by name, writes
`/var/lib/nutsnews/rabbitmq-probes/apply-metadata.json`, and the postcheck
blocks on RabbitMQ health, network posture, public exposure, and
`rabbitmq_drift`. It also installs `nutsnews-rabbitmq-canary.timer` and runs the
private AMQP canary once after topology bootstrap.

Rollback path:

1. Revert the reviewed change or open a dedicated rollback PR.
2. Run protected check mode.
3. Run protected apply mode only after approval.
4. Verify the postcheck report, drift report, and live read-only SSH evidence.

Break-glass SSH mutation is emergency-only and must be reconciled back into this
repository.

### Backend Cloudflare Routing

Check mode plans DNS state and runs the routing safety gate as a non-blocking
dry run. Apply and rollback modes require confirmation text, Environment
approval, enforced preflight, and enforced postcheck.

Apply rollback path:

1. Run `Backend Cloudflare Routing` with `run_mode=rollback`.
2. Set `confirm_apply=backend.nutsnews.com`.
3. Verify the routing artifact and DNS result.
4. Verify the backend origin health separately through read-only SSH or direct
   origin resolution.

Rollback mode does not require the public endpoint to remain healthy after DNS
deletion, but it still requires host health and required secrets.

### Backend Controlled Maintenance

The controlled maintenance workflow has its own fixed pre/post-check runner:

```text
scripts/backend_controlled_maintenance.py
```

Allowed actions are only `precheck`, `security-upgrade`, and `reboot`.
Mutating actions require `confirm_target=backend.nutsnews.com` and
`production-backend` Environment approval.

Recovery path:

- Security updates are not generally rolled back in place. Recover the affected
  service first, document changed packages, and reconcile the fix through this
  repo.
- Reboot failure recovery starts with provider console or SSH access, then
  verifies boot ID, kernel, failed units, Caddy health, disk pressure, backup
  state, and alert state before any further mutation.

### Backend Backup Maintenance

The backup workflow starts only fixed systemd units:

- `nutsnews-backup.service`
- `nutsnews-backup-verify.service`
- `nutsnews-restore-drill.service`

It then reads `/usr/local/sbin/nutsnews-backup status` and uploads the sanitized
JSON report. It does not accept arbitrary commands or paths.

Recovery path:

1. If backup fails, preserve the existing restic repository and status files.
2. Fix credentials, repository access, or matrix paths through a reviewed PR.
3. Re-run `backup`, then `verify`, then `restore-drill`.
4. Do not delete old snapshots until the latest backup verifies and a restore
   drill passes.

### Backend Recovery

The recovery workflow has its own fixed action runner:

```text
scripts/backend_recovery_workflow.py
```

Allowed actions are only:

- `diagnostics`
- `backup-status`
- `trigger-backup`
- `trigger-restore-drill`
- `reload-caddy`
- `restart-caddy`
- `restart-alloy`
- `restart-fail2ban`
- `refresh-metrics`
- `refresh-ops-dashboard`

Mutating applies require `mode=apply`, `confirm_target=backend.nutsnews.com`,
and `production-backend` Environment approval. Check mode runs the same fixed
prechecks without mutation. No action accepts free-form commands, service
names, Ansible tags, paths, or scripts.

Recovery path:

1. Run the intended action with `mode=check`.
2. Review the `backend-recovery-report` artifact for blockers.
3. Run `mode=apply` only for a fixed mutating action that passes check mode.
4. Verify the postchecks and the health report `recovery_last_run` state.
5. Reconcile any break-glass manual change through this repository.

### Backend RabbitMQ Smoke

The RabbitMQ smoke workflow has only fixed actions:

- `status`: read-only health check;
- `smoke`: isolated broker probe that creates only
  `worker.uplift.probe.smoke.*` resources, restarts
  `nutsnews-rabbitmq.service`, verifies the durable probe message, and cleans up.

The mutating `smoke` action requires `confirm_target=backend.nutsnews.com` and
`production-backend` approval. It uploads
`backend-rabbitmq-smoke-report.json` and writes
`/var/lib/nutsnews/rabbitmq-probes/last-smoke.json` on the host. It does not
accept free-form commands, service names, queue names, message payloads, or
Ansible tags.

Recovery path:

1. Run `action=status` and `Backend Drift Check`.
2. If smoke is needed, run `action=smoke` only after approval.
3. If smoke fails, preserve `last-smoke.json`, inspect `rabbitmq_drift`, and fix
   through a reviewed PR plus protected check/apply.
4. Use break-glass SSH mutation only if the protected workflow cannot restore
   broker health, then reconcile the state back into this repository.

## Current Expected Non-Configured Surfaces

Until the related issues land, the backend app and active-alert state may be
reported as `not_configured`. Backup freshness and restore verification become
healthy only after the backup workflow has run `backup`, `verify`, and
`restore-drill`. RabbitMQ smoke remains `not_configured` until the fixed
`Backend RabbitMQ Smoke` workflow writes `last-smoke.json`. Profiles only block
on the surfaces that are critical for the specific workflow action, so
foundational work can still deploy missing safety components through the
protected path.

## Validation

Run locally:

```bash
python3 scripts/validate_recovery_workflows.py
python3 scripts/validate_worker_uplift_rabbitmq_operations.py
python3 -m unittest discover -s tests
git diff --check
actionlint .github/workflows/*.yml
python3 scripts/validate_no_secret_files.py
```

Live validation should use:

- protected check-mode dry run for `Protected Backend Ansible Apply`;
- Cloudflare routing check mode;
- controlled maintenance `precheck`;
- backend recovery `mode=check`;
- backend RabbitMQ smoke `action=status`;
- read-only SSH verification after any approved mutation.
