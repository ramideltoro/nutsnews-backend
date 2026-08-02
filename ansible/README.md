# Backend Ansible

Ansible is the chosen configuration tool for the backend host at `65.75.201.18`.

## Scope

This tree will own backend host configuration that must be repeatable:

- OS package and security update baseline
- SSH hardening
- UFW firewall policy
- SSH brute-force protection
- swap or zram safety buffer
- Docker Compose runtime
- Caddy reverse proxy
- authenticated, loopback-isolated Ollama/Qwen runtime for Wiki automation
- read-only ops dashboard collector
- PostgreSQL failover target and protected management dashboard
- backup, restore, monitoring, and verification tasks
- restricted Supabase standby forced-command probe
- backend-to-existing-Supabase standby sync relay

## Inventory

Production inventory lives in `inventories/production/hosts.yml`.

The production host is intentionally addressed by IP until `backend.nutsnews.com` routing is implemented and verified through the Cloudflare issue.

The Supabase standby forced-command probe targets only the existing production
backend host through `playbooks/supabase_standby_probe.yml`. It provisions the
locked `nutsnews-standby-probe` identity, root-owned fixed probe program,
protected expected Supabase target config, forced `authorized_keys` entry, and
an sshd `Match User` boundary. It must run through the protected
`production-backend` workflow path, with check mode reviewed before apply. The
dedicated workflow is `.github/workflows/supabase-standby-probe.yml`.

The retired disposable Supabase standby IPv6 runner design must not be
reintroduced. This repository no longer maintains a runner inventory, runner
playbook, runner role, or runner lifecycle workflow for that path.

The backend-to-existing-Supabase sync relay is installed by the main protected
backend apply path when `NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED=true`.
It runs as `nutsnews-standby-relay` under
`nutsnews-supabase-sync-relay.timer`, reads backend PostgreSQL over loopback,
and writes outbound to the existing production Supabase standby. Its env file
is root-owned and group-readable only by the relay user; app and worker
services do not receive Supabase write credentials.

## Validation

Syntax check:

```bash
ansible-playbook playbooks/bootstrap.yml --syntax-check
ansible-playbook playbooks/supabase_standby_probe.yml --syntax-check
```

`ansible.cfg` sets `roles_path = roles` so `playbooks/bootstrap.yml` resolves repository-owned roles from `ansible/roles` in both local checks and the protected GitHub Actions workflow.

Dry-run against the backend server must happen only through the protected workflow once issue #10 adds it.

Do not run mutating playbooks directly from an operator laptop as the normal path.

## Protected Workflow

GitHub Actions can run this playbook through `.github/workflows/protected-backend-ansible-apply.yml`.

- The workflow is `workflow_dispatch` only.
- The default run mode is `check`.
- Apply mode requires `confirm_apply` to equal `backend.nutsnews.com`.
- The job uses the `production-backend` GitHub Environment.
- Required secrets are documented in `../runbooks/PROTECTED_BACKEND_APPLY.md`.

The Wiki AI runtime is installed only by this protected baseline. It pins the
reviewed Ollama archive and checksum, pulls the pinned `qwen3.5:4b-q4_K_M`
model identity, creates the `nutsnews-wiki-qwen` 65,536-token alias, and binds
both Ollama and its authenticated proxy to loopback. Caddy publishes only
`/wiki-ai/health` and `/wiki-ai/v1/responses`; raw Ollama management routes are
not exposed. The proxy permits one active inference plus one authenticated,
bounded waiter so a Codex follow-up can bridge a finishing stream; additional
overlap fails with `429`.
