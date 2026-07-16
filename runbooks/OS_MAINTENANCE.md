# OS Maintenance Runbook

This runbook covers backend issue #2 for `65.75.201.18`.

## Acceptance Criteria

- `apt list --upgradable` is empty or only contains explicitly deferred packages.
- `uname -r` shows the expected updated kernel.
- `/var/run/reboot-required` is absent after reboot.
- SSH key login as `rami` still works after reboot.
- No failed systemd units are present after reboot.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The maintenance tasks:

- refresh apt metadata;
- apply available package upgrades with `upgrade: dist`;
- remove unused packages and clean the cache;
- reboot when `/var/run/reboot-required` exists during real apply mode;
- capture package, kernel, reboot-required, and failed-unit evidence in the workflow logs.

The reboot is skipped in Ansible check mode.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed OS maintenance PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review the package and reboot plan.
4. Run `apply` mode only during an approved maintenance window.
5. Wait for Ansible to reconnect after any reboot.
6. Run the verification commands below before considering #2 complete.

## Verification

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'uname -r'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'test ! -e /var/run/reboot-required && echo no-reboot-required'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'apt list --upgradable 2>/dev/null'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'systemctl --failed --no-pager'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
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
