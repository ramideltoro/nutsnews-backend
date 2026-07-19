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

## Current Status

The backend database gates are complete, and the app/worker writer-pause
controls have now been deployed in production without changing the database
provider:

- app readiness reports `productionWritesPaused=true` and
  `databaseProviderMode=supabase_primary`;
- the Vercel production release run
  `https://github.com/ramideltoro/nutsnews/actions/runs/29704129436` promoted
  commit `936062eee2ed097817a81f881920faa9808c2fac` with
  `PRODUCTION_WRITES_PAUSED=true`;
- the worker pipeline run
  `https://github.com/ramideltoro/nutsnews-worker/actions/runs/29703213882`
  deployed all 25 worker shards with
  `NUTSNEWS_PRODUCTION_WRITES_PAUSED=true`;
- backend provider-shadow dry-run
  `https://github.com/ramideltoro/nutsnews-backend/actions/runs/29705168086`
  remains non-mutating and ready;
- production `backend_postgres_primary` still fails closed outside the
  protected cutover workflow with
  `production_switch_requires_protected_cutover_workflow`;
- final catch-up/rollback-window guardrails still fail closed until live
  writer-pause and Supabase no-new-write watermark evidence is attached.

Mutating cutover operations remain blocked until coordinated production
cutover ownership exists:

- maintenance window approval;
- verified app, worker, scheduler, and admin writer-pause evidence, including
  Supabase no-new-write watermarks after the pause;
- app and worker provider switch owner approval;
- final go/no-go owner approval;
- rollback owner coverage through the rollback window.
- `production-backend` approval for refreshed DB gate workflows and the
  eventual protected cutover run.

Supabase remains the production writer until the protected cutover is explicitly
approved and executed.
