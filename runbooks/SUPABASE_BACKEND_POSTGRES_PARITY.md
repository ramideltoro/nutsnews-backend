# Supabase To Backend PostgreSQL Parity

Issues: #99, #107

## Purpose

`docs/supabase-backend-postgres-parity.json` is the machine-readable contract
for dump, restore, validation, replication, rehearsal, and cutover work.

Validate the manifest locally:

```bash
python3 scripts/validate_supabase_backend_postgres_parity.py
python3 scripts/backend_postgres_parity_validate.py --offline
```

## Operating Rules

- Supabase remains the production writer until #119 is explicitly approved and
  executed.
- Manifest changes are required for every schema or database-behavior change
  during the migration window.
- Every required object needs an owner, migration classification, status, and
  validation method.
- Exclusions must include a reason and cutover risk.
- Validation output must contain safe metadata only: counts, hashes, object
  names, versions, timings, and pass/fail state.

## How Later Work Uses The Manifest

Dump workflows use `required_objects` and `excluded_objects` to decide which
schemas, roles, table data, migration history, and Supabase-managed internals
are in scope.

Restore workflows use the same manifest to recreate only approved target
databases and to leave production-capable databases untouched by default.

Parity validation must emit a JSON report with:

- manifest version and git SHA;
- source and target identifiers without passwords or URLs;
- pass/fail/warning/skipped-with-reason states;
- row counts, checksums, sequence safety, and behavior-object hashes;
- explicit cutover blocker status.

## Cutover Gate

Cutover remains blocked unless:

1. the manifest validates;
2. migration drift check passes in `--enforce` mode;
3. restore and behavior parity reports pass for all required objects;
4. app/API compatibility and feature-flagged provider switch tests pass;
5. backup restore proof and rollback rehearsal are recorded.
