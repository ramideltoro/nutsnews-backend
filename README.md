# NutsNews Backend

This repository owns the repeatable backend server setup for `backend.nutsnews.com` at `65.75.201.18`.

## Operating Model

Backend changes must follow:

```text
commit -> PR -> checks -> merge -> protected pipeline check -> approved pipeline apply -> read-only verification
```

Routine SSH is read-only verification only. Any server mutation must be represented in this repository and applied by the protected pipeline once it exists.

## Current Host Contract

| Item | Value |
| --- | --- |
| Hostname | `backend` |
| IPv4 | `65.75.201.18` |
| OS target | Ubuntu 26.04 LTS |
| SSH user for read-only verification | `rami` |
| SSH command | `ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18` |
| Backend domain target | `backend.nutsnews.com` |

## Chosen Runtime Shape

- Host configuration: Ansible.
- Normal apply path: protected GitHub Actions workflow with check mode before apply.
- Backend app runtime: Docker Compose, installed and managed by Ansible.
- Reverse proxy: Caddy, managed by Ansible, with public HTTP/HTTPS enabled only after DNS, TLS, firewall, and health checks are ready.
- Ops dashboard: static read-only dashboard served from generated JSON status snapshots and protected before public exposure.
- Database failover target: PostgreSQL design-first, deployed only after backup, access-control, and failover runbooks are reviewed.

No backend app, reverse proxy, dashboard, or database service is considered deployed until the protected pipeline applies the relevant configuration and read-only verification confirms the result.

## Repository Layout

```text
.
├── .github/workflows/   # Validation and protected apply workflows
├── ansible/             # Backend host automation
├── docs/                # Short backend-specific design notes
├── runbooks/            # Backend operation procedures
└── scripts/             # Local validation helpers
```

## Bootstrap Entry Point

Start with [runbooks/BACKEND_BOOTSTRAP.md](runbooks/BACKEND_BOOTSTRAP.md).

Use [runbooks/CREDENTIAL_BOOTSTRAP.md](runbooks/CREDENTIAL_BOOTSTRAP.md) to create or update the protected GitHub Environment and provider credential inventory.
Use [runbooks/PROTECTED_BACKEND_APPLY.md](runbooks/PROTECTED_BACKEND_APPLY.md) before running the protected backend Ansible workflow.
Use [runbooks/DRIFT_CHECK.md](runbooks/DRIFT_CHECK.md) to run the protected read-only drift report.
Use [runbooks/SSH_HARDENING.md](runbooks/SSH_HARDENING.md) before applying or verifying SSH hardening.
Use [runbooks/OS_MAINTENANCE.md](runbooks/OS_MAINTENANCE.md) before applying package updates or rebooting the backend host.
Use [runbooks/FIREWALL_BASELINE.md](runbooks/FIREWALL_BASELINE.md) before applying or verifying firewall policy.
Use [runbooks/CLOUDFLARE_ROUTING.md](runbooks/CLOUDFLARE_ROUTING.md) before applying or rolling back `backend.nutsnews.com` DNS.
Use [runbooks/FAIL2BAN_SSH.md](runbooks/FAIL2BAN_SSH.md) before applying or verifying SSH brute-force protection.
Use [runbooks/ABUSE_PROTECTION.md](runbooks/ABUSE_PROTECTION.md) before adding broader backend abuse protection.
Use [runbooks/SWAP_BASELINE.md](runbooks/SWAP_BASELINE.md) before applying or verifying the swap safety buffer.
Use [runbooks/CLOUD_INIT_PROVIDER_BOOTSTRAP.md](runbooks/CLOUD_INIT_PROVIDER_BOOTSTRAP.md) for the provider NoCloud deprecation warning notes.
Use [runbooks/BACKUP_RESTORE_BASELINE.md](runbooks/BACKUP_RESTORE_BASELINE.md) before adding any stateful backend workload.
Use [runbooks/MONITORING_BASELINE.md](runbooks/MONITORING_BASELINE.md) before applying or verifying monitoring and log retention.
Use [runbooks/BACKEND_HEALTH_REPORT.md](runbooks/BACKEND_HEALTH_REPORT.md) for the recurring read-only backend health report workflow.
Use [runbooks/BACKEND_SYNTHETIC_MONITORING.md](runbooks/BACKEND_SYNTHETIC_MONITORING.md) for off-box public endpoint synthetic monitoring.
Use [runbooks/BACKEND_CLEANUP_MAINTENANCE.md](runbooks/BACKEND_CLEANUP_MAINTENANCE.md) before running cleanup report, dry-run, or apply actions.
Use [runbooks/BACKEND_RECOVERY.md](runbooks/BACKEND_RECOVERY.md) before running fixed-purpose recovery checks or approved recovery actions.
Use [runbooks/BACKEND_OPENAI_MAINTENANCE_ROBOT.md](runbooks/BACKEND_OPENAI_MAINTENANCE_ROBOT.md) before changing the daily OpenAI maintenance issue robot.
Use [runbooks/SERVICE_BASELINE_ATTESTATION.md](runbooks/SERVICE_BASELINE_ATTESTATION.md) before adding optional backend services.
Use [runbooks/REDIS_VALKEY_DECISION.md](runbooks/REDIS_VALKEY_DECISION.md) before adding Redis or Valkey.
Use [runbooks/SEARCH_SERVICE_DECISION.md](runbooks/SEARCH_SERVICE_DECISION.md) before adding a dedicated search service.
Use [runbooks/POSTGRES_REPLACEMENT_PLAN.md](runbooks/POSTGRES_REPLACEMENT_PLAN.md) before replacing Supabase or installing production PostgreSQL.
Use [runbooks/SUPABASE_BACKEND_POSTGRES_PARITY.md](runbooks/SUPABASE_BACKEND_POSTGRES_PARITY.md) before changing the Supabase-to-backend PostgreSQL parity manifest.
Use [runbooks/DB_MIGRATION_CHANGE_FREEZE.md](runbooks/DB_MIGRATION_CHANGE_FREEZE.md) before changing database schema during the primary migration window.
Use [runbooks/DB_MIGRATION_ACCESS.md](runbooks/DB_MIGRATION_ACCESS.md) before running migration access, restore, validation, replication, or cutover workflows.

The current Ansible scaffold is intentionally narrow. It defines the host contract, runtime choice, validation commands, required secret boundaries, and rollback expectations that later issues will fill in through Ansible roles and protected workflows.

## Secret Boundaries

Required secret names may be documented here or in runbooks, but values must only live in GitHub repository or Environment secrets, a password manager, or another documented secret store.

Never commit:

- SSH private keys
- GitHub, Cloudflare, Supabase, backup, or database tokens
- database passwords or dumps
- `.env` files
- Terraform state or `.tfvars`
- generated server fact snapshots

## Validation

For documentation-only or scaffold changes:

```bash
git diff --check
```

For Ansible changes once roles are added:

```bash
cd ansible
ansible-playbook playbooks/bootstrap.yml --syntax-check
```

For protected applies, run check mode first and apply only after the protected environment approval gate.

The protected backend workflow is manual-only and uses the `production-backend` GitHub Environment.

For DB primary migration contract checks:

```bash
python3 scripts/validate_supabase_backend_postgres_parity.py
python3 scripts/check_supabase_migration_drift.py
python3 scripts/backend_postgres_migration_access_preflight.py --offline
```
