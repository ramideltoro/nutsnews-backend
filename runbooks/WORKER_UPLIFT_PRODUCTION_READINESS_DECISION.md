# Worker-Uplift Production Readiness Decision

Tracking issue: `ramideltoro/nutsnews-worker#125`

This runbook interprets
`docs/worker-uplift-production-readiness-decision.json`. The current decision
is **NO-GO**. It is a non-mutating decision about whether guarded
cutover-control implementation may begin. It is not cutover authority.

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

## Corrected dependency graph

The gate order is:

1. `#125` evaluates whether current evidence is sufficient to begin guarded
   control implementation.
2. A named GO on `#125` may authorize `#150` implementation only.
3. `#150` must complete before `#126`.
4. `#126` implements and rehearses reversible controls without cutting over.
5. `#166` is the final non-mutating cutover-execution readiness gate. It must
   verify the implemented controls, current evidence, rollback rehearsal,
   named approver, and exact production candidate.
6. Only a GO on `#166` may unblock the separately protected execution in
   `#127`.

Missing implementation of `#150` or `#126` is deliberately not a blocker to
`#125`, because those issues follow it. A `#125` GO does not authorize `#126`
apply mode, `#127`, production writes, or an ingestion-owner change.

## Current disposition

| Area | Status | Evidence or blocker |
| --- | --- | --- |
| Single writer/write policy | Pass | Legacy commit and false write/visibility gates are pinned in the decision |
| Exact images/packages/contracts | Pass | Eight image digests and four package releases/attestations are pinned |
| 48-hour soak/capacity/cost | Pass | Run `30405550709`, 72.43 hours and 415 events on the current image set |
| Zero-consumer recovery/drain | Pass | Runs `30404840645`, `30405237965`, and `30405294851` |
| Grafana metrics/logs/alerts | Pass | Infra apply plus protected metrics/logs artifacts are pinned |
| Operations guide | Pass | Docs merge `77eeb52078878c2f95989db3107b814e54c52222`; it denies cutover authority |
| Runtime identity inventory | Block | Dedicated inventory uses superseded queue/permission names; `#160` owns reconciliation |
| Parity | Block | Run `30203441579` used eight superseded image digests; `#158` owns the current-candidate rerun |
| Empty-broker recovery | Block | Run `30215207093` predates the current topology hash; `#159` owns the current-topology drill |
| Dependency outages | Block | Current-candidate PostgreSQL/API/Qwen artifacts are missing; `#161` owns the protected drills |
| Backup/isolated restore | Block | Current candidate-tied backup and isolated restore artifacts are missing; `#162` owns the evidence and links infra implementation findings |
| Protected runtime status | Block | Read-only run `30513933114` is waiting for `production-backend` approval and artifact inspection |
| Admin deployed proof | Block | Source and access control are present; `#163` owns current authenticated production projection proof |
| Security residuals | Block | `SEC-124-002` through `SEC-124-009` lack named disposition; `#164` owns the record |
| Cloudflare failover | Block | Deployed and source config omit `FAILOVER_ANALYTICS`; worker tracker `#157` owns the infra fix/decision |
| Control implementation plan | Block | Exact planned watermark, rollback deadline, observation window, thresholds, and named owners are missing; `#165` owns the non-mutating plan |
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
  `ramideltoro/nutsnews-worker#157`, implemented in `nutsnews-infra`;
- current parity, empty-broker, identity, and outage evidence belongs to
  `#158` through `#161`;
- backup/restore and admin proof belongs to `#162` and `#163`;
- named security dispositions and the control implementation plan belong to
  `#164` and `#165`;
- implementation of scheduling and reversible owner/write controls belongs
  to downstream issues `#150` and `#126`, not #125.

## Cloudflare failover rule

The current read-only API evidence proves:

- `nutsnews-dns-failover` version 30 receives 100% of traffic;
- `DNS_FAILOVER` binds `DnsFailoverController`;
- the once-per-minute cron is deployed;
- `FAILOVER_ANALYTICS` is not a deployed binding;
- current infra source also omits the binding and has no analytics writer.

Documentation is not proof of a deployed binding. Readiness remains blocked
until `ramideltoro/nutsnews-worker#157` attaches value-free protected
deployment/query
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
7. Define the exact planned watermark, synchronization boundary, rollback
   deadline, observation window, thresholds, and named ownership roster in
   `#165`; do not require the downstream controls to exist yet.
8. Allow read-only run `30513933114` to complete only through normal
   `production-backend` approval, inspect its artifact, and incorporate the
   result.
9. Name the current authorized approver and record an explicit #125 decision.
   GO may authorize beginning `#150` only.
10. After `#150` and `#126`, require `#166` to revalidate the exact production
    candidate and rollback rehearsal before `#127`.

Until then, keep #125 open and do not begin #150 or any cutover work.
