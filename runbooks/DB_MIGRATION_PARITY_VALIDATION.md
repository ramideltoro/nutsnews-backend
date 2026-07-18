# Database Migration Parity Validation

Issues: #107, #108

## Command

Offline manifest check:

```bash
python3 scripts/backend_postgres_parity_validate.py --offline
```

Protected staging comparison:

```bash
gh workflow run backend-postgres-parity-validation.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=validate-staging
```

## Report Contract

The validator reads `docs/supabase-backend-postgres-parity.json` and emits JSON
with safe metadata only:

- manifest version;
- source/target secret names, not secret values;
- pass, fail, skipped-with-reason, or blocked status;
- aggregate counts and metadata values only;
- failed required object ids.

Any failed required check blocks staging rehearsal and production cutover. A
skipped required check is allowed only in offline/local development mode; in
protected validation mode it is a blocker.

## Sensitivity

Validation queries must not select row-level content. Use counts, hashes,
object names, object-definition hashes, sequence bounds, role names, policy
names, extension versions, and timing metadata.
