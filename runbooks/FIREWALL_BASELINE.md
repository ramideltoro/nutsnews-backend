# Firewall Baseline Runbook

This runbook covers backend issue #3 for `65.75.201.18`.

## Acceptance Criteria

- `sudo ufw status verbose` shows a clear default-deny incoming policy.
- Only intended public ports are allowed.
- RabbitMQ AMQP `5672`, management `15672`, and Prometheus `15692` must remain private and must not appear in UFW public allow rules.
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
- allow TCP `22` for SSH;
- allow TCP `80` for backend HTTP health and ACME;
- allow TCP `443` for backend HTTPS health;
- do not allow RabbitMQ TCP `5672`, `15672`, or `15692` on any public interface;
- enable UFW and record `ufw status verbose` in workflow logs.

## Current Public Ports

| Port | Protocol | Purpose |
| --- | --- | --- |
| `22` | TCP | SSH |
| `80` | TCP | HTTP health and ACME |
| `443` | TCP | HTTPS health |

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed firewall baseline PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Confirm the plan preserves SSH and enables only reviewed HTTP/HTTPS health routing.
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

RabbitMQ public scan:

```bash
for port in 5672 15672 15692; do
  nc -vz -w5 65.75.201.18 "$port"
done
```

Expected result: every RabbitMQ port is refused or times out from outside the
host. The protected deployment safety gate also runs this public scan and fails
post-apply when any RabbitMQ port opens externally.

## Rollback

Preferred rollback is a git revert followed by protected apply.

Break-glass rollback, only if SSH or firewall access is broken:

```bash
sudo ufw allow 22/tcp
sudo ufw reload
sudo ufw status verbose
```

Record any break-glass command and reconcile the final state back into this repository.
