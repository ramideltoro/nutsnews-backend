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
- read-only ops dashboard collector
- PostgreSQL failover target and protected management dashboard
- backup, restore, monitoring, and verification tasks

## Inventory

Production inventory lives in `inventories/production/hosts.yml`.

The production host is intentionally addressed by IP until `backend.nutsnews.com` routing is implemented and verified through the Cloudflare issue.

## Validation

Syntax check:

```bash
ansible-playbook playbooks/bootstrap.yml --syntax-check
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
