# NutsNews Backend

Backend-owned operational runbooks include the zero-cost Supabase standby
forced-command probe boundary for the protected app readiness workflow:

- [Supabase Standby Forced-Command Probe](runbooks/SUPABASE_STANDBY_PROBE_BOUNDARY.md)
- [Supabase Standby Split-Brain Fence Gate](runbooks/SUPABASE_STANDBY_SPLIT_BRAIN_FENCE_GATE.md)

This supersedes the disposable IPv6 runner design. The old runner runbook is
retired along with the obsolete runner workflow, Ansible role, playbook,
inventory, and boundary record.

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
Use [runbooks/BACKEND_SYNTHETIC_MONITORING.md](runbooks/BACKEND_SYNTHETIC_MONITORING.md) for off-box public endpoint and protected admin backend operation synthetic monitoring.
Use [runbooks/BACKEND_CLEANUP_MAINTENANCE.md](runbooks/BACKEND_CLEANUP_MAINTENANCE.md) before running cleanup report, dry-run, or apply actions.
Use [runbooks/BACKEND_RECOVERY.md](runbooks/BACKEND_RECOVERY.md) before running fixed-purpose recovery checks or approved recovery actions.
Use [runbooks/BACKEND_OPENAI_MAINTENANCE_ROBOT.md](runbooks/BACKEND_OPENAI_MAINTENANCE_ROBOT.md) before changing the daily OpenAI maintenance issue robot.
Use [runbooks/SERVICE_BASELINE_ATTESTATION.md](runbooks/SERVICE_BASELINE_ATTESTATION.md) before adding optional backend services.
Use [runbooks/REDIS_VALKEY_DECISION.md](runbooks/REDIS_VALKEY_DECISION.md) before adding Redis or Valkey.
Use [runbooks/SEARCH_SERVICE_DECISION.md](runbooks/SEARCH_SERVICE_DECISION.md) before adding a dedicated search service.
Use [runbooks/WORKER_UPLIFT_RABBITMQ_CAPACITY_SECURITY.md](runbooks/WORKER_UPLIFT_RABBITMQ_CAPACITY_SECURITY.md) before adding RabbitMQ for the worker-uplift runtime.
Use [runbooks/WORKER_UPLIFT_SECURITY_REVIEW.md](runbooks/WORKER_UPLIFT_SECURITY_REVIEW.md) to validate and refresh the non-mutating worker-uplift security review.
Use [runbooks/WORKER_UPLIFT_PRODUCTION_READINESS_DECISION.md](runbooks/WORKER_UPLIFT_PRODUCTION_READINESS_DECISION.md) to interpret and refresh the non-mutating worker-uplift GO/NO-GO evidence.
Use [runbooks/WORKER_UPLIFT_STAGE_HEALTH_PROJECTION.md](runbooks/WORKER_UPLIFT_STAGE_HEALTH_PROJECTION.md) to build, apply, and verify the bounded eight-row worker-uplift admin health projection.
Use [runbooks/WORKER_UPLIFT_RABBITMQ_PROVISIONING.md](runbooks/WORKER_UPLIFT_RABBITMQ_PROVISIONING.md) before applying RabbitMQ Docker Compose provisioning.
Use [runbooks/POSTGRES_REPLACEMENT_PLAN.md](runbooks/POSTGRES_REPLACEMENT_PLAN.md) before replacing Supabase or installing production PostgreSQL.
Use [runbooks/SUPABASE_BACKEND_POSTGRES_PARITY.md](runbooks/SUPABASE_BACKEND_POSTGRES_PARITY.md) before changing the Supabase-to-backend PostgreSQL parity manifest.
Use [runbooks/DB_MIGRATION_CHANGE_FREEZE.md](runbooks/DB_MIGRATION_CHANGE_FREEZE.md) before changing database schema during the primary migration window.
Use [runbooks/DB_MIGRATION_ACCESS.md](runbooks/DB_MIGRATION_ACCESS.md) before running migration access, restore, validation, replication, or cutover workflows.
Use [runbooks/DB_MIGRATION_LOGICAL_REPLICATION.md](runbooks/DB_MIGRATION_LOGICAL_REPLICATION.md) before configuring Supabase-to-backend PostgreSQL logical replication.
Use [runbooks/DB_MIGRATION_REPLICATION_HEALTH.md](runbooks/DB_MIGRATION_REPLICATION_HEALTH.md) before checking or alerting on logical replication health.
Use [runbooks/DB_MIGRATION_SMOKE_TESTS.md](runbooks/DB_MIGRATION_SMOKE_TESTS.md) before running backend PostgreSQL query smoke tests.
Use [runbooks/DB_MIGRATION_API_COMPATIBILITY.md](runbooks/DB_MIGRATION_API_COMPATIBILITY.md) before changing app or worker database provider compatibility.
Use [runbooks/DB_MIGRATION_PROVIDER_SWITCH.md](runbooks/DB_MIGRATION_PROVIDER_SWITCH.md) before changing provider switch modes or rollback behavior.
Use [runbooks/DB_MIGRATION_ROLLBACK_FAILBACK.md](runbooks/DB_MIGRATION_ROLLBACK_FAILBACK.md) before changing rollback, failback, or single-writer guardrails.
Use [runbooks/DB_MIGRATION_PRODUCTION_CUTOVER.md](runbooks/DB_MIGRATION_PRODUCTION_CUTOVER.md) before running production cutover planning or protected cutover workflows.
Use [runbooks/DB_MIGRATION_BENCHMARK_TUNING.md](runbooks/DB_MIGRATION_BENCHMARK_TUNING.md) before benchmarking or tuning backend PostgreSQL for primary workload.
Use [runbooks/SUPABASE_PLATFORM_PARITY.md](runbooks/SUPABASE_PLATFORM_PARITY.md) before changing Supabase Auth, Storage, Realtime, Edge Function, Data API, or API key cutover decisions.
Use [runbooks/SUPABASE_STANDBY_PROBE_BOUNDARY.md](runbooks/SUPABASE_STANDBY_PROBE_BOUNDARY.md) before changing the protected Supabase standby readiness probe.
Use [runbooks/SUPABASE_STANDBY_SYNC_RELAY.md](runbooks/SUPABASE_STANDBY_SYNC_RELAY.md) before enabling, proving, or rolling back the backend-to-Supabase standby sync relay.
Use [runbooks/SUPABASE_STANDBY_LAG_GATE.md](runbooks/SUPABASE_STANDBY_LAG_GATE.md) before evaluating or consuming the Supabase standby lag gate.
Use [runbooks/SUPABASE_STANDBY_PARITY_GATE.md](runbooks/SUPABASE_STANDBY_PARITY_GATE.md) before evaluating or consuming the Supabase standby required-table parity gate.
Use [runbooks/SUPABASE_STANDBY_SCHEMA_GATE.md](runbooks/SUPABASE_STANDBY_SCHEMA_GATE.md) before evaluating or consuming the Supabase standby schema compatibility gate.
Use [runbooks/SUPABASE_STANDBY_SEQUENCE_GATE.md](runbooks/SUPABASE_STANDBY_SEQUENCE_GATE.md) before evaluating or consuming the Supabase standby sequence safety gate.
Use [runbooks/SUPABASE_STANDBY_WRITER_PAUSE_GATE.md](runbooks/SUPABASE_STANDBY_WRITER_PAUSE_GATE.md) before evaluating or consuming the Supabase standby writer pause and quiescence gate.
Use [runbooks/SUPABASE_STANDBY_PROMOTION_DECISION.md](runbooks/SUPABASE_STANDBY_PROMOTION_DECISION.md) before evaluating or consuming the Supabase standby promotion `GO`/`NO-GO` decision.
Use [runbooks/SUPABASE_STANDBY_RECOVERY_BOUNDARIES.md](runbooks/SUPABASE_STANDBY_RECOVERY_BOUNDARIES.md) before changing abort, forward-recovery, or switch-back policy after a Supabase standby failover.
Use [runbooks/SUPABASE_STANDBY_FAILOVER.md](runbooks/SUPABASE_STANDBY_FAILOVER.md) before planning or applying the protected Supabase standby failover workflow.
Use [runbooks/SUPABASE_STANDBY_STAGING_FAILOVER_DRILL.md](runbooks/SUPABASE_STANDBY_STAGING_FAILOVER_DRILL.md) before running the protected staging failover drill.
Use [runbooks/SUPABASE_STANDBY_PRODUCTION_ACCEPTANCE.md](runbooks/SUPABASE_STANDBY_PRODUCTION_ACCEPTANCE.md) before recording the production standby soak and acceptance `GO`/`NO-GO` decision.
Use [runbooks/SUPABASE_CLEANUP_RETENTION_POLICY.md](runbooks/SUPABASE_CLEANUP_RETENTION_POLICY.md) before changing post-cutover Supabase cleanup or retirement policy.

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
python3 scripts/backend_postgres_parity_validate.py --offline
python3 scripts/validate_backend_api_compatibility_contract.py
python3 scripts/validate_backend_database_provider_switch.py
python3 scripts/validate_backend_db_rollback_guardrails.py
python3 scripts/validate_backend_supabase_standby_recovery_boundaries.py
python3 scripts/validate_backend_supabase_standby_failover.py
python3 scripts/validate_backend_supabase_standby_staging_failover_drill.py
python3 scripts/validate_backend_supabase_standby_production_acceptance.py
python3 scripts/validate_backend_supabase_cleanup_retention_policy.py
python3 scripts/validate_backend_production_cutover_plan.py
python3 scripts/validate_backend_postgres_logical_replication_plan.py
python3 scripts/backend_postgres_logical_replication_source.py --operation status
python3 scripts/backend_postgres_replication_health.py --offline
python3 scripts/backend_postgres_smoke_tests.py --offline
python3 scripts/backend_app_db_api_smoke.py --offline
python3 scripts/backend_postgres_benchmark_tuning.py --offline
python3 scripts/validate_backend_postgres_backup_proof.py
```
