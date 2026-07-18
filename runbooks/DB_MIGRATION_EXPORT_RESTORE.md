# Database Migration Export And Restore

Issues: #103, #104

## Export

Protected logical export workflow:

```bash
gh workflow run backend-postgres-logical-export.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=plan
```

Non-production staging export:

```bash
gh workflow run backend-postgres-logical-export.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=export-staging \
  -f confirm_export=export-staging-supabase
```

The workflow may create temporary role, schema, data, and migration-history dump
files in runner-local temp storage only. It uploads only safe metadata:

- source environment and project ref;
- manifest version;
- artifact labels, byte counts, and SHA-256 checksums;
- start/end time and workflow run id.

Dump files, direct database URLs, passwords, Supabase tokens, service-role keys,
and row data must never be uploaded or printed.

## Restore

The existing `backend-postgres-failover-drill.yml` remains the first protected
restore path. Restore defaults to `nutsnews_restore_rehearsal`, recreates that
database from scratch, and leaves any production-capable database untouched.
Because each drill drops and recreates the rehearsal database, the restore
runner reapplies grants for `nutsnews_readonly`,
`nutsnews_migration_validation`, and `nutsnews_app_rehearsal` before downstream
parity, smoke, and benchmark workflows connect with migration credentials.

Restore evidence must include:

- manifest version;
- schema and data dump checksums;
- restore start/end time and duration;
- source project ref;
- aggregate row counts;
- validation status.

Production restore to the future-primary shadow database is enabled only by the
protected `backend-postgres-primary-shadow-restore.yml` workflow. It restores
production Supabase public schema/data and Supabase migration history into
`nutsnews_primary_shadow`; it does not cut over app or worker writes.

Run status mode:

```bash
gh workflow run backend-postgres-primary-shadow-restore.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=status
```

Run the protected restore only after #211 live provisioning evidence passes:

```bash
gh workflow run backend-postgres-primary-shadow-restore.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=restore-production \
  -f confirm_restore=restore-production-to-primary-shadow
```

The workflow is fixed to `nutsnews_primary_shadow` and refuses the rehearsal and
backup-proof databases. Restore evidence must include:

- logical snapshot id;
- target database;
- public and migration-history dump checksums;
- restore duration;
- validation status;
- RPO/RTO seconds;
- protected workflow operator and workflow URL.

## Cleanup

Each workflow must remove temporary dump files before completion. If cleanup
fails, the workflow must fail or produce a blocker in the safe metadata report.
