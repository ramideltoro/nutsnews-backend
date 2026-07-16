# Swap Baseline Runbook

This runbook covers backend issue #5 for `65.75.201.18`.

## Acceptance Criteria

- `swapon --show` shows the chosen swap device.
- `free -h` shows the configured buffer.
- The setting persists after reboot.
- The repo documents why swap was chosen and how to disable it.
- No production data or secrets are introduced as part of this change.

## Decision

Use a 2 GiB disk swapfile at `/swapfile`.

Why swapfile instead of zram:

- The host has about 77 GiB root disk with ample free space.
- A swapfile is simple to inspect, persist, disable, and recover.
- It avoids adding another package or generator before backend workloads exist.
- The low `vm.swappiness = 10` setting makes swap a safety buffer, not normal working memory.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The swap tasks:

- create `/swapfile` with size `2048M`;
- set root ownership and `0600` permissions;
- format it with `mkswap` when newly created;
- add it to `/etc/fstab`;
- enable it during real apply mode;
- write `/etc/sysctl.d/99-nutsnews-backend-swap.conf`;
- capture `swapon --show` and `free -h` in workflow logs.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed swap baseline PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review the planned swapfile creation.
4. Run `apply` mode after the `production-backend` approval gate.
5. Verify swap state before considering #5 complete.

## Verification

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'swapon --show'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'free -h'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'grep -E "^/swapfile\\s+none\\s+swap\\s+" /etc/fstab'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'cat /proc/sys/vm/swappiness'
```

After the next reboot, repeat `swapon --show` and `free -h` to confirm persistence.

## Disable Or Roll Back

Preferred rollback is a git revert followed by protected apply.

Break-glass disable steps, only if swap causes an immediate problem:

```bash
sudo swapoff /swapfile
sudo sed -i.bak '\|^/swapfile\s\+none\s\+swap\s\+|d' /etc/fstab
sudo rm -f /etc/sysctl.d/99-nutsnews-backend-swap.conf
sudo sysctl --system
```

Do not delete `/swapfile` until the rollback is verified. Record any break-glass command and reconcile the final state back into this repository.
