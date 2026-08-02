# Supabase Standby Sync Relay

Issue #499 adds a private backend-side relay that keeps the existing production
Supabase database synchronized from backend PostgreSQL primary. Backend
PostgreSQL remains the normal read/write database. Supabase is only the
backup/hot-standby target until a later owner-approved failover.

> Operational state: suspended. Snapshot parity scans were found to exhaust the
> shared production Supabase database even without target writes. Keep
> `NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED=false`; the Ansible baseline
> stops and disables both the timer and any in-flight service. Re-enablement
> requires reviewed incremental replication that does not scan whole tables on
> the production API database.

## Safety Boundary

- The relay runs on the backend host through systemd.
- Source reads use backend PostgreSQL over `127.0.0.1:5432`.
- Target writes go outbound to the existing production Supabase PostgreSQL URL.
- No inbound Supabase connection to backend PostgreSQL is required.
- App and worker services do not receive Supabase write credentials.
- The relay blocks before mutating Supabase if schema or table identity checks
  fail.
- Reports contain safe metadata only: check IDs, counts, hashes, status, and
  bounded object names.

## Installation

Use the protected backend apply workflow:

1. Keep `NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED=false` in the
   `production-backend` environment.
2. Confirm `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL` and
   `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD` exist in the same
   protected environment.
3. Run `Protected Backend Ansible Apply` in `check` mode.
4. Review the check-mode output.
5. Run `Protected Backend Ansible Apply` in `apply` mode with
   `confirm_apply=backend.nutsnews.com`.

The workflow renders `/etc/nutsnews-supabase-sync-relay/relay.env` as
`root:nutsnews-standby-relay` with mode `0640`.

## Runtime

- Service: `nutsnews-supabase-sync-relay.service`
- Timer: `nutsnews-supabase-sync-relay.timer`
- Default interval: 60 seconds after the prior run finishes, with up to 10
  seconds of jitter
- Oneshot startup timeout: 120 seconds
- Last safe report: `/var/lib/nutsnews/supabase-sync-relay/last-run.json`
  with mode `0644`

The report is schema version 2 and is replaced atomically. Every attempt records
`checked_at_utc` and `finished_at_utc`. Only a fully validated
`mode=sync-once`, `post_sync.status=pass` run advances
`last_success_at_utc`. A run that repaired drift also advances
`last_applied_at_utc`; a parity pass with `sync.status=not_required` preserves
the prior apply timestamp and performs no target mutation. Later failures
preserve both timestamps so their ages continue to increase. Dry runs never
create success history. `validation_summary` reports complete table coverage,
failed-table count, and bounded maximum target lag rows; skipped or incomplete
validation remains explicitly unavailable.

Each run validates parity before applying changes. If parity already passes,
the relay exits without writing. Otherwise it copies only drifted tables, and
its target update statement touches only rows whose values changed. Target
lock waits are capped at 2 seconds, statements at 45 seconds, and systemd stops
the entire oneshot start at 120 seconds. These limits intentionally prefer a stale,
fail-closed standby over production database starvation.

These bounds are defense in depth only; they do not authorize the snapshot
timer to be enabled in production. Apply the baseline with the enable variable
false to stop and disable the timer and any in-flight service. Verify
`systemctl is-enabled` reports `disabled` and `systemctl is-active` reports
`inactive` for both units.

Useful backend-host checks:

```bash
systemctl status nutsnews-supabase-sync-relay.timer
systemctl status nutsnews-supabase-sync-relay.service
journalctl -u nutsnews-supabase-sync-relay.service -n 100 --no-pager
sudo -u nutsnews-standby-relay python3 /usr/local/libexec/nutsnews-backend-supabase-sync-relay.py \
  --contract /etc/nutsnews-supabase-sync-relay/contract.json \
  --mode dry-run \
  --enforce
```

## Health, Lag, and Alerting

Issue #500 exposes relay health through the existing `Backend Health Report`
workflow as the `supabase_sync_relay_health` check. The health report reads the
timer/service state and the safe relay report at:

```text
/var/lib/nutsnews/supabase-sync-relay/last-run.json
```

The report includes only safe metadata:

- timer state, service state, and last service result;
- durable `last_applied_at_utc` and `last_success_at_utc` from successful
  sync-once runs;
- last validated sync age (`lag_seconds`, retained for compatibility) and
  maximum table lag rows;
- `failed_table_count`;
- generic `last_error` code.

The protected disabled tuple (`timer=inactive/disabled`,
`service=inactive/static`, and a successful last unit result) reports
`not_configured` with `expected_active=false` and remains ineligible for
failover. When the relay is expected active, lag over `180` seconds marks
standby failover as blocked with `relay_lag_exceeds_threshold`. Missing or
stopped relay timer state then marks the check critical with
`relay_timer_stopped` or `relay_report_missing`. Failed relay status, failed
table checks, invalid report JSON, or missing safe-metadata marking also fail
closed.

Run the health report manually without email:

```bash
gh workflow run backend-health-report.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f send_email=false
```

Acceptance evidence for #500 must include the successful health report run URL,
the `supabase_sync_relay_health` check summary, and confirmation that no
database URLs, passwords, Supabase keys, raw row data, or host/project metadata
were printed.

## Acceptance Evidence

For #499, record:

- PR and merge SHA.
- Local validator/test output.
- Protected backend apply run URL for check and apply mode when enabled.
- Protected `Backend Supabase Sync Relay Smoke` run URL showing synthetic
  fixture insert, update, and delete catch-up.
- Relay `last-run.json` summary showing `status=pass`, `safe_metadata_only=true`,
  `backend_postgresql_remains_primary=true`, and no app/worker Supabase write
  credential injection.

## Protected Catch-Up Smoke

After the relay is installed and the timer is active, run:

```bash
gh workflow run backend-supabase-sync-relay-smoke.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f confirmation=prove-backend-supabase-sync-relay
```

The smoke workflow enters the protected `production-backend` Environment,
copies the fixed smoke script to a remote temp directory on the backend host,
and uses only synthetic rows in:

```text
public.staging_fixture_runs
public.staging_fixture_users
```

It proves:

- insert catch-up from backend PostgreSQL to Supabase;
- update catch-up from backend PostgreSQL to Supabase;
- delete catch-up from backend PostgreSQL to Supabase;
- backend PostgreSQL remains private on loopback;
- app/worker Supabase writes remain disabled before failover.

The smoke report is safe metadata only. It must not print database URLs,
passwords, Supabase host/project metadata, PostgreSQL errors, or row values.

This relay does not approve failover. Later issues still gate failover on lag,
parity, schema, sequence safety, writer pause, and split-brain checks.
