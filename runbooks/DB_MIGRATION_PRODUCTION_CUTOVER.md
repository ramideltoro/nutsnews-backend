# Backend PostgreSQL Production Cutover

Issue: #116

Depends on #115 and #118.

## Purpose

Prepare the production cutover procedure before moving NutsNews writes from
Supabase to backend PostgreSQL.

Machine-readable plan:

```text
docs/backend-production-cutover-plan.json
```

Validators:

```bash
python3 scripts/validate_backend_production_cutover_plan.py
python3 scripts/backend_production_cutover_plan.py --operation dry-run
```

Protected workflow:

```bash
gh workflow run backend-production-cutover.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref db-primary-migration-production-cutover-runbook \
  -f operation=dry-run \
  -f confirmation=plan-production-cutover-only
```

## Preflight Gates

- backup freshness and restore proof;
- replication health or approved final dump path;
- parity validation;
- query smoke tests;
- API compatibility contract;
- provider switch contract;
- rollback guardrails;
- ops dashboard health;
- disk capacity;
- staging rehearsal evidence.

## Production Sequence

1. Announce maintenance window.
2. Run `preflight-only`.
3. Pause app and worker writers.
4. Verify writer pause.
5. Run final replication catch-up or final dump/restore.
6. Run parity validation and smoke tests.
7. Switch provider mode to `backend_postgres_primary`.
8. Monitor through the rollback window.
9. Record go/no-go and evidence.

## Abort Criteria

Abort if staging rehearsal evidence is missing, writer pause cannot be proven,
backup/replication/parity/smoke checks fail, dashboard health is critical, or
the rollback boundary is unclear.

## Current Blocker

Mutating operations are intentionally blocked in the current workflow scaffold
until #115 records staging rehearsal evidence.
