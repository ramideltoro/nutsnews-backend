# Supabase Standby Production Acceptance

Issue #505 records the final production `GO` or `NO-GO` decision for treating
the existing production Supabase database as the official hot standby backup
target for backend PostgreSQL primary.

This gate does not switch providers. Backend PostgreSQL remains the production
read/write primary unless a later protected failover run consumes a fresh #528
GO decision and completes the #502 failover workflow.

## Required Evidence

The acceptance report must show all of the following:

- Standby sync has soaked for at least 24 hours.
- Relay health is healthy.
- Replication lag <= 30 seconds throughout the accepted window.
- No critical backend health checks are present.
- Required parity passes.
- At least one protected production failover dry-run is accepted.
- The #503 staging failover drill evidence is accepted.
- The owner records an explicit `GO` or `NO-GO`.

All evidence must be safe metadata only. Do not publish credentials, connection
strings, Supabase host or project metadata, raw SQL, row data, or secret values.

## Safety Boundary

- Target: existing production Supabase.
- Do not create a new Supabase project.
- Do not create a `nutsnews-standby` database.
- Do not send app or worker writes to Supabase before approved failover.
- Do not mutate production from this acceptance gate.
- A `GO` acceptance marks standby backup readiness only.
- Actual failover still requires a fresh #528 GO decision consumed by #502.

## Local Validation

Run these before opening a PR:

```bash
python3 scripts/validate_backend_supabase_standby_production_acceptance.py
python3 -m unittest tests.test_backend_supabase_standby_production_acceptance
python3 scripts/backend_supabase_standby_production_acceptance.py \
  --operation acceptance \
  --confirmation record-production-standby-acceptance \
  --owner-decision GO \
  --fixture-pass \
  --enforce
```

## Protected Workflow

Dry-run the acceptance contract from `main`:

```bash
gh workflow run backend-supabase-standby-production-acceptance.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=dry-run \
  -f owner_decision=NO-GO \
  -f confirmation=plan-production-standby-acceptance \
  -f enforce=true
```

Record the acceptance decision from `main` after protected review:

```bash
gh workflow run backend-supabase-standby-production-acceptance.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f operation=acceptance \
  -f owner_decision=GO \
  -f confirmation=record-production-standby-acceptance \
  -f enforce=true
```

The workflow uses the `production-backend` GitHub Environment and emits the
`backend-supabase-standby-production-acceptance` artifact with:

- `backend-supabase-standby-failover-plan.json`
- `backend-supabase-standby-staging-failover-drill.json`
- `backend-supabase-standby-production-acceptance.json`

Post the safe summary to #223 and #505. If the decision is `NO-GO`, keep #505
open and resolve the listed blockers before recording another decision.
