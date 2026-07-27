# Supabase Standby Failover

Issue #502 adds the protected manual failover workflow for moving production
from backend PostgreSQL primary to the existing production Supabase standby.

## Boundary

The failover workflow consumes a fresh, matching, unused, single-use #528
`GO` decision. It does not reimplement the lag, parity, schema, sequence,
writer-pause, or split-brain gates. Missing, stale, consumed, mismatched, or
`NO-GO` decision evidence blocks failover.

The workflow also validates the #504 recovery boundary contract before any
apply can be considered ready. After approved failover, Supabase is
authoritative and backend PostgreSQL cannot resume primary until rebuilt or
reconciled from Supabase.

This workflow does not create a Supabase project, create a `nutsnews-standby`
database, expose Supabase write credentials to app or worker traffic before
approved failover, write SQL, or print credentials, connection strings, raw
SQL, hostnames, project refs, or row data.

## Planned Provider Switch

When apply is ready, the plan records these required handoff actions:

- consume the #528 promotion decision for #502;
- switch the app production release to `supabase_primary` with production
  writes still paused;
- switch the worker pipeline to `supabase_primary` with production writes still
  paused and `deploy-supabase-primary` confirmation;
- run post-failover smoke in #503/#505 before production writers resume.

The protected #502 workflow artifact is safe metadata only. Cross-repo app and
worker deployment runs remain separate evidence because the app release path
requires a full Vercel production release payload and the worker path is owned
by `ramideltoro/nutsnews-worker`.

## Manual Run

Prove failover is blocked without a #528 decision:

```bash
gh workflow run backend-supabase-standby-failover.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=dry-run \
  -f promotion_decision_run_id= \
  -f failover_attempt_id=<attempt-id> \
  -f candidate_application_revision=<main-sha> \
  -f fence_epoch=<fence-epoch> \
  -f confirmation=plan-supabase-standby-failover \
  -f enforce=false
```

Model an apply after a fresh #528 `GO` decision:

```bash
gh workflow run backend-supabase-standby-failover.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=apply \
  -f promotion_decision_run_id=<promotion-decision-run-id> \
  -f failover_attempt_id=<attempt-id> \
  -f candidate_application_revision=<main-sha> \
  -f fence_epoch=<fence-epoch> \
  -f confirmation=execute-supabase-standby-failover \
  -f enforce=true
```

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_failover.py
python3 -m unittest tests.test_backend_supabase_standby_failover
python3 scripts/validate_backend_database_provider_switch.py
python3 scripts/validate_backend_supabase_standby_promotion_decision.py
python3 scripts/validate_backend_supabase_standby_recovery_boundaries.py
```

Required fixture coverage:

- missing #528 decision blocks dry-run and apply;
- `NO-GO`, expired, consumed, mismatched-attempt, mismatched-revision, and
  mismatched-fence-epoch decisions block apply;
- a valid #528 `GO` is apply-ready and would be consumed exactly once;
- artifacts and summaries remain safe metadata only;
- app/worker switch actions target `supabase_primary` and keep writes paused
  until post-failover smoke evidence exists.

## Acceptance Evidence

For #502, record:

- merged backend PR and main checks;
- a protected main-branch failover workflow run URL;
- operation, status, blocker codes, attempt, candidate revision, fence epoch,
  and decision id if present;
- confirmation that missing or non-`GO` #528 evidence blocks failover;
- confirmation that the target is existing production Supabase;
- confirmation that no new Supabase project and no `nutsnews-standby` database
  were created.
