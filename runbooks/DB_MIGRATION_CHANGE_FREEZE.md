# Database Migration Change Freeze

Issues: #101, #116

## Rule

During the DB primary migration window, database schema and data-contract
changes may only land through reviewed migrations or documented SQL files.
Direct Supabase Dashboard/Table Editor changes are allowed only for emergency
production repair and must be captured before any migration rehearsal continues.

## Owners

| Area | Owner | Rule |
| --- | --- | --- |
| Production Supabase schema | release operations | Reviewed migration or captured emergency SQL only |
| Backend PostgreSQL target schema | backend operations | Repo-managed restore/replication workflow only |
| App/API data contracts | app/API owner | Companion app PR plus manifest update |
| Worker data contracts | worker runtime owner | Companion worker/app PR plus manifest update |

## Required Evidence For A Schema Change

- migration file or SQL file path;
- manifest update in `docs/supabase-backend-postgres-parity.json`;
- owner and reviewer;
- affected tables/views/functions/triggers/RLS/grants;
- rollback notes;
- migration drift check output.

## Emergency Capture

1. Record the incident, operator, UTC time, and reason.
2. Export the exact SQL change or use Supabase migration history to reconstruct
   it.
3. Add the SQL or equivalent migration to the owning repo.
4. Update the parity manifest when object scope or validation changes.
5. Run:

```bash
python3 scripts/check_supabase_migration_drift.py --manifest docs/supabase-backend-postgres-parity.json
```

Use `--enforce` in protected rehearsal and cutover workflows:

```bash
python3 scripts/check_supabase_migration_drift.py --enforce
```

If source DB access or local migration files are unavailable, `--enforce`
returns a blocking status. Rehearsal, replication refresh, and production
cutover must stop until the drift source is available and reconciled.

## Cutover Checklist Blocker

The production cutover runbook must not proceed while any of these are true:

- source Supabase migration history cannot be read;
- repo migration files cannot be located;
- manifest version is behind an approved schema change;
- direct Supabase Dashboard/Table Editor changes have not been captured;
- object inventory or ownership is ambiguous.
