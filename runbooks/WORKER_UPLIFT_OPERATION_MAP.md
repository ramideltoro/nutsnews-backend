# Worker-Uplift Operation Map

This runbook covers tracking issue `ramideltoro/nutsnews-worker#70`.

The machine-readable source of truth is:

```text
docs/worker-uplift-operation-map.json
```

Validate it with:

```bash
python3 scripts/validate_worker_uplift_operation_map.py
```

Shared explanatory docs are linked from:

```text
ramideltoro/nutsnews-docs/NUTSNEWS_WORKER_UPLIFT_OPERATION_MAP.md
```

## Boundary

This map does not mutate servers, legacy Cloudflare Workers, Cloudflare DNS, or
Grafana Cloud resources. It assigns exactly one source-controlled owner for
each retained operation and marks the legacy behavior as copied, reused by API
contract, or intentionally retired.

New worker-uplift deployment, smoke, health, queue, DLQ, broker, reconciliation,
and cutover operations must not require a checkout of `ramideltoro/nutsnews-worker`.
The legacy Worker repo remains a rollback source until the approved cutover
retires it, but backend runtime automation must not import, call, or source
legacy Worker scripts.

## Owner Summary

| Operation surface | Owner | Protected path |
| --- | --- | --- |
| Backend host deploy and runtime bootstrap | `ramideltoro/nutsnews-backend` | `Protected Backend Ansible Apply` |
| Backend restart and fixed recovery actions | `ramideltoro/nutsnews-backend` | `Backend Recovery` |
| Backend status, health, drift, smoke, and logs | `ramideltoro/nutsnews-backend` | `Backend Health Report`, `Backend Drift Check`, `Backend Synthetic Monitor` |
| Backend PostgreSQL provider switch, cutover, backup, and restore proof | `ramideltoro/nutsnews-backend` | backend DB dry-run, proof, smoke, failover, and cutover workflows |
| Worker-uplift queue/DLQ/replay/drain/broker/reconciliation | `ramideltoro/nutsnews-backend` | future fixed backend workflows behind `production-backend` |
| Grafana folders, dashboards, alert rules, synthetics, quotas, and drift | `ramideltoro/nutsnews-infra` | `Grafana Cloud Plan` and `Grafana Cloud Apply` |
| DNS failover controller operations | `ramideltoro/nutsnews-infra` | `Cloudflare DNS Failover Apply` |
| Legacy Worker shard deployment and controller deployment | `ramideltoro/nutsnews-worker` | legacy rollback only until retirement |

## Legacy Worker Audit

| Legacy behavior | Source paths | Destination owner | Classification |
| --- | --- | --- | --- |
| Worker shard deploy | `.github/workflows/worker-pipeline.yml`, `scripts/deploy_worker_shards.mjs` | backend protected runtime bootstrap | Retire for new deployments; copy bounded deploy/preflight behavior |
| Public smoke and post-deploy verification | `.github/workflows/worker-smoke-test.yml`, `scripts/worker_smoke_test.mjs`, `scripts/post_deploy_verify.sh`, `scripts/edge_snapshot_health_check.mjs` | backend synthetic, health, and smoke workflows | Copy behavior; keep read-only |
| Backend shadow smoke | `.github/workflows/worker-shadow-smoke.yml`, `scripts/worker_shadow_smoke_live_check.mjs` | backend API compatibility, smoke, and cutover gates | Reuse current backend API contract until backend-owned shadow flow replaces it |
| Feed health | `scripts/feed_health_report.mjs`, `worker/src/index.ts` | backend fetcher service, health report, and Ops Dashboard | Copy behavior; write through backend API/stage state |
| Translation audit | `scripts/audit_article_translations.mjs` | backend translation service quality checks | Copy behavior; keep sanitized evidence |
| Backpressure and lock safety | `scripts/assert_worker_backpressure_locks.mjs`, `worker/src/index.ts` | backend RabbitMQ/stage-state guardrails | Copy behavior; replace Upstash/Worker locks with stage leases and RabbitMQ backpressure |
| Database provider switch | `docs/WORKER_DATABASE_PROVIDER_MODES.md`, `scripts/worker_db_provider_modes_smoke.mjs` | backend provider-switch and cutover workflows | Reuse API contract; backend owns final cutover gate |
| Supabase REST backup/restore | `scripts/supabase_backup.mjs`, `scripts/validate_supabase_restore.sh` | backend PostgreSQL backup/restore proof | Retire after backend primary cutover; Supabase remains rollback source before cutover |
| Controller failover | `docs/FAILOVER_ALERTS.md`, `docs/FAILOVER_ANALYTICS_ENGINE.md` | infra DNS failover controller | Retain separately; never classify as ingestion-only |

## Backend Operations

Backend owns these operations for the uplift runtime:

- deploy: `Protected Backend Ansible Apply`
- restart: `Backend Recovery`
- scale, pause, drain, and resume: future fixed backend workflows behind `production-backend`
- status and health: `Backend Health Report`, `Backend Drift Check`, read-only Ops Dashboard
- logs: backend Alloy as telemetry producer with Prometheus/Loki write credentials only
- smoke: `Backend PostgreSQL Smoke Tests`, `Backend Synthetic Monitor`, cutover smoke gates
- queue and DLQ inspect/replay: future fixed backend RabbitMQ operations
- broker backup/restore: rebuild topology from backend source control and replay from stage outbox
- reconciliation: backend stage schemas, outbox/watermark proof, and cutover gates
- database cutover and rollback: backend provider-switch and production-cutover workflows
- backend DNS/routing: `Backend Cloudflare Routing` only for `backend.nutsnews.com`

Every mutating backend operation requires a reviewed PR or a fixed workflow,
`production-backend` approval, and a rollback path recorded in the map.

## Grafana And Failover Separation

Grafana resource ownership is centralized in `ramideltoro/nutsnews-infra`.
Backend may validate its historical catalog fixture, but it must not create,
update, or delete Grafana folders, dashboards, datasources, alert rules, contact
points, quota guardrails, or Synthetic Monitoring resources.

DNS failover is not ingestion. It remains a separate Cloudflare DNS controller
operation in `ramideltoro/nutsnews-infra`. Worker-uplift services must not call
legacy controller scripts, and backend-host routing for `backend.nutsnews.com`
does not replace public apex/www failover ownership.

## Rollback Model

Before cutover, rollback can still use the legacy Worker deployment and
Supabase primary path. After backend PostgreSQL accepts authoritative writes,
rollback is limited to the documented rollback window; outside that window the
default is forward recovery from backend PostgreSQL backups, stage outbox,
watermarks, and replay evidence.
