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
- `security-upgrade`: runs the fixed `unattended-upgrade` path only after prechecks pass and `confirm_target` is exactly `backend.nutsnews.com`.
- `reboot`: runs a fixed reboot path only after prechecks pass and `confirm_target` is exactly `backend.nutsnews.com`.

The workflow has no arbitrary command input. All actions run under the protected
`production-backend` Environment approval gate and upload a sanitized
`backend-controlled-maintenance-report` artifact.

Deployment safety and recovery paths for this workflow are summarized in
[DEPLOYMENT_SAFETY_GATES.md](DEPLOYMENT_SAFETY_GATES.md).

Prechecks include:

- backup freshness status;
- failed systemd units;
- running kernel and latest installed kernel;
- Docker, Caddy, SSH, UFW, and fail2ban states;
- backend health endpoint state;
- root disk and inode pressure;
- active alert state;
- reboot-required and package-update visibility;
- unattended-upgrade availability.

Current expected not-configured states are explicit. A reboot is blocked until
backup freshness and active-alert state are healthy, so it should normally be
run after backup and alerting issues are complete.

## Apply Path

For baseline changes, run only through the protected backend workflow:

1. Merge the reviewed OS maintenance PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Confirm the baseline plan does not enable broad upgrades or reboot unless a
   reviewed issue explicitly adds those variables.
4. Run `apply` mode only during an approved maintenance window.
5. Run the verification commands below.

For controlled maintenance:

1. Run `Backend Controlled Maintenance` with `action=precheck`.
2. Review the report artifact and summary.
3. Run `action=security-upgrade` or `action=reboot` only when required.
4. For mutating actions, set `confirm_target=backend.nutsnews.com` and approve
   the `production-backend` gate.
5. Review the postcheck report before closing any maintenance issue.

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
