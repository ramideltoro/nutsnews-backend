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

## Dashboard and Alerts

The protected workflow publishes sanitized report JSON to
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
