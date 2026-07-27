# Supabase Standby Reconciliation

Issue #501 keeps the existing production Supabase database qualified as the
backend PostgreSQL hot standby. The workflow runs through `workflow_dispatch`
and on a `schedule` every six hours.

The normal `report` mode is safe metadata only. It collects a fresh backend
health report, evaluates the existing #523 parity gate, #524 schema gate, and
#525 sequence gate against the same reconciliation attempt, and uploads an
aggregate `backend-supabase-standby-reconciliation` artifact plus the individual
gate artifacts. It does not write to Supabase, does not switch providers, does
not create a new Supabase project, and does not create a `nutsnews-standby`
database.

Guardrail wording for cleanup/review: report mode does not create a new Supabase
project and does not create a `nutsnews-standby` database.

Exact policy phrase: report mode does not create a new Supabase project.

Manual report:

```bash
gh workflow run backend-supabase-standby-reconciliation.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=report \
  -f confirmation=report-existing-production-supabase-standby \
  -f enforce=false
```

The report passes only when required table row counts/checksums, schema
fingerprint and required object compatibility, and sequence safety all pass with
zero failed required checks. Any failed required table produces stable blocker
codes and blocks later failover approval.

The `apply-backfill` mode is retained for the completed #498 bootstrap path. It
requires the exact confirmation
`backfill-existing-production-supabase-from-backend-primary`, uses the protected
`production-backend` environment, and may mutate only the existing production
Supabase standby from backend PostgreSQL primary. Do not use it for routine
scheduled reconciliation.

Local validation:

```bash
python3 scripts/validate_backend_supabase_standby_reconciliation.py
python3 -m unittest tests.test_backend_supabase_standby_reconciliation_report
python3 scripts/backend_supabase_standby_reconcile.py --offline --enforce
python3 -m unittest tests.test_backend_supabase_standby_reconcile
```
