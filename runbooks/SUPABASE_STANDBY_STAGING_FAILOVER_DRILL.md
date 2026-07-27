# Supabase Standby Staging Failover Drill

Issue #503 adds the protected staging drill for the backend PostgreSQL primary
to existing production Supabase standby failover path.

## Boundary

The drill rehearses backend PostgreSQL unavailable, runs the #502 protected
failover dry-run plan, then performs a staging-only apply simulation. It proves
that staging reads and controlled writes target `supabase_primary`, write pause
is preserved, no split brain exists, and backend PostgreSQL receives no writes
after failover.

This drill uses the same intended target label as production failover:
existing production Supabase. It does not create a Supabase project, create a
`nutsnews-standby` database, switch production provider mode, resume production
writers, or mutate production.

## Manual Run

Dry-run the drill from `main`:

```bash
gh workflow run backend-supabase-standby-staging-failover-drill.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=dry-run \
  -f failover_attempt_id=<attempt-id> \
  -f candidate_application_revision=<main-sha> \
  -f fence_epoch=<fence-epoch> \
  -f confirmation=plan-staging-supabase-failover-drill \
  -f enforce=true
```

Run the staging-apply drill:

```bash
gh workflow run backend-supabase-standby-staging-failover-drill.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=staging-apply \
  -f failover_attempt_id=<attempt-id> \
  -f candidate_application_revision=<main-sha> \
  -f fence_epoch=<fence-epoch> \
  -f confirmation=execute-staging-supabase-failover-drill \
  -f enforce=true
```

The artifact is `backend-supabase-standby-staging-failover-drill`. It contains
the #502 dry-run plan and the #503 staging drill report.

## Required Evidence

Record these fields on #503 before moving to #505:

- workflow run URL and backend main revision;
- operation `staging-apply` and status `PASS`;
- provider mode `supabase_primary`;
- write pause `true`;
- no split brain with exactly one write-eligible provider;
- backend PostgreSQL write delta `0`;
- public read smoke and controlled write smoke results;
- confirmation that the target is existing production Supabase;
- confirmation that no new Supabase project or `nutsnews-standby` database was
  created;
- confirmation that no production mutation or production provider switch was
  performed by the drill.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_staging_failover_drill.py
python3 -m unittest tests.test_backend_supabase_standby_staging_failover_drill
python3 scripts/validate_backend_supabase_standby_failover.py
python3 scripts/validate_backend_supabase_standby_recovery_boundaries.py
```

Required fixture coverage:

- missing #502 dry-run evidence blocks staging apply;
- mismatched attempt, revision, or fence epoch blocks staging apply;
- wrong staging confirmation blocks staging apply;
- backend PostgreSQL available or nonzero backend write delta blocks the drill;
- provider mode other than `supabase_primary` blocks the drill;
- failed public reads, controlled writes, write pause, or no split brain blocks
  the drill;
- artifacts and summaries remain safe metadata only.
