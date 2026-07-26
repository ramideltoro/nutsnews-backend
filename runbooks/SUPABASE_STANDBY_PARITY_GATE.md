# Supabase Standby Required-Table Parity Gate

Issue #523 turns the relay `post_sync` parity metadata into an independent,
machine-readable promotion gate for required application tables.

## Boundary

The gate:

- reads the safe relay status JSON already collected by `Backend Health Report`;
- compares every table listed in `docs/backend-supabase-sync-relay.json`;
- emits `PASS` only when every required table has matching row counts, matching
  aggregate row checksums, zero target lag rows, fresh telemetry, and matching
  source/target fingerprints;
- emits `FAIL` for missing tables, incomplete comparisons, count mismatch,
  checksum mismatch, stale telemetry, malformed telemetry, mismatched
  fingerprints, or unavailable relay status;
- writes safe metadata only.

The gate does not read database credentials, run SQL, query Supabase directly,
approve failover, validate schemas, advance sequences, pause writers, or fence
providers.

## Result

The gate artifact is `backend-supabase-standby-parity-gate.json` and includes:

- `failover_attempt_id`;
- `manifest_fingerprint`;
- `relay_contract_fingerprint`;
- `source_fingerprint`;
- `target_fingerprint`;
- `measured_at_utc`;
- `expires_at_utc`;
- `relay_checked_at_utc`;
- `relay_comparison_age_seconds`;
- per-table `source_count`, `target_count`, `source_row_checksum`,
  `target_row_checksum`, `target_lag_rows`, status, and blockers;
- overall `status` as `PASS` or `FAIL`;
- `safe_metadata_only=true`.

The artifact must not contain database URLs, passwords, SQL, row values,
backend hostnames, Supabase project refs, or raw PostgreSQL errors.

## Manual Run

Run the non-enforcing proof mode:

```bash
gh workflow run backend-supabase-standby-parity-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ) \
  -f confirmation=evaluate-standby-parity-gate \
  -f enforce=false
```

Promotion consumers should use `enforce=true` or call the script with
`--enforce`, where any `FAIL` result returns non-zero.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_parity_gate.py
python3 -m unittest tests.test_backend_supabase_standby_parity_gate
```

Required fixture coverage:

- exact parity passes;
- added-row fixture fails row count and target lag;
- changed-row fixture fails checksum;
- deleted-row fixture fails row count;
- missing required table fails;
- incomplete comparison fails;
- stale, malformed, mismatched-target, unavailable-telemetry, and enforce-mode
  failures fail closed.

## Acceptance Evidence

For #523, record:

- merged backend PR and main checks;
- a main-branch `Backend Supabase Standby Parity Gate` run URL;
- artifact status, table counts, and blockers;
- a leak scan showing no database URLs, private keys, Supabase tokens, password
  fields, backend host metadata, Supabase host/project metadata, raw row
  markers, or raw source/target labels.

If the live result is `FAIL`, that is valid fail-closed evidence. It means
failover remains blocked until a later run produces passing parity evidence.
