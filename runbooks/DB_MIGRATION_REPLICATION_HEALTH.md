# Backend PostgreSQL Replication Health

Issues: #109, #110

## Purpose

Replication health must make lag, inactive subscriptions, slot risk, disk risk,
and stale validation visible before backend PostgreSQL can become primary.

Local offline check:

```bash
python3 scripts/backend_postgres_replication_health.py --offline
```

Simulated broken-replication check:

```bash
python3 scripts/backend_postgres_replication_health.py --simulate-broken --enforce
```

Protected backend status check:

```bash
gh workflow run backend-postgres-replication-health.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref db-primary-migration-replication-health \
  -f mode=status
```

## Report Fields

- subscription count;
- whether a subscription worker PID is present;
- LSN field presence;
- maximum observed lag seconds;
- source slot status when a source DB URL is available;
- blocker list;
- safe textfile metrics for dashboard or node-exporter ingestion.
- dashboard status JSON at `/var/lib/nutsnews/postgres/replication-health.json`
  after the protected workflow publishes a status run.

The report must not print database URLs, replication credentials, WAL contents,
row data, or source connection strings.

## Thresholds

| Signal | Default threshold | Failure |
| --- | --- | --- |
| Subscription worker | PID absent when subscriptions exist | `subscription_inactive` |
| Replication lag | 300 seconds | `replication_lag_exceeds_threshold` |
| Source slot check | Source DB unavailable | `source_slot_check_failed` or skipped until configured |
| Slot activity | Slot inactive | `source_replication_slot_inactive` |
| Validation freshness | 900 seconds | `validation_status=stale` |

## Recovery

Repair the existing subscription only when:

- source slot is active;
- WAL retention risk is below threshold;
- target schema matches the parity manifest;
- parity validation can still pass after catch-up.

Re-seed from a fresh dump when:

- slot WAL is unavailable or unsafe;
- schema drift is detected;
- the subscription was inactive beyond the approved lag window;
- parity cannot be proven after repair.

## Automated Collection

The protected backend Ansible apply installs
`nutsnews-postgres-replication-health.timer`. It runs every five minutes with
root-only database URLs from
`/etc/nutsnews-backend-postgres-replication-health.env`, refreshes the JSON and
textfile evidence paths below, and fails the oneshot service when replication is
unhealthy. The timer remains enabled so later runs can record recovery.

Verify with read-only commands:

```bash
systemctl is-enabled nutsnews-postgres-replication-health.timer
systemctl list-timers nutsnews-postgres-replication-health.timer --no-pager
sudo systemctl status nutsnews-postgres-replication-health.service --no-pager
```

Rollback by setting the protected environment variable
`NUTSNEWS_BACKEND_POSTGRES_REPLICATION_HEALTH_ENABLED=false` and running the
protected baseline apply after a reviewed replacement telemetry path exists.

## Dashboard and Alerts

The systemd collector and the protected manual workflow publish sanitized report JSON to
`/var/lib/nutsnews/postgres/replication-health.json` and low-cardinality
textfile metrics to `/var/lib/nutsnews/metrics/backend-postgres-replication-health.prom`.
The ops dashboard collector overlays that replication section into
`/var/www/nutsnews-ops-dashboard/status.json`.

The health report emits `postgres_replication_health` as critical when lag,
inactive subscription, inactive slot, or blockers are present. The simulated
broken mode must fail with those blockers before staging or production cutover
uses the real status path.

## Cutover Gate

Production cutover is blocked unless replication is healthy or an approved final
dump path intentionally bypasses replication. Any bypass must be documented in
the production cutover issue with backup freshness, validation, and rollback
evidence.

## Cutover-aware enforcement

`backend_postgres_replication_health_expected_active` is `false` while
Supabase remains the only approved production writer. In that state, replication
readiness failures are reported as `blocked` warnings and do not fail the
one-shot systemd unit.

The protected cutover must set the variable to `true`. Once active,
missing, lagging, or inactive replication is fail-closed: the report status is
`fail` and `--enforce` returns non-zero.
