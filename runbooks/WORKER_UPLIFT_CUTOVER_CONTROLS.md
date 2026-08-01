# Worker-uplift reversible cutover controls

This runbook describes the as-built controls from `ramideltoro/nutsnews-worker#126`. It does not authorize cutover. Until #166 records an exact GO and #127 executes it, `ramideltoro/nutsnews-worker` remains the sole ingestion owner, legacy dispatch stays enabled, the eight uplift services stay in shadow mode, and `production_writes_enabled=false`.

## Authority boundary

The owner standing authorization at `nutsnews-worker#126` comment `5150510712` removes repeated per-release, first-run, and routine environment-wait approval for source-validated preflight, value-free dry-run, isolated rehearsal, verification, and safe control deployment. `scripts/validate_worker_uplift_cutover_controls.py` pins the authorized operation set, confirmations, production-backend environment, mutation target, database role, workflow, current safe state, and exclusions to scope digest `17dffe06f80ec9266761a84a2c738517c57da31e57ad8936dce16d003c021804`. Any change fails closed and needs a new reviewed authorization.

The #126 standing policy never authorizes #166 GO, #127 execution, a writer or owner switch, disabling legacy scheduling, enabling uplift writes, DNS/failover/Cloudflare changes, arbitrary SQL, secret retrieval, or risk acceptance. Final readiness is now governed by the separate, owner-recorded standing bounded authorization in `docs/worker-uplift-final-cutover-authorization.json`; it removes recurring owner prompts only for an exact machine-validated candidate and fails closed on drift.

## Fixed surfaces

- Workflow: `Backend Worker-Uplift Cutover Controls` on backend `main`, serialized by `backend-worker-uplift-cutover-controls`.
- State row: exactly `worker_uplift_final.cutover_control(control_id='production')`.
- Audit: `worker_uplift_final.cutover_control_audit`, populated by a security-definer trigger. The control role cannot write it.
- Role: `nutsnews_worker_uplift_cutover`, with only schema usage, control-row select, and column-level update. It has no insert, delete, truncate, create, domain-table, queue, or schema authority.
- Manager: `/usr/local/sbin/nutsnews-worker-uplift-cutover-control`.
- Decision: `/etc/nutsnews-worker-uplift/final-cutover-decision.json`, deployed as NO-GO until the final readiness gate freezes an exact candidate.
- Persistence production materialization: the controller can add only `NUTSNEWS_PERSISTENCE_PRODUCTION_WRITES_ENABLED=true` plus the fixed `backend-protected-persistence-cutover-approved` confirmation. The service must then issue one idempotent accepted-article command and one idempotent five-summary command through the scoped backend API. The base Compose file contains neither activation value.
- Publication production materialization: the same generated override activates the existing fixed publication confirmation. Persistence and publication are recreated together while the database control row is still fenced, so neither API command is authorized before the compare-and-swap activates the exact candidate.
- Legacy scheduling: the fixed protected workflow in `ramideltoro/nutsnews-worker`; it toggles only the `INGESTION_SCHEDULING_ENABLED` controller variable and retains cron, Durable Object, Analytics Engine, health/status/actions, DNS readback, live-origin readiness, alerts, and manual failover surfaces.

## Observability authority

Grafana ownership and alert-gating metrics are derived only from exactly one
`worker_uplift_final.cutover_control(control_id='production')` row. The
collector validates the generation, state, ingestion owner, legacy-dispatch
flag, uplift scheduler flag, production-write flag, and publication write mode
as one allowed tuple; it never infers ownership from Compose, a manifest, or an
environment variable. Missing, duplicate, malformed, or inconsistent control
state fails closed as `ownership_available=0` and `expected_active=0`.
A valid `shadow` row projects legacy ownership, shadow comparison, and writes
disabled; a valid `cutover_active` row projects uplift ownership and the
production write gate.

## Read-only and rehearsal operations

Dispatch from backend `main` with 64-character lowercase candidate and watermark SHA-256 values, an absolute UTC rollback deadline, and the exact confirmation:

| Action | Confirmation | Mutates production |
|---|---|---|
| `dry-run` | `plan-worker-uplift-cutover` | No |
| `rehearse` | `rehearse-worker-uplift-rollback` | No; simulation cannot reach production targets |
| `preflight` | `inspect-worker-uplift-cutover-controls` | No |
| `verify` | `verify-worker-uplift-cutover-controls` | No |

Dry-run renders all `shadow → fenced → cutover_active → rollback_pending → shadow` transitions and asserts no dual-writer state. Rehearsal runs the full rollback cycle or injects one of four fixed failures. Preflight/verify read the live state row plus the public legacy scheduling status and require all eight failover surfaces.

Every run uploads a value-free report and `SHA256SUMS` for 90 days. Inspect and verify both files; a green workflow conclusion alone is not evidence.

## Safe deployment recovery

Deploy control code and the dedicated role through `Protected Backend Ansible Apply`, first `check`, then `apply` with confirmation `backend.nutsnews.com`. Safe deployment must leave the state row at `shadow`, legacy scheduling enabled, uplift scheduler running in shadow, and uplift production writes disabled. It installs no active production API drop-in or publication override.

Use the protected worker-runtime `restart` operation for a healthy deployed image whose process or RabbitMQ channel needs recovery. Use protected Ansible apply when the installed manager, state contract, database grants, service environment, or Compose configuration is absent or drifted. Do not use restart to repair configuration drift, and do not hand-edit host files.

## Future apply sequence (only after #166 GO)

`apply` requires `execute-worker-uplift-cutover:<candidate_sha256>`. The fixed manager validates that the source-controlled decision is GO for the same candidate, watermark, deadline, control commit, #166, #127, standing authorization scope, and frozen execution window. The protected workflow then:

1. captures preflight evidence;
2. disables only legacy ingestion scheduling through the #150 workflow and verifies retained failover surfaces;
3. stops the uplift scheduler and requires the separately recorded drain/watermark evidence;
4. prepares the API, persistence, and publication production configuration while the database row still denies writes;
5. compare-and-swaps `fenced` to `cutover_active` for the exact generation, candidate, and watermark;
6. starts the uplift scheduler and verifies one writer.

The database gate is required in addition to the API environment and publication confirmation, so flags alone cannot enable production writes. A stale generation loses the compare-and-swap. Failures before activation leave legacy active or both writers fenced; failures after activation leave only uplift authorized and legacy fenced.

## Future rollback sequence (only for the exact eligible cutover)

`rollback` requires `rollback-worker-uplift-cutover:<watermark_sha256>` before the absolute deadline. It stops the uplift scheduler, moves the row to `rollback_pending` with uplift writes denied, removes the production API/persistence/publication overlay, recreates persistence and publication from the source-controlled shadow Compose file, verifies zero uplift output, re-enables legacy scheduling through #150, verifies all failover surfaces, then finalizes `shadow` and restarts the uplift scheduler in shadow. Preserve queues, DLQs, audit rows, and watermark evidence.

If the deadline has passed, the manager rejects rollback and operators must use the named forward-recovery plan from #166/#127. Never modify DNS or the Cloudflare failover controller as part of this procedure.
