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

The image scan detections recorded by this review are not reachable from the
service entry point because they are confined to the unused npm CLI dependency
tree or development-only content. That disposition does not make the image
content desirable: production-only multi-stage images remain required before
production-readiness approval.

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
