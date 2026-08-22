# Backend Apply Runbook

Use this runbook for the GitHub Actions workflow that automatically applies backend host changes after a successful merge to `main`. The `production-backend` Environment scopes credentials only; it has no required reviewers, wait timer, or deployment-branch protection.

## What This Owns

The backend workflow is the normal mutation path for `65.75.201.18`.

It must own or orchestrate backend host changes for:

- baseline package metadata refresh and maintenance-state reporting
- controlled security-update and reboot handling through `Backend Controlled Maintenance`
- SSH configuration and access hardening
- UFW firewall policy
- fail2ban or equivalent SSH abuse protection
- swap or zram baseline
- backend runtime and reverse proxy deployment
- environment and secret placement
- backup and restore configuration
- monitoring, health checks, log retention, and drift checks

## Required GitHub Environment

Create a GitHub Environment named `production-backend` in `ramideltoro/nutsnews-backend`.

The Environment must not require reviewers, approvals, or a wait timer. Automated main-branch deployment starts immediately and uses the Environment only to access scoped secrets.

The credential bootstrap path is documented in [CREDENTIAL_BOOTSTRAP.md](CREDENTIAL_BOOTSTRAP.md). Use it to create or update the Environment, set non-secret variables, and load Environment secrets from local-only values.

## Required Environment Secrets

Add these to the `production-backend` Environment:

| Secret | Purpose |
| --- | --- |
| `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY` | Private key allowed to SSH to `65.75.201.18` for backend Ansible runs |
| `NUTSNEWS_BACKEND_KNOWN_HOSTS` | Verified `known_hosts` entry for `65.75.201.18` |
| `GRAFANA_CLOUD_PROMETHEUS_URL` | Grafana Cloud Prometheus remote-write endpoint |
| `GRAFANA_CLOUD_PROMETHEUS_USERNAME` | Metrics-publisher identity |
| `GRAFANA_CLOUD_PROMETHEUS_PASSWORD` | Metrics-publisher credential |
| `GRAFANA_CLOUD_LOKI_URL` | Grafana Cloud Loki write endpoint |
| `GRAFANA_CLOUD_LOKI_USERNAME` | Logs-publisher identity |
| `GRAFANA_CLOUD_LOKI_PASSWORD` | Logs-publisher credential |
| `NUTSNEWS_WIKI_AI_API_KEY` | Dedicated bearer credential for the authenticated Wiki AI Responses endpoint |

Optional secrets:

| Secret | Purpose |
| --- | --- |
| `NUTSNEWS_BACKEND_ANSIBLE_USER` | Overrides the inventory SSH user if the pipeline uses a dedicated automation account |
| `NUTSNEWS_BACKEND_BECOME_PASSWORD` | Sudo password for the Ansible user if passwordless sudo has not been provisioned |
| `RESTIC_REPOSITORY` | Encrypted restic repository for service-aware backend backups |
| `RESTIC_PASSWORD` | Restic repository encryption password |
| `AWS_ACCESS_KEY_ID` | S3-compatible restic access key when `NUTSNEWS_BACKEND_RESTIC_PROVIDER=s3` |
| `AWS_SECRET_ACCESS_KEY` | S3-compatible restic secret key when `NUTSNEWS_BACKEND_RESTIC_PROVIDER=s3` |
| `AWS_DEFAULT_REGION` | Optional S3-compatible region |

Prefer a dedicated automation user with tightly scoped key-based SSH and passwordless sudo for the reviewed Ansible command set. If a sudo password is used during early bootstrap, rotate it after the automation user exists.

Do not commit secret values.

## Wiki AI runtime

The protected full-baseline path owns the complete Wiki AI installation on the
backend host:

- verified Ollama `0.32.5` amd64 archive installation
- pinned `qwen3.5:4b-q4_K_M` model identity and `nutsnews-wiki-qwen` alias
- 49,152-token context and 6,144-token output ceiling with one loaded model and
  one active request; excess public inference requests are rejected with `429`
- systemd CPU and memory ceilings that preserve capacity for PostgreSQL, Caddy,
  RabbitMQ, and the Worker runtime
- loopback-only Ollama and proxy listeners
- a dedicated bearer-authenticated `/wiki-ai/v1/responses` route and a bounded
  public `/wiki-ai/health` readiness route

The proxy accepts only the configured model, rejects oversized or unauthenticated
requests, strips the credential before forwarding to Ollama, and records only
bounded request metadata. It never logs prompts, source patches, responses, or
credentials. A closed downstream streaming connection aborts its matching
loopback Ollama request and releases the single inference slot, so a canceled
GitHub job cannot leave an abandoned generation blocking the serialized queue.
Do not reuse `LOCAL_AI_API_KEY`; Wiki automation requires the
separate `NUTSNEWS_WIKI_AI_API_KEY` value, which is also stored as
`WIKI_AI_API_KEY` in `ramideltoro/nutsnews-docs`.

Pull-request automation validates the source before merge. A push to exact
`main` automatically runs apply mode and verifies the public health contract
and a real Responses API tool call before the deployment safety postcheck. If
the tool-call qualification fails, roll back the wiki workflow to a
non-publishing state, then revert through a normal tested pull request; never
expose port `11434` or mutate the host manually.

## Run Check Mode

Manual check mode remains available for diagnostics:

1. Open GitHub Actions and select `Backend Ansible Apply`.
2. Run the workflow from exact `main`.
3. Leave `run_mode` as `check` and `confirm_apply` blank.
4. Review the Ansible diff and recap.

Check mode is non-mutating. It is not a deployment approval gate.

## Automatic Apply Mode

Every push to exact `main` automatically selects `run_mode=apply`,
`deployment_scope=full-baseline`, and the fixed
`backend.nutsnews.com` confirmation. No reviewer, approval, or manual
dispatch is required after merge.

Manual apply dispatch remains available for recovery or a scoped helper run.
It must use exact `main`; choose `apply` and the fixed host confirmation.

Production Alloy is a code-controlled enabled desired state. Missing Grafana
write credentials fail the automated workflow closed. Disabling Alloy requires
a desired-state change and the exact
`backend_metrics_alloy_disable_confirmation=DISABLE_PRODUCTION_ALLOY`; do not
use credential removal as a disable mechanism.

Generic apply accepts only
`NUTSNEWS_WORKER_UPLIFT_CUTOVER_STATE=shadow` and
`NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=false`. It cannot perform
or preserve a production owner/write transition; use the dedicated automated
cutover-control path for that state.

Routine baseline applies must not be used as a broad OS-upgrade or reboot
shortcut. The backend baseline defaults keep `dist` upgrades, package
autoremove, and reboot disabled. Use the `Backend Controlled Maintenance`
workflow for fixed-purpose `precheck`, `security-upgrade`, or `reboot`
actions.

## Deployment Safety Gates

Apply mode runs the deployment safety gate before and after Ansible. Check mode
runs the same gate as a non-blocking dry run. The gate and rollback map are
documented in [DEPLOYMENT_SAFETY_GATES.md](DEPLOYMENT_SAFETY_GATES.md).

## Read-Only Verification After Apply

After apply, verify from a read-only SSH session:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && uname -a'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'systemctl --failed --no-pager'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'ss -tulpen 2>/dev/null || ss -tulpn'
```

Issue-specific verification commands belong in the issue PR that adds the relevant role or service.

## Break-Glass Rule

Manual SSH mutation is allowed only for emergency recovery when the pipeline cannot restore safe access or service health.

If break-glass is used:

1. Record why it was required.
2. Record every command or file changed.
3. Verify the recovered state.
4. Open a concrete issue or PR that reconciles the server state back into this repository.
5. Update shared docs with the lesson learned.

Do not use break-glass to bypass the normal automated workflow.

## Current Blockers

The workflow cannot apply to `65.75.201.18` until the `production-backend` Environment exists and all required secrets are present. No environment approval gate is expected.
