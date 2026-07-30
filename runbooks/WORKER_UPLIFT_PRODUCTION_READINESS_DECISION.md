# Worker-Uplift Production Readiness Decision

Tracking issue: `ramideltoro/nutsnews-worker#125`

This runbook interprets
`docs/worker-uplift-production-readiness-decision.json`. The current decision
is **NO-GO**. It is a non-mutating readiness record, not cutover authority.

Legacy `ramideltoro/nutsnews-worker` remains the production ingestion owner.
All uplift services remain shadow-only. `production_writes_enabled` and
publication visibility remain false. Do not change DNS, failover, the legacy
worker, production ownership, environment protections, or production
infrastructure from this issue.

## Evidence artifacts

- Decision:
  `docs/worker-uplift-production-readiness-decision.json`
- Value-free deployed Cloudflare binding proof:
  `docs/evidence/worker-uplift-cloudflare-bindings-2026-07-30.json`
- Validator:
  `python3 scripts/validate_worker_uplift_production_readiness.py`
- Focused tests:
  `python3 -m unittest tests.test_worker_uplift_production_readiness`

The Cloudflare evidence records binding names and types, the deployed version
and cron metadata, and source hashes. It deliberately excludes values,
account/zone identifiers, credentials, private headers, and DNS record data.

## Decision rule

The validator must fail if a document:

- changes the decision to GO while a blocker is open;
- authorizes #125 closure without a named approver;
- enables uplift writes or production visibility;
- changes the single-writer owner away from the legacy worker;
- implies that `FAILOVER_ANALYTICS` exists without deployed binding proof;
- fabricates an owner decision or risk waiver;
- drops a stale/missing evidence item;
- records a credential, connection string, or binding value.

The decision may be reconsidered only after every recorded blocker is closed
with current immutable evidence. Reconsideration is a new review; a passing
validator does not automatically upgrade NO-GO to GO.

## Current disposition

| Area | Status | Evidence or blocker |
| --- | --- | --- |
| Single writer/write policy | Pass | Legacy commit and false write/visibility gates are pinned in the decision |
| Exact images/packages/contracts | Pass | Eight image digests and four package releases/attestations are pinned |
| 48-hour soak/capacity/cost | Pass | Run `30405550709`, 72.43 hours and 415 events on the current image set |
| Zero-consumer recovery/drain | Pass | Runs `30404840645`, `30405237965`, and `30405294851` |
| Grafana metrics/logs/alerts | Pass | Infra apply plus protected metrics/logs artifacts are pinned |
| Operations guide | Pass | Docs merge `77eeb52078878c2f95989db3107b814e54c52222`; it denies cutover authority |
| Runtime identity inventory | Block | Dedicated inventory uses superseded queue/permission names |
| Parity | Block | Run `30203441579` used eight superseded image digests |
| Empty-broker recovery | Block | Run `30215207093` predates the current topology hash |
| Dependency outage/backup proof | Block | Current candidate-tied PostgreSQL/API/Qwen and restore artifacts are missing |
| Admin deployed proof | Block | Source and access control are present; current authenticated production projection proof is missing |
| Security residuals | Block | `SEC-124-002` through `SEC-124-009` lack named #125 disposition |
| Cloudflare failover | Block | Deployed and source config omit `FAILOVER_ANALYTICS`; `nutsnews-infra#440` owns the independent fix/decision |
| Cutover/rollback controls | Block | Watermark, rollback deadline, observation window, and owner/write controls are future #150/#126/#127 work |
| Named readiness approval | Block | No named GO approver exists |

## Action classes

### Read-only

Local validation, GitHub metadata/artifact inspection, Cloudflare
binding-name/type inspection, runtime `status`, parity reports, soak reports,
metrics, logs, recovery `status`, and public access-control checks are
read-only.

The current-head runtime status dispatch is:

```text
Backend Worker Runtime Operations
action=status
dry_run=true
run=30513933114
```

It is waiting for `production-backend` approval. Do not approve on another
person's behalf or bypass the environment. Once approved, inspect the artifact
for mode, write policy, images, service health, consumers, queues, and
guardrails; the workflow conclusion alone is insufficient.

A later owner may refresh parity through the protected read-only workflow:

```bash
gh workflow run backend-worker-uplift-parity-report.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=live-read-only
```

If images or runtime configuration change, refresh the complete soak:

```bash
gh workflow run backend-worker-uplift-soak-report.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f mode=live-read-only \
  -f min_window_hours=48 \
  -f require_complete_window=true
```

Both workflows use the protected environment. Do not bypass approval. Download
and inspect every report artifact, then record its run, head commit, artifact
ID/digest, window, versions, failed checks, and safety state.

### Dry run

Ansible check mode, runtime operations with `dry_run=true`, reconciliation
plans, Grafana plans, and Cloudflare failover plan mode describe intended
changes but do not authorize apply. A dry run cannot satisfy a recovery
acceptance criterion that explicitly requires a protected drill.

### Protected mutation

Service deploy/restart/rollback, shadow smoke, reconciliation apply,
clean-rebuild/restore drills, backups, Grafana apply, and Cloudflare apply are
mutations even when they preserve shadow-only policy.

Do not run them from this non-mutating #125 review. Each missing drill or
remediation needs separate owner approval and must use its existing protected
workflow. In particular:

- `clean-rebuild-drill` requires `Backend RabbitMQ Recovery`, the exact
  confirmation target, and `production-backend` approval;
- Cloudflare analytics remediation or disposition belongs to
  `ramideltoro/nutsnews-infra#440`;
- admin deployed proof belongs to `ramideltoro/nutsnews`;
- future owner/write/watermark/rollback controls belong to the ordered
  tracking issues, not #125.

## Cloudflare failover rule

The current read-only API evidence proves:

- `nutsnews-dns-failover` version 30 receives 100% of traffic;
- `DNS_FAILOVER` binds `DnsFailoverController`;
- the once-per-minute cron is deployed;
- `FAILOVER_ANALYTICS` is not a deployed binding;
- current infra source also omits the binding and has no analytics writer.

Documentation is not proof of a deployed binding. Readiness remains blocked
until `nutsnews-infra#440` attaches value-free protected deployment/query
evidence, or a named owner explicitly decides with rationale that Analytics is
unnecessary. This runbook does not make that decision. Analytics must remain
best-effort and must never become a dependency of DNS failover.

## Reconsideration checklist

Before any later GO:

1. Resolve every blocker in the machine-readable decision.
2. Refresh evidence at the exact repository heads, image digests, package
   versions, contracts, topology, and config hashes proposed for approval.
3. Prove all eight services healthy, seven required consumers positive,
   queues drained, DLQs explained, and write/visibility gates false.
4. Attach current parity, outage/recovery, backup/restore, admin, Grafana, and
   Cloudflare artifacts.
5. Reconcile the identity inventory with the authoritative topology.
6. Record remediation or a named bounded acceptance for every #124 residual.
7. Define and separately test the future watermark, synchronization boundary,
   rollback triggers/deadline, observation window, and one-writer controls.
8. Name the final approver and record the explicit decision.

Until then, keep #125 open and do not begin cutover.
