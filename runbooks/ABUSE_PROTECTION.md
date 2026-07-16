# Backend Abuse Protection Decision

This runbook covers backend issue #24 for `65.75.201.18`.

## Acceptance Criteria

- A narrow implementation PR installs/configures the selected tool through Ansible/GitOps or records a documented decision not to install.
- Enforcement policy has allowlists, test cases, and rollback instructions.
- False-positive-sensitive routes such as auth/admin are covered by tests or documented probes.
- Live verification is read-only until a later approved apply.
- No `Protected Ansible Apply`, restart, firewall mutation, or production enforcement change is run without separate explicit approval.

## Current Host State

The 2026-07-16 service-baseline attestation found SSH as the only public service.

Not deployed yet:

- Caddy
- Docker
- public HTTP
- public HTTPS
- backend app service
- ops dashboard

Because there is no HTTP listener, no Caddy access log, and no deployed app/admin route behavior, HTTP abuse blocking would be speculative and false-positive-prone.

## Decision

Use fail2ban for the current SSH-only phase.

Defer CrowdSec and HTTP/Caddy enforcement until a later reviewed PR adds Caddy, backend app health routes, app/admin probes, and real log sources.

Machine-readable record:

```text
docs/backend-abuse-protection-decision.json
```

Validator:

```bash
python3 scripts/validate_abuse_protection_decision.py
```

The validator intentionally fails if the service baseline starts listing HTTP/Caddy as deployed while this decision still says SSH-only.

## CrowdSec Versus Fail2ban

| Area | Fail2ban now | CrowdSec later |
| --- | --- | --- |
| Current fit | Strong for SSH auth logs | Weak until Caddy/app logs exist |
| Resource use | Low | Higher agent/parser footprint |
| False-positive risk | Low with SSH-only jail | Depends on HTTP routes and allowlists |
| UFW integration | Existing backend role already manages UFW and fail2ban separately | Needs bouncer policy and rollback path |
| Metrics | Basic jail status now | Better later if dashboards and labels are designed |
| Maintenance | Small config surface | More moving parts and upstream scenario updates |

## Allowlist Policy

Current SSH jail allowlist:

```text
127.0.0.1/8
::1
```

Do not commit personal/operator home IPs unless there is a reviewed operational reason. Prefer a reviewed PR or protected secret-backed variable for trusted operator ranges.

Future HTTP abuse enforcement must explicitly account for:

- Cloudflare edge ranges from the provider-managed source;
- GitHub Actions callbacks only where the route is documented;
- uptime probe provider names and source ranges when available;
- operator/admin access boundaries.

## False-Positive-Sensitive Probes

Before any HTTP blocking mode is enabled, document or automate probes for:

- `/health`
- `/healthz`
- `/readyz`
- auth provider redirects/callbacks
- admin redirects and dashboard access boundary

Initial HTTP mode should be detection/report-only. Blocking can start only after probe evidence shows no impact to health, auth, admin navigation, or normal release checks.

## Safe Tests

Current non-mutating checks:

```bash
python3 scripts/validate_abuse_protection_decision.py
cd ansible
ansible-playbook playbooks/bootstrap.yml --syntax-check -i inventories/production/hosts.yml
```

After later approved fail2ban apply:

```bash
sudo fail2ban-client status sshd
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
```

Controlled ban testing must use a disposable test source IP and immediately unban it.

## Rollback

Preferred rollback is a git revert followed by protected apply.

Unban one IP:

```bash
sudo fail2ban-client set sshd unbanip <ip-address>
```

Break-glass stop only if operator SSH access is at risk:

```bash
sudo systemctl stop fail2ban
```

Any break-glass action must be documented and reconciled back into this repository.
