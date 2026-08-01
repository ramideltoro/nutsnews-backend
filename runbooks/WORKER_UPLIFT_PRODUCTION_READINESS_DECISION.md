# Worker-Uplift Production Readiness Decision

Tracking issue: `ramideltoro/nutsnews-worker#125`

The machine-readable decision in
`docs/worker-uplift-production-readiness-decision.json` is **GO**. Its scope is
only permission to begin the guarded scheduling-control implementation in
`ramideltoro/nutsnews-worker#150`.

This decision does **not** authorize cutover, uplift production writes,
ingestion-ownership changes, legacy-worker changes, DNS/failover changes, or
production-infrastructure changes. Final cutover-execution readiness remains a
separate decision in `#166`, after `#150` and `#126` are implemented and
rehearsed. Only that later gate can unblock `#127`.

Legacy `ramideltoro/nutsnews-worker` remains the production ingestion owner.
All eight uplift services remain shadow-only, and
`production_writes_enabled=false`.

## Standing #125 authorization

Repository owner `ramideltoro` supplied explicit standing authorization for
this exact decision scope. The source-controlled contract and validator make
that authorization machine-enforced:

- it authorizes beginning `#150` implementation only;
- it applies to current and future candidate revisions only while every
  machine-validated scope and safety invariant remains exact;
- it fails closed if evidence becomes stale, a blocker reopens, the active
  writer changes, uplift writes or visibility become enabled, or authority is
  expanded;
- it expressly excludes `#166` approval and cutover execution.

No recurring owner comment or per-release approval is required for this #125
scope while those invariants continue to pass. Existing GitHub environment
protections remain unchanged. Exact environment waits for separately
authorized protected workflows continue to use their normal protected path.

## Validation

Run the machine gate and focused negative tests:

```bash
python3 scripts/validate_worker_uplift_production_readiness.py
python3 -m unittest tests.test_worker_uplift_production_readiness
```

The validator fails closed on:

- an approver, authorization source, or `#150`-only scope mismatch;
- any attempt to authorize `#166`, `#127`, cutover, writes, ownership, DNS,
  failover, legacy, or infrastructure mutation;
- any unresolved dependency, blocker, or readiness item;
- stale or mismatched images, packages, contracts, source/config hashes,
  parity, soak, runtime, recovery, outage, restore, admin, security, or
  Cloudflare evidence;
- scheduler local/test adapters or an unbounded/fixed readiness clock;
- a risk waiver, secret-bearing key, connection string, or credential value.

The two committed 2026-07-30 evidence files remain immutable historical
observations. They are retained for diagnostic provenance and value-free
validation, but the GO is based on the newer immutable runs below.

## Exact candidate

The decision pins all repository heads, four immutable package releases,
source/configuration hashes, the RabbitMQ topology and identities, and these
eight deployed images:

| Stage | Source commit | Image digest |
| --- | --- | --- |
| scheduler | `f627a6014a43cadba89cffc31a40565c2d00f001` | `sha256:f489c7bdf60433b44c21a69224b10e62a1b39203119da53378b2018c17ce1452` |
| fetcher | `501ededcad48924b632b0547679f4dcb54ed4a90` | `sha256:7ee6f7a8155b959f01ff265c9b3b8861be5ef5f9bcef5616d5eba88a48e0fbaa` |
| canonicalizer | `64dea55ad8f0e9b67e746b5d4363d43762fea792` | `sha256:6d9fa787412697767a4b8af0eecef43b7c333fdcbae4ad41bda34ce94a2da4c9` |
| enrichment | `c7a464e54eda1ed9e5f82d8dc545b1f7f7a36ec0` | `sha256:c00bec63cc7a410d8ba228e72b0616d561e64d05ae9316f871183fe5b2a840dc` |
| approval | `6597030ffcc07c51fb1259b3152d2645f5927d75` | `sha256:b977adb932647808b73f62dc84bee69a52dca88e4198566d9e0c30180fdb2407` |
| translation | `d1206b73157305128b3bf5dc6bf5a7bcbb4c818d` | `sha256:926dc46ac29f9e6661ee8baa83171aca7a4e9941edbe93af6dbd19708a614da9` |
| persistence | `e646a51bfea5142d769e0980608dc86f6c7623f7` | `sha256:3d158826415f1ad602436aebd21280dd35b1fa34a8f44303b6f39a814ba2b9b9` |
| publication | `47f05a5e177d63c43f18ac203ec773d606a2fbf4` | `sha256:3e9fbebb7e345b7928f94de37e646e4829e81732f5153b6da06246f7618adf4a` |

All entries are `mode=shadow` with production writes disabled. Package
versions, publish runs, attestation IDs, and tarball SHA-256 values are in the
JSON decision.

## Current immutable evidence

### Exact-candidate smoke and parity

- Protected smoke run `30684419368`; artifact `8813411638`, digest
  `sha256:b26eab38ca661f69057b59c3ccafa103d93dadea3648cda80bdfcdb2579a3651`;
  report SHA-256
  `7d9a45715e700d2877618ed27dce5d6da57553e12c39ba9be81c34b74f04931d`.
- Live read-only parity run `30684493608`; artifact `8813470677`, digest
  `sha256:cf52503d66064b07c2d394923031f3f7daf8070c1e4980ad152198bb6f168e33`;
  report SHA-256
  `a8ef46568b03e03180901cb628041adba9dc89d29195a00a496176330b8299d3`.
- Window: `2026-08-01T04:41:52Z` through `2026-08-01T04:48:58Z`.
- All eight images match. Final/ready aggregates are 40/40, failed and blocked
  checks are empty, DLQ growth is zero, legacy endpoints were not invoked, and
  write/cutover flags are false.

### Complete soak, capacity, and cost

- Live read-only run `30684679655`; artifact `8813482408`, digest
  `sha256:6ccd7c7bd5015437d118f9987c2423914efe71421fd85c2fdc88ca0ca57390bf`;
  report SHA-256
  `acad2e6190d7b3ff0f97a39532a150e9e2c9678019b2b788ee28087e935b619c`.
- Window: `2026-07-25T22:13:47Z` through `2026-08-01T04:45:50Z`,
  150.53 hours and 1,389 events.
- The runtime snapshot matches the exact current eight-image candidate: eight
  healthy services, one consumer on every required main queue, zero backlog,
  and no DLQ growth.
- Host evidence: 4 CPUs, load/vCPU 0.237, memory 14.91%, root disk 17.53%,
  and no failed systemd units.
- Model evidence: 317 Qwen calls, 0 OpenAI calls. Legacy owner rows are 8,
  uplift owner rows are 0, and production writes/cutover remain false.

The 150.53-hour aggregate window begins before the scheduler replacement. It
is not represented as 150.53 hours on the new scheduler image. Exact
replacement-scheduler behavior is instead bounded by the fresh smoke/parity
above and current status/log/queue evidence below.

### Runtime and scheduler

- Current status `30683488159`; artifact `8813086274`, digest
  `sha256:6626c2316ca68ea1476606f27de5d959ef940307e286641dcf5e5e4f8bda0111`;
  report SHA-256
  `9c6b03a2553a148f39108e3712ab51f76a874fd1e25e332f8fb456853b1e1e42`.
- Scheduler logs `30683457473` and queue inspection `30683458187` prove the
  production adapter set, one durable lease, one publisher-confirmed shadow
  message, one fetch consumer, and a drained fetch queue.
- Readiness uses the system clock with a five-second freshness bound and only
  RabbitMQ publisher, backend API feed-source, PostgreSQL lease-store, and
  system-clock dependencies. Production mode rejects local/test adapters.

### Recovery, outages, backups, and admin

- `#159` recovery run `30593155799` rebuilt a disposable broker in 20.763
  seconds, restored all seven consumers, replayed one bounded item, drained
  every main queue/DLQ, and produced no duplicate domain/API effects. The live
  production broker was read-only and unchanged.
- `#161` run `30595967083` proves PostgreSQL, backend API, and Qwen failure
  detection and recovery, retry/DLQ policy, and final drain. Later candidate
  changes did not alter the approval/persistence dependency paths exercised by
  that drill; `#168` separately tests the current scheduler adapters.
- `#162` runs `30596331065`, `30596391281`, and `30596869305` prove isolated
  PostgreSQL restore, RabbitMQ clean rebuild, and stopped-volume restore.
  Current scheduler restore run `30683327104` additionally verifies selection
  and restore of one exact immutable snapshot.
- `#163` authenticated admin run `30632243204`, artifact `8793826506`, digest
  `sha256:e4666853abfacf85d0b5df238e0deb929dbcf59c31d5f1d2af40153b72e22509`,
  proves unauthorized rejection and authorized read-only eight-stage status
  against unchanged app source
  `3108ff1def6dca5bf89bbdc8fcfb7db7dcfd291f`.

### Cloudflare, security, observability, and operations

- `#157` protected run `30588814493`; artifact `8777561586`, digest
  `sha256:5939295e1de8e9d0b41dad3a893d01a44deed53556b56d4e7a1068baad300db7`;
  report SHA-256
  `d125b916b8ff01019b537fe5d2746efe21454677732bd33600a9e1bc02369854`.
  It proves value-free deployed `FAILOVER_ANALYTICS` type `analytics_engine`,
  retained `DNS_FAILOVER` Durable Object and once-per-minute cron, 72 sampled
  events, zero query errors, and no DNS/failover state change.
- `#160` reconciles all runtime identities. `#164` has no pending security
  disposition. Its exact-scope standing authorization remains dated and
  fail-closed. `#165` supplies the planning-only watermark, rollback deadline,
  48-hour observation, thresholds, and owner map that later work must
  implement and refresh.
- Grafana alert/log/metric ownership remains in `nutsnews-infra`, and the
  as-built operations guide remains in `nutsnews-docs`. Neither document nor
  this readiness decision authorizes cutover.

## Dependency and authority sequence

The required sequence remains:

```text
#125 GO -> #150 implementation -> #126 implementation/rehearsal
        -> #166 final execution-readiness decision -> #127 protected cutover
```

Missing implementation of `#150` and `#126` was not a circular blocker for
`#125`; those controls follow this gate. Conversely, this GO cannot skip any
downstream issue.

## Revalidation rule

Before beginning or continuing `#150`, rerun this validator. Reopen `#125` and
return to NO-GO if the exact candidate, package/contracts, topology, identity,
write policy, evidence status, single-writer owner, or authorization scope no
longer passes.

Before `#127`, `#166` must independently freeze and verify the exact production
candidate, implemented controls, rollback rehearsal, watermark, absolute
rollback deadline, observation window, current evidence, and separately named
final approver. The standing #125 authorization is deliberately insufficient
for that final gate.
