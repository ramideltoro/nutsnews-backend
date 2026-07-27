# Supabase Standby Recovery Boundaries

Issue #504 defines the abort, forward-recovery, and switch-back boundaries for
the protected backend PostgreSQL to Supabase standby failover path.

## Boundary

Before #502 switches provider mode, backend PostgreSQL remains the normal
production read/write primary. If an attempt aborts before the provider switch,
resume production writers only through the #526 writer-pause resume proof and
confirm app and worker Supabase writes were never enabled.

After #502 switches provider mode to Supabase, Supabase is authoritative. The
old backend PostgreSQL primary is stale by default. Backend PostgreSQL cannot
resume primary until it has been rebuilt or reconciled from authoritative
Supabase data and the switch-back gates pass.

This work does not create a Supabase project, create a `nutsnews-standby`
database, expose Supabase write credentials to app or worker traffic before
approved failover, write SQL, switch providers, or mutate production.

## Switch-Back Gates

Switch-back to backend PostgreSQL is a new protected promotion from
authoritative Supabase to a rebuilt or reconciled backend PostgreSQL candidate.
It requires:

- backend PostgreSQL rebuild or reconciliation from authoritative Supabase;
- required-table parity from Supabase to backend PostgreSQL;
- backend PostgreSQL sequence safety;
- no-split-brain evidence proving exactly one write-eligible provider;
- app, worker, scheduler, and admin writer pause evidence;
- staging drill evidence for recovery and negative paths;
- explicit owner approval and rollback boundary acceptance.

Missing, malformed, failing, unsafe, or mismatched evidence blocks switch-back.

## Manual Run

Render a non-mutating post-failover recovery boundary:

```bash
python3 scripts/backend_supabase_standby_recovery_boundaries.py \
  --boundary post_supabase_failover_forward_recovery \
  --provider-switch-performed true
```

Prove switch-back fails closed without the required evidence:

```bash
python3 scripts/backend_supabase_standby_recovery_boundaries.py \
  --boundary switch_back_to_backend_postgres \
  --provider-switch-performed true \
  --enforce
```

Protected workflow:

```bash
gh workflow run backend-supabase-standby-recovery-boundaries.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f boundary=switch_back_to_backend_postgres \
  -f provider_switch_performed=true \
  -f failover_attempt_id=<attempt-id> \
  -f fence_epoch=<fence-epoch> \
  -f confirmation=evaluate-supabase-standby-recovery-boundaries \
  -f enforce=false
```

The artifact is `backend-supabase-standby-recovery-boundaries.json`.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_recovery_boundaries.py
python3 -m unittest tests.test_backend_supabase_standby_recovery_boundaries
python3 scripts/validate_backend_database_provider_switch.py
python3 scripts/validate_backend_db_rollback_guardrails.py
```

Required fixture coverage:

- post-failover forward recovery treats Supabase as authoritative and blocks
  backend PostgreSQL reuse;
- switch-back without rebuild/reconciliation, parity, sequence, no-split-brain,
  writer-pause, staging-drill, and owner-approval evidence is blocked;
- switch-back with safe passing evidence is only dry-run ready and remains
  non-mutating;
- pre-switch abort is blocked after provider switch has completed;
- artifacts and summaries remain safe metadata only.

## Acceptance Evidence

For #504, record:

- merged backend PR and main checks;
- a protected main-branch recovery-boundary workflow run URL;
- boundary evaluated, status, blocker codes, attempt, and fence epoch;
- confirmation that the target is existing production Supabase;
- confirmation that no new Supabase project and no `nutsnews-standby` database
  were created;
- confirmation that backend PostgreSQL cannot resume primary after failover
  until rebuilt or reconciled from authoritative Supabase;
- confirmation that provider switching remains in #502 and was not performed.
