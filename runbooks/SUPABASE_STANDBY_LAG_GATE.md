# Supabase Standby Lag Gate

Issue #522 turns the relay health telemetry from #500 into an independent,
machine-readable promotion gate for the existing production Supabase standby.

## Boundary

The gate:

- reads only the safe `supabase_sync_relay_health` section from a backend health
  report and the safe relay status JSON already collected by that report;
- emits `PASS` only when relay health is healthy, telemetry is fresh, source and
  target fingerprints match the relay contract, and lag is `<= 30` seconds;
- emits `FAIL` for lag `> 30` seconds, missing telemetry, stale telemetry,
  malformed telemetry, mismatched target/source fingerprints, stopped relay, or
  unavailable relay status;
- writes safe metadata only.

The gate does not read database credentials, send SQL, query Supabase directly,
approve failover, pause writers, compare parity, validate schemas, advance
sequences, or fence providers.

## Result

The gate artifact is `backend-supabase-standby-lag-gate.json` and includes:

- `failover_attempt_id`;
- `source_fingerprint`;
- `target_fingerprint`;
- `observed_lag_seconds`;
- `measured_at_utc`;
- `expires_at_utc`;
- `status` as `PASS` or `FAIL`;
- `blockers`;
- `safe_metadata_only=true`.

The result intentionally omits backend hostnames, Supabase project refs,
database URLs, passwords, SQL, row data, and raw PostgreSQL errors.

## Manual Run

Run the non-enforcing proof mode:

```bash
gh workflow run backend-supabase-standby-lag-gate.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f failover_attempt_id=failover-$(date -u +%Y%m%dT%H%M%SZ) \
  -f confirmation=evaluate-standby-lag-gate \
  -f enforce=false
```

Promotion consumers should use `enforce=true` or call the script with
`--enforce`, where any `FAIL` result returns non-zero.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_lag_gate.py
python3 -m unittest tests.test_backend_supabase_standby_lag_gate
```

Boundary coverage requires `29` and `30` seconds to pass, and `31` seconds to
fail. Missing, stale, malformed, mismatched-target, stopped-relay, and
unavailable-telemetry fixtures must fail closed.

## Acceptance Evidence

For #522, record:

- merged backend PR and main checks;
- a main-branch `Backend Supabase Standby Lag Gate` run URL;
- the artifact status and blockers;
- a leak scan showing no database URLs, private keys, Supabase tokens, password
  fields, backend hostnames, Supabase project refs, or raw row markers.

If the live result is `FAIL` because lag is over `30` seconds, that is valid
fail-closed gate evidence. It means failover remains blocked until later work
produces fresh passing lag evidence.
