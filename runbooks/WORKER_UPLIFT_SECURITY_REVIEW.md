# Worker-Uplift Security Review

This runbook covers `ramideltoro/nutsnews-worker#124`. The machine-readable
review is `docs/worker-uplift-security-review.json`.

The review permits continued shadow operation only. It does not authorize
cutover or production writes. The legacy worker remains the production
ingestion owner, `production_writes_enabled` remains false, and Cloudflare,
DNS, failover, and cutover state remain unchanged.

## Validate the review

Run the source checks:

```bash
python3 scripts/validate_backend_github_actions_security.py
python3 scripts/validate_worker_uplift_security_review.py
python3 -m unittest tests.test_worker_uplift_security_review
```

`Backend Checks` runs the same checks on pull requests and `main`. The security
review validator fails if a critical or high finding is merely accepted,
required control evidence is missing, an acceptance criterion does not pass, or
the shadow safety invariants change.

Issue `ramideltoro/nutsnews-worker#164` owns the later disposition gate for
`SEC-124-002` through `SEC-124-009`. Validate its current source-controlled
record separately:

```bash
python3 scripts/validate_worker_uplift_security_dispositions.py
python3 -m unittest tests.test_worker_uplift_security_dispositions
```

The normal validator accepts an accurate `pending` record so that CI can carry
the unresolved gate without inventing a decision. Before closing #164, run the
strict form:

```bash
python3 scripts/validate_worker_uplift_security_dispositions.py --enforce-closure
```

That command fails unless all eight findings are either remediated with an
immutable PR merge and successful CI evidence, plus deployment proof when the
runtime or deployed configuration changed, or explicitly accepted by a named
authorized owner. A current pending record keeps #125 NO-GO.

The GitHub Actions validator requires exact commit SHAs for external actions and
exact digests for container actions. Dispatch inputs, repository variables, and
event data may reach a shell only through the step `env` boundary. This keeps
untrusted expression expansion out of generated shell programs.

## Evidence refresh

Review evidence is metadata-only. Record repository commits, workflow run IDs,
artifact IDs and digests, alert counts, package and image digests, port
open/closed results, certificate identity and dates, and pass/fail outcomes.
Never copy credential material, connection strings, authorization headers,
prompt bodies, article bodies, provider responses, or production records into
the review, workflow logs, issues, or pull requests.

Use the normal GitHub repository APIs for branch controls, workflow
permissions, package provenance, signatures, CodeQL, Dependabot, and secret
scanning. Run dependency and source checks from clean locked installs. Scan the
exact image digests recorded by the runtime manifest, then distinguish scanner
severity from runtime reachability.

Host, RabbitMQ, PostgreSQL, credential, runtime, metrics, and log checks must use
the existing protected backend workflows. Do not bypass `production-backend`
approval. A waiting run is not passing evidence. Read-only external checks may
confirm public ports, TLS, health, and authentication rejection; do not invoke
an endpoint that creates even shadow data during this review.

## Finding disposition

Every finding must be one of:

- `remediated`, with verification;
- `not_affected`, with reachability or applicability evidence; or
- `accepted_residual_risk`, with scope, rationale, owner, expiry gate, and a
  required follow-up.

Critical and high findings cannot be accepted. Residual risks in this review
expire at `ramideltoro/nutsnews-worker#125`; production-readiness approval must
either verify remediation or explicitly renew a narrower acceptance.

The generic `owner` strings in the historical #124 artifact are not current
named dispositions. Repository ownership may identify the accountable person,
but it does not imply that person accepted risk. A current residual acceptance
must be an explicit owner-authored #164 issue comment or a source-controlled
signed artifact and must state:

- the exact affected scope and rationale;
- the compensating controls;
- the named authorized GitHub login and decision timestamp;
- a non-expired review and expiry date; and
- the trigger that reopens the finding.

Issue closure, a merged PR, advisory approval metadata, and automation output do
not supply that decision. No disposition authorizes cutover, production writes,
ingestion ownership changes, legacy-worker changes, or DNS/failover changes.

The image scan detections recorded by this review are not reachable from the
service entry point because they are confined to the unused npm CLI dependency
tree or development-only content. That disposition does not make the image
content desirable: production-only multi-stage images remain required before
production-readiness approval.

## #164 remediation builds and shadow deployment

The immutable build record is
`docs/worker-uplift-security-remediation-builds.json`. Validate it before a
shadow deployment:

```bash
python3 scripts/validate_worker_uplift_security_remediation_builds.py
python3 -m unittest tests.test_worker_uplift_security_remediation_builds
```

The validator binds each of the eight source commits to its signed GHCR index
digest, SPDX attestation, SLSA provenance attestation, pull request, CI runs,
publication run, and source-controlled backend runtime candidate. It also
requires the fetcher DNS resolution-to-connect proof. Each service CI builds
the runtime image and proves that npm, npx, npm's global module tree,
TypeScript, and Vitest are absent.

Deploy these digest-pinned candidates only with the existing protected backend
workflows. The Ansible check must precede apply; apply installs the reviewed
runtime manager, manifest, and Compose definition but does not itself recreate
the eight containers. After apply, invoke the protected worker-runtime `deploy`
action separately for each affected service with
`confirm_target=backend.nutsnews.com` and `dry_run=false`. Each artifact must
show only the named service pull/recreate, `mode=shadow`, and
`production_writes_enabled=false`.

After all service-scoped deploys, run the approval-free worker-runtime `status`
action and inspect its artifact for all eight exact source commits and digests,
shadow mode, writes disabled, all services healthy, one consumer on every
required main queue, unchanged legacy ownership, and no queue backlog or DLQ
growth. A successful workflow conclusion without those artifact fields is
insufficient.

The current immutable proof is recorded in the build record:

- Ansible check `30654352991` and apply `30654848549` passed with empty blocker
  lists in the downloaded safety artifacts.
- Protected service deploys `30655582222`, `30655683990`, `30655874225`,
  `30655921484`, `30655959048`, `30655999806`, `30656028849`, and
  `30656073845` each pulled and recreated exactly one named shadow service.
- Read-only status `30656141654`, artifact `8803290341`, proved the eight exact
  digests, eight healthy services, seven active consumers, zero queued or
  unacknowledged messages, shadow mode, and writes disabled.

This evidence remediates `SEC-124-002` through `SEC-124-006`. Findings
`SEC-124-007` through `SEC-124-009` remain pending and keep #164 and #125
blocked until actual remediation or an explicit, bounded, owner-authored
disposition satisfies the strict validator.

This refresh changes only the eight shadow container revisions. It does not
authorize cutover, alter the legacy worker, change ingestion ownership, enable
production writes, or modify DNS or failover behavior. Roll back a failed
service through the protected worker-runtime rollback action to the existing
digest-pinned per-service rollback image; do not change DNS or invoke a writer
path as recovery.

## Operational ownership and recovery

Protected backend workflows remain the only route for deployment, restart,
credential audit, and host recovery. Grafana Cloud management stays in
`ramideltoro/nutsnews-infra`; worker services receive remote-write-only
telemetry credentials. RabbitMQ route identities, PostgreSQL stage roles, and
backend API tokens remain service-scoped.

The source hardening in this review changes only how workflow metadata enters
shell scripts. It does not change operation inputs or host behavior. If a
workflow regression is found, revert the review commit through a pull request
and rerun `Backend Checks`. No host rollback, deployment, DNS change, failover,
or writer-state change is required.

Do not close #124 until the review pull request is merged, current CI passes,
all required protected evidence has completed without bypass, and the issue
contains the evidence and residual-risk dispositions.
