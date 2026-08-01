# Worker-Uplift Cutover-Control Plan

This runbook explains the non-mutating planning contract owned by
`ramideltoro/nutsnews-worker#165`. The machine-readable source of truth is
`docs/worker-uplift-cutover-control-plan.json`.

This plan does not implement or exercise a cutover control. Legacy
`ramideltoro/nutsnews-worker` remains the production ingestion owner, legacy
dispatch remains enabled, uplift remains in `shadow` mode, and uplift
production writes and visibility remain false. It changes no worker, host,
database, queue, Cloudflare, DNS, failover, Environment protection, credential,
or production record.

## Decision sequence

The gates have deliberately separate authority:

1. #125 may approve beginning #150 only.
2. #150 implements a reversible legacy ingestion-dispatch flag while leaving
   dispatch enabled and the DNS failover controller intact.
3. #126 implements protected dry-run/apply/verify/rollback controls while
   leaving legacy ingestion enabled and uplift writes disabled.
4. #166 revalidates the exact deployed candidate, rehearses rollback without
   changing live ownership or writes, freezes all timestamps and evidence, and
   may approve #127 only.
5. #127 is the only execution issue allowed to switch ingestion ownership.
6. #128 may retire legacy ingestion only after the complete observation window.

A merged plan, green CI, closed #165, GO on #125, or completion of #150/#126 is
not cutover authorization.

## Planned execution window and deadline

The current planning reference window is
`2026-08-02T02:00:00Z` through `2026-08-02T04:00:00Z`. It is an absolute
planning value, not a scheduled or approved mutation. #166 must replace it if
the final execution is outside that window.

The planned rollback deadline is `2026-08-04T04:00:00Z`, calculated as the
planning window end plus 48 hours. The target recovery time is 900 seconds.
#166 must freeze a fresh absolute deadline derived from its exact execution
window. A relative phrase such as “48 hours after cutover” is insufficient.

Rollback eligibility may end before the deadline. It ends immediately if the
watermark is stale, the candidate changes, a second writer appears, or an
uplift-only domain effect cannot be proven present in the durable audit and
visible to legacy natural-key dedupe. After eligibility ends, the safe policy
is forward recovery unless a separately reviewed sync-back proof establishes
zero loss and duplication.

## Watermark semantics

The final watermark is not captured in #165. #166 must build one canonical,
value-free JSON snapshot with six sources:

1. the #150 legacy-dispatch fence receipt, including the last completed
   dispatch and zero in-flight legacy dispatches;
2. all seven RabbitMQ main/retry/DLQ snapshots and main-queue consumer counts;
3. every stage reconciliation-watermark row;
4. every stage maximum confirmed outbox tuple and zero pending/retrying/DLQ
   counts;
5. final aggregate, API-command, natural-key, and domain-effect boundaries; and
6. the exact images, packages, contracts, configuration, topology, identities,
   and write policy.

The watermark ID is the SHA-256 of canonical UTF-8 JSON with sorted keys and
compact separators. A candidate revision, resumed legacy dispatch, changed
queue/outbox/watermark count, failed API receipt, unexplained domain effect, or
DNS-controller drift invalidates it. Recompute only after rerunning the complete
preflight; never edit a digest to fit changed evidence.

## Single-writer handoff model

The future protected control must implement this exact state sequence:

| Phase | Legacy dispatch | Active owner | Uplift writes |
| --- | --- | --- | --- |
| Preflight | enabled | `legacy_shards` | false |
| Fence and drain | disabled only by authorized #127 execution | `legacy_shards` | false |
| Freeze watermark | disabled | `legacy_shards` | false |
| Atomic owner/write switch | disabled | `worker_uplift` | true |
| Start uplift scheduler and verify | disabled | `worker_uplift` | true |

The owner/write transition must be a compare-and-swap operation that verifies
the expected legacy owner and cannot create two writers. Start the uplift
production scheduler only after that transaction succeeds. A partial failure
must keep uplift writes false or immediately run the protected rollback path;
it must never resume both schedulers.

Rollback reverses the order: pause uplift scheduling, settle or preserve
in-flight work, atomically disable uplift output and restore the legacy owner,
prove zero uplift output after the transaction, then re-enable legacy dispatch
through #150. Preserve queues, outboxes, watermarks, and audit rows.

## Observation window and thresholds

The observation duration is 48 hours with five-minute checkpoints. #166 freezes
absolute start and end timestamps. Legacy ingestion remains disabled but
reversible, and its assets remain deployed or immediately redeployable for the
entire window. #128 cannot begin before the window succeeds.

Immediate abort conditions include dual/unknown ownership, a lost or duplicate
boundary effect, any unexplained DLQ message, any failed production API receipt,
an unresolved AI-stage failure/OpenAI use, or DNS-failover drift. Other limits
abort after two five-minute checkpoints:

- all eight services healthy;
- at least one consumer on every required main queue;
- no queue above 100 ready or 10 unacknowledged messages;
- no queue/outbox age above 900 seconds, with 1800 seconds an immediate abort;
- zero reconciliation lag and zero pending/retrying/dead-lettered outbox rows;
- availability and error-free requests at least 99.5 percent;
- at least 95 percent of backend requests within 0.75 seconds;
- at least 99 percent of feed samples no older than 900 seconds;
- publisher timeout/nack rate at most 1 percent over ten minutes;
- disk and memory no more than 80 percent, load per vCPU no more than 1.5, and
  zero failed required systemd units;
- fresh Prometheus/Loki data and zero unexplained critical alerts.

The machine contract contains the complete threshold table and exact abort
wording. #166 must attach current evaluation artifacts and digests; it may not
silently relax a threshold.

## Ownership and unavailable-owner behavior

`ramideltoro` is the named primary owner for ingestion, scheduling, write
enablement, RabbitMQ, database/API, Qwen, observability, Cloudflare failover,
incident command, rollback, and final approval. Repository ownership is
recorded per domain in the machine contract.

There is currently no independent human backup. The plan does not invent one.
Each domain instead names a concrete fail-closed safe-state control. If the
owner or incident commander is unavailable, the decision is NO-GO or immediate
abort: keep `legacy_shards` as owner, legacy dispatch enabled, and uplift writes
false. Automation cannot substitute for the named #125 or #166 approver.

## Evidence custody

The backend repository owns the plan and machine artifacts; the worker
repository owns tracking and immutable evidence comments. Protected backend
and infra workflows own runtime artifacts. Every record must include the
repository, run ID, head commit, Environment, artifact ID/digest, downloaded
JSON SHA-256, generated timestamp, watermark ID, and candidate-manifest digest.

Download evidence before platform expiry and retain it for at least 90 days
after #128 closes. Issues record identifiers and digests only. Never copy
credentials, connection strings, authorization headers, private keys, message
payloads, article bodies, prompts, or model responses.

## Validate

Run:

```bash
python3 scripts/validate_worker_uplift_cutover_control_plan.py
python3 -m unittest tests.test_worker_uplift_cutover_control_plan
python3 scripts/validate_worker_uplift_production_readiness.py
python3 -m unittest tests.test_worker_uplift_production_readiness
```

`Backend Checks` enforces the same validator and focused tests. The validator
rejects placeholder/anonymous owners, missing safe-state backups, relative-only
or inconsistent deadlines, incomplete watermark sources, shortened observation,
missing thresholds/final-gate fields, a current owner other than legacy, writes
enabled in the current state, or any implied cutover authority.
