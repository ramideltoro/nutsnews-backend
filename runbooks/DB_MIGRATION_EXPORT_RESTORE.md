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

Restore evidence must include:

- manifest version;
- schema and data dump checksums;
- restore start/end time and duration;
- source project ref;
- aggregate row counts;
- validation status.

Production restore is not enabled by this runbook. It requires a later approved
cutover runbook and protected workflow inputs.

## Cleanup

Each workflow must remove temporary dump files before completion. If cleanup
fails, the workflow must fail or produce a blocker in the safe metadata report.
