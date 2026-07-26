# Supabase Standby Schema Compatibility Gate

Issue #524 turns the relay schema and identity metadata into an independent,
machine-readable promotion gate for the existing production Supabase standby.

## Boundary

The gate:

- checks out the candidate `ramideltoro/nutsnews` application revision and
  reads `supabase/standby_manifest.json`;
- reads the safe relay status JSON already collected by `Backend Health Report`;
- verifies the candidate manifest fingerprint, required table set, required
  sequence set, and no-new-Supabase safety flags against the backend sync
  contract;
- requires matching source/target schema fingerprints and source/target
  migration contract fingerprints;
- verifies every required table has passing manifest replica identity metadata
  and passing live source/target primary-key identity metadata;
- verifies every required sequence has safe metadata for the named
  sequence/table/column binding;
- emits `PASS` only when all schema/identity/binding evidence is fresh,
  complete, and target-matched;
- writes safe metadata only.

The gate does not read database credentials, run SQL from GitHub Actions, query
Supabase directly, approve failover, validate row parity, prove sequence write
safety, pause writers, or fence providers.

## Result

The gate artifact is `backend-supabase-standby-schema-gate.json` and includes:

- `failover_attempt_id`;
- `candidate_application_revision`;
- `repository_revision`;
- `manifest_fingerprint`;
- candidate manifest fingerprints/counts;
- `relay_contract_fingerprint`;
- `source_fingerprint`;
- `target_fingerprint`;
- `measured_at_utc`;
- `expires_at_utc`;
- `relay_checked_at_utc`;
- `relay_schema_age_seconds`;
- schema hash status and bounded schema/migration-contract diff counts when
  incompatible;
- per-table identity status, safe primary-key fingerprints, relation kind,
  replica identity type, and blockers;
- per-sequence binding status, binding fingerprint, position-safety status, and
  blockers;
- overall `status` as `PASS` or `FAIL`;
- `safe_metadata_only=true`.

The artifact must not contain database URLs, passwords, SQL, row values,
backend hostnames, Supabase project refs, raw source/target labels, or raw
PostgreSQL errors.

## Manual Run

Use the full app commit SHA that would be promoted:

```bash
app_revision="$(gh api repos/ramideltoro/nutsnews/commits/main --jq .sha)"
gh workflow run backend-supabase-standby-schema-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ) \
  -f candidate_application_revision="$app_revision" \
  -f confirmation=evaluate-standby-schema-gate \
  -f enforce=false
```

Promotion consumers should use `enforce=true` or call the script with
`--enforce`, where any `FAIL` result returns non-zero.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_schema_gate.py
python3 -m unittest tests.test_backend_supabase_standby_schema_gate
```

Required fixture coverage:

- exact compatible schema passes;
- candidate manifest fingerprint or table/sequence set mismatch fails;
- missing function or view declarations fail closed until explicit validators
  exist;
- schema fingerprint and migration-contract mismatch fail;
- missing columns, constraints, and indexes are surfaced as bounded safe diff
  metadata;
- missing or failing primary-key/replica-identity checks fail;
- missing sequence binding fails;
- sequence position-only failure remains scoped to #525 and does not block this
  schema gate;
- stale, malformed, mismatched-target, unavailable-telemetry,
  wrong-candidate-revision, and enforce-mode failures fail closed.

## Acceptance Evidence

For #524, record:

- merged backend PR and main checks;
- a main-branch `Backend Supabase Standby Schema Gate` run URL;
- artifact status, schema status, identity counts, sequence binding counts, and
  blockers;
- the candidate application revision used for the proof;
- a leak scan showing no database URLs, private keys, Supabase tokens, password
  fields, backend host metadata, Supabase host/project metadata, raw row
  markers, or raw source/target labels.

If the live result is `FAIL`, failover remains blocked until a later run
produces passing schema compatibility evidence.
