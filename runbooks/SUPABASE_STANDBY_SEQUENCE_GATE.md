# Supabase Standby Sequence Safety Gate

Issue #525 turns relay sequence metadata into an independent, machine-readable
promotion gate for the existing production Supabase standby.

## Boundary

The gate:

- reads the safe relay status JSON already collected by `Backend Health Report`;
- evaluates only the sequences named in `docs/backend-supabase-sync-relay.json`;
- requires each sequence to be owned by the expected table/column binding;
- requires increment `1`, cycle disabled, and next value inside min/max bounds;
- requires the source next value to be greater than the source table max ID;
- requires the target next value to be greater than the target table max ID;
- requires the target next value to be greater than the source table max ID and
  not behind the source next safe value;
- covers empty-table and never-called sequence semantics without consuming
  sequence values;
- emits `PASS` only when all sequence evidence is fresh, complete, and
  target-matched;
- writes safe metadata only.

The gate does not read database credentials, run SQL from GitHub Actions, query
Supabase directly, call `nextval`, mutate sequences, approve failover, validate
row parity, validate schema compatibility, pause writers, or fence providers.

## Result

The gate artifact is `backend-supabase-standby-sequence-gate.json` and includes:

- `failover_attempt_id`;
- `repository_revision`;
- `manifest_fingerprint`;
- `relay_contract_fingerprint`;
- `source_fingerprint`;
- `target_fingerprint`;
- `measured_at_utc`;
- `expires_at_utc`;
- `relay_checked_at_utc`;
- `relay_sequence_age_seconds`;
- required/passed/failed sequence counts;
- per-sequence table/column binding, last/next values, `is_called`,
  increment, min/max, cycle, source/target max ID, binding fingerprint, and
  blockers;
- overall `status` as `PASS` or `FAIL`;
- `safe_metadata_only=true`.

The artifact must not contain database URLs, passwords, SQL, row values,
backend hostnames, Supabase project refs, raw source/target labels, or raw
PostgreSQL errors.

## Manual Run

```bash
gh workflow run backend-supabase-standby-sequence-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ) \
  -f confirmation=evaluate-standby-sequence-gate \
  -f enforce=false
```

Promotion consumers should use `enforce=true` or call the script with
`--enforce`, where any `FAIL` result returns non-zero.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_sequence_gate.py
python3 -m unittest tests.test_backend_supabase_standby_sequence_gate
python3 -m unittest tests.test_backend_supabase_standby_reconcile
```

Required fixture coverage:

- safe sequence fixtures pass without `nextval` or mutation;
- behind-max-ID and behind-source fixtures fail;
- missing sequence and incomplete report fixtures fail;
- misbound, unowned, cycled, exhausted, and unexpected-increment fixtures fail;
- empty-table and never-called sequence semantics are covered;
- stale, malformed, mismatched-target, unavailable-telemetry, and enforce-mode
  failures fail closed.

## Acceptance Evidence

For #525, record:

- merged backend PR and main checks;
- protected Ansible check/apply evidence when relay metadata fields change;
- a main-branch `Backend Supabase Standby Sequence Gate` run URL;
- artifact status, sequence counts, per-sequence blockers, and relay sequence
  age;
- a leak scan showing no database URLs, private keys, Supabase tokens, password
  fields, backend host metadata, Supabase host/project metadata, raw row
  markers, or raw source/target labels.

If the live result is `FAIL`, failover remains blocked until a later run
produces passing sequence safety evidence.
