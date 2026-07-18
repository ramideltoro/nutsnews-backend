# Backend PostgreSQL Production Cutover

Issue: #119

Depends on #211, #212, #213, #214, #215, #216, and #217.

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
  --ref main \
  -f operation=dry-run \
  -f confirmation=plan-production-cutover-only
```

## Preflight Gates

- primary shadow restore proof;
- primary shadow backup freshness and restore proof;
- production logical replication health;
- production shadow object and behavior parity validation;
- query smoke tests;
- API compatibility contract;
- provider switch contract;
- writer pause evidence across app, worker, scheduler, and admin paths;
- rollback guardrails;
- ops dashboard health;
- disk capacity;
- maintenance window and rollback owner confirmation.

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

The backend database gates are complete, but mutating cutover operations remain
blocked in the current workflow scaffold until coordinated production cutover
ownership exists outside this backend DB-only path:

- maintenance window approval;
- app, worker, scheduler, and admin writer pause evidence;
- app and worker provider switch owner approval;
- final go/no-go owner approval;
- rollback owner coverage through the rollback window.

Supabase remains the production writer until the protected cutover is explicitly
approved and executed.
