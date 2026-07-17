# Backend Abuse Protection Decision

This runbook covers backend issues #24 and #40 for `65.75.201.18`.

## Acceptance Criteria

- A narrow implementation PR installs/configures the selected tool through Ansible/GitOps or records a documented decision not to install.
- Enforcement policy has allowlists, test cases, and rollback instructions.
- False-positive-sensitive routes such as auth/admin are covered by tests or documented probes.
- Live verification is read-only until a later approved apply.
- No `Protected Ansible Apply`, restart, firewall mutation, or production enforcement change is run without separate explicit approval.

## Current Host State

The current service-baseline attestation allows SSH plus the Caddy-managed
HTTP/HTTPS `/healthz` endpoint.

Read-only verification on 2026-07-17 showed:

- `ssh`, `ufw`, `fail2ban`, `caddy`, and `alloy` are active;
- `backend.nutsnews.com/healthz` returns `ok`;
- public `/` and `/readyz` return `404` while the backend app is absent;
- `/var/log/auth.log` and `/var/log/fail2ban.log` exist as `adm`-owned logs
  and are collected by Alloy/Loki;
- `/var/log/caddy/access.log` and `/var/log/caddy/error.log` are not present
  yet because there is no deployed backend app route with access logging.

Not deployed yet:

- Docker
- backend app service
- ops dashboard

Because there is no deployed backend app/admin route behavior, HTTP abuse
blocking would still be speculative and false-positive-prone. The health route
must remain observable and unblocked.

## Decision

Use fail2ban for SSH in the current phase.

Add report-only Grafana/Loki detection for:

- SSH authentication failure spikes;
- fail2ban SSH ban events.

Defer CrowdSec, Cloudflare blocking rules, and HTTP/Caddy enforcement until a
later reviewed PR adds backend app routes, app/admin probes, and real route log
sources.

Machine-readable record:

```text
docs/backend-abuse-protection-decision.json
```

Validator:

```bash
python3 scripts/validate_abuse_protection_decision.py
```

The validator intentionally fails if the service baseline starts exposing ports
outside SSH plus HTTP/HTTPS health routing while this decision is still in the
observe-only HTTP phase.

Grafana detection rules:

```text
nn-backend-ssh-auth-spike
nn-backend-fail2ban-ban-events
```

These rules use low-cardinality Loki queries scoped to
`host="backend.nutsnews.com"` and `service="security"`. They do not label,
group, or route on IP address, path, user, request ID, or raw message text.

## CrowdSec Versus Fail2ban

| Area | Fail2ban now | CrowdSec later |
| --- | --- | --- |
| Current fit | Strong for SSH auth logs | Weak until Caddy/app logs exist |
| Resource use | Low | Higher agent/parser footprint |
| False-positive risk | Low with SSH-only jail | Depends on HTTP routes and allowlists |
| UFW integration | Existing backend role already manages UFW and fail2ban separately | Needs bouncer policy and rollback path |
| Metrics | Basic jail status now | Better later if dashboards and labels are designed |
| Maintenance | Small config surface | More moving parts and upstream scenario updates |

## Report-Only Maintenance Automation

The #40 implementation is detection/report-only. It does not mutate UFW, Caddy,
Cloudflare, fail2ban jail behavior, or provider firewall policy.

Detection runs through the existing `Backend Grafana Observability` workflow:

```bash
gh workflow run backend-grafana-metrics.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=apply \
  -f confirm_apply=backend.nutsnews.com

gh workflow run backend-grafana-metrics.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=verify
```

The workflow uses the `production-backend` Environment for Grafana credentials.
It does not need SSH or host mutation for these detection rules.

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
python3 scripts/provision_grafana_metrics.py --check
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

For #40 Grafana-only detection changes, preferred rollback is:

1. Revert the Grafana spec/runbook PR.
2. Run `Backend Grafana Observability` with `action=apply`.
3. Run `Backend Grafana Observability` with `action=verify`.

Unban one IP:

```bash
sudo fail2ban-client set sshd unbanip <ip-address>
```

Break-glass stop only if operator SSH access is at risk:

```bash
sudo systemctl stop fail2ban
```

Any break-glass action must be documented and reconciled back into this repository.
