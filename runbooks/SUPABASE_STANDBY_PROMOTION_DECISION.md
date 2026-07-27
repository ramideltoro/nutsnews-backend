# Supabase Standby Promotion Decision

Issue #528 combines the six independent Supabase standby failover gates into
one protected, auditable `GO` or `NO-GO` decision.

## Boundary

The decision:

- consumes fresh artifacts from #522, #523, #524, #525, #526, and #527;
- requires every gate to be `PASS`;
- binds all evidence to the same `failover_attempt_id`;
- binds all evidence to the same source and target safe binding fingerprints;
- binds schema evidence to the candidate backend application revision;
- binds sequence, writer-pause, and split-brain evidence to the repository
  revision where reported;
- binds split-brain evidence to the same fence epoch;
- emits a short-lived, single-use decision artifact;
- writes safe metadata only.

The decision does not create a Supabase project, create a `nutsnews-standby`
database, expose Supabase write credentials to app or worker traffic before
approved failover, write SQL, switch providers, resume writers, or approve
production failover by itself.

Backend PostgreSQL remains the normal production read/write primary until #502
consumes a fresh single-use `GO` decision and runs its protected provider
switch workflow.

## Result

The artifact is `backend-supabase-standby-promotion-decision.json` and
includes:

- `decision` as `GO` or `NO-GO`;
- `decision_id`;
- `failover_attempt_id`;
- `candidate_application_revision`;
- `repository_revision`;
- `fence_epoch`;
- source and target binding fingerprints;
- per-gate safe status, blocker codes, fingerprints, measurement, and expiry;
- decision measurement and expiry time;
- `single_use=true`;
- `consumed=false`;
- `provider_switch_performed=false`;
- `safe_metadata_only=true`.

Any missing, stale, malformed, duplicate, replayed, mismatched, unavailable, or
failing evidence emits `NO-GO`.

## Manual Run

Run each required gate for the same attempt and current main revision, then run
the combined decision before any evidence expires. The writer-pause evidence is
short-lived, so run #526, #527, and this decision tightly together.

```bash
gh workflow run backend-supabase-standby-promotion-decision.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id=<attempt-id> \
  -f candidate_application_revision=<main-sha> \
  -f fence_epoch=<fence-epoch> \
  -f lag_gate_run_id=<lag-run-id> \
  -f parity_gate_run_id=<parity-run-id> \
  -f schema_gate_run_id=<schema-run-id> \
  -f sequence_gate_run_id=<sequence-run-id> \
  -f writer_pause_gate_run_id=<writer-pause-run-id> \
  -f split_brain_fence_gate_run_id=<split-brain-run-id> \
  -f confirmation=evaluate-standby-promotion-decision \
  -f enforce=false
```

Use `enforce=true` only when the caller requires a non-zero exit for `NO-GO`.
#502 must consume only a fresh `GO` decision.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_promotion_decision.py
python3 -m unittest tests.test_backend_supabase_standby_promotion_decision
python3 -m unittest tests.test_backend_database_provider_switch
```

Required fixture coverage:

- happy path returns `GO` only when all six gates pass;
- one failing fixture per required gate returns `NO-GO`;
- missing, stale, malformed, duplicate, replayed, mismatched-attempt,
  mismatched-target, mismatched-revision, and mismatched-epoch evidence returns
  `NO-GO`;
- a `GO` cannot be reused after expiry or consumption;
- provider-switch dry run rejects production switching without a fresh `GO`;
- artifacts and summaries remain safe metadata only.

## Acceptance Evidence

For #528, record:

- merged backend PR and main checks;
- a main-branch promotion-decision workflow run URL;
- the six gate run IDs consumed by the decision;
- decision, blockers, attempt, candidate revision, fence epoch, decision id,
  and expiry;
- confirmation that the target is existing production Supabase;
- confirmation that no new Supabase project and no `nutsnews-standby` database
  were created;
- confirmation that app/worker Supabase writes remained disabled before
  approved failover;
- confirmation that provider switching remains in #502 and was not performed.
