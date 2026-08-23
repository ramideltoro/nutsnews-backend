# OS Maintenance Runbook

This runbook covers backend OS maintenance for `65.75.201.18`.

## Acceptance Criteria

- `apt list --upgradable` is empty or only contains explicitly deferred packages.
- `uname -r` shows the expected updated kernel.
- `/var/run/reboot-required` is absent after reboot.
- SSH key login as `rami` still works after reboot.
- No failed systemd units are present after reboot.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The baseline maintenance tasks:

- refresh apt metadata;
- do not apply broad package upgrades by default;
- do not remove packages by default;
- do not reboot by default;
- capture package, kernel, reboot-required, and failed-unit evidence in the workflow logs.

Broad `dist` upgrades, package autoremove, and reboots are opt-in variables and
must not be enabled for routine baseline applies. Security-update and reboot
maintenance belongs in the fixed-purpose workflow described below.

## Controlled Maintenance Workflow

Workflow: `Backend Controlled Maintenance`

Allowed actions:

- `precheck`: read-only state collection only.
- `security-upgrade`: records precheck evidence, then runs the fixed `unattended-upgrade` path.
- `reboot`: records precheck evidence, then runs the fixed reboot path.

The workflow has no arbitrary command input. The `production-backend`
Environment scopes credentials only; it has no reviewer, approval, or wait
gate. Every action uploads a sanitized
`backend-controlled-maintenance-report` artifact.

When the backend RabbitMQ broker is healthy before a controlled reboot, the
reboot action attempts to publish a durable RabbitMQ probe message, reboots the
host, then waits for SSH to return with a changed boot ID before attempting to
verify and delete the probe message. Probe failures are advisory evidence and
do not block the reboot or turn a changed boot ID into a failed maintenance
result. Probe credentials are read from the root-only broker environment file
and are not passed as workflow inputs or command-line secret values.

Deployment safety and recovery paths for this workflow are summarized in
[DEPLOYMENT_SAFETY_GATES.md](DEPLOYMENT_SAFETY_GATES.md).

Prechecks include:

- backup freshness status;
- failed systemd units;
- running kernel and latest installed kernel;
- Docker, Caddy, SSH, UFW, and fail2ban states;
- RabbitMQ service state and loopback management health when configured;
- backend health endpoint state;
- root disk and inode pressure;
- active alert state;
- reboot-required and package-update visibility;
- unattended-upgrade availability.

Current expected not-configured states are explicit. Precheck findings and the
derived `mutation_blockers` list are advisory evidence: they do not stop or
delay a fixed maintenance action. A reboot succeeds when the host returns with
a changed boot ID; post-reboot service, kernel, backup, alert, and RabbitMQ
findings remain visible in the report.

## Apply Path

For baseline changes, merge a tested pull request to exact `main`. The
`Backend Ansible Apply` workflow starts automatically in apply mode without a
reviewer, approval, wait timer, or manual dispatch. Manual check mode remains
available for diagnostics but is not a production gate.

For controlled maintenance:

1. Run `Backend Controlled Maintenance` with `action=precheck`.
2. Review the report artifact and summary.
3. Run `action=security-upgrade` or `action=reboot` only when required.
4. Review the postcheck report before closing any maintenance issue.

## Verification

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'uname -r'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'test ! -e /var/run/reboot-required && echo no-reboot-required'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'apt list --upgradable 2>/dev/null'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'systemctl --failed --no-pager'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'cat /proc/sys/kernel/random/boot_id'
```

If packages remain upgradable, document whether they are explicitly deferred and why.

## Rollback

Package upgrades are not generally rolled back in place.

If a package update breaks the host:

1. Use provider console or SSH break-glass only if the protected workflow cannot recover.
2. Restore service health first.
3. Capture changed packages and logs.
4. Reconcile the fix into this repository through a pull request.

For a kernel regression, select the previous bootable kernel from the provider console or bootloader if available, then reconcile the pinned/deferred package state in this repo.

If a controlled reboot fails postchecks:

1. Do not run additional broad maintenance.
2. Verify SSH and provider console access.
3. Capture boot ID, kernel, failed units, Caddy health, disk pressure, and backup
   state through read-only commands.
4. Recover only the minimum service needed for access or health.
5. Reconcile any break-glass change into this repository through review.
