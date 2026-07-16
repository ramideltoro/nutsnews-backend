# Firewall Baseline Runbook

This runbook covers backend issue #3 for `65.75.201.18`.

## Acceptance Criteria

- `sudo ufw status verbose` shows a clear default-deny incoming policy.
- Only intended public ports are allowed.
- IPv6 exposure is intentional and matches the documented policy.
- A read-only verification command is documented for future audits.
- SSH access remains available after any firewall reload.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The firewall tasks:

- install UFW;
- set `IPV6=yes` in `/etc/default/ufw`;
- reset existing UFW rules during approved apply;
- set default incoming policy to deny;
- set default outgoing policy to allow;
- allow only TCP `22` for SSH in the current phase;
- keep HTTP/HTTPS disabled until the reverse proxy and routing issues are ready;
- enable UFW and record `ufw status verbose` in workflow logs.

## Current Public Ports

| Port | Protocol | Purpose |
| --- | --- | --- |
| `22` | TCP | SSH |

Future ports `80/tcp` and `443/tcp` are documented in Ansible defaults but disabled until explicitly enabled by a reviewed PR.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed firewall baseline PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Confirm the plan preserves SSH and does not enable HTTP/HTTPS yet.
4. Keep provider console or an existing privileged session available.
5. Run `apply` mode after the `production-backend` approval gate.
6. Verify SSH access and firewall state before considering #3 complete.

## Verification

Privileged firewall check:

```bash
sudo ufw status verbose
```

Read-only listener check:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'ss -tulpen 2>/dev/null || ss -tulpn'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
```

Expected result: SSH remains reachable, UFW defaults to deny incoming and allow outgoing, and only intended public ports are allowed.

## Rollback

Preferred rollback is a git revert followed by protected apply.

Break-glass rollback, only if SSH or firewall access is broken:

```bash
sudo ufw allow 22/tcp
sudo ufw reload
sudo ufw status verbose
```

Record any break-glass command and reconcile the final state back into this repository.
