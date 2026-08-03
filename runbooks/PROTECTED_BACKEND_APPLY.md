# Protected Backend Apply Runbook

Use this runbook for the manual GitHub Actions workflow that applies backend host changes through the protected `production-backend` Environment.

## What This Owns

The protected backend workflow is the normal mutation path for `65.75.201.18`.

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

The Environment must require explicit approval before jobs can access secrets or mutate the backend server.

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

Run check mode from exact `main` before apply. Apply mode verifies the public
health contract and a real Responses API tool call before the normal deployment
safety postcheck. If the tool-call qualification fails, do not enable the wiki
workflow. Roll back the wiki workflow to a non-publishing state first, then
revert the backend change through a normal PR and protected check/apply cycle;
never expose port `11434` or mutate the host manually.

## Run Check Mode

1. Open GitHub Actions.
2. Select `Protected Backend Ansible Apply`.
3. Select `Run workflow`.
4. Leave `run_mode` as `check`.
5. Leave `confirm_apply` blank.
6. Approve the `production-backend` Environment gate if prompted.
7. Review the Ansible diff and recap.

Check mode is required before apply mode.

Check mode must stay non-mutating. When a package such as `fail2ban` or
`sysstat` is not installed yet, Ansible may report that it would install the
package, then skip dependent service management until apply mode creates the
service unit. Swapfile permission and format steps follow the same pattern when
the swapfile would only be created by a real apply.

## Run Apply Mode

1. Run check mode first and review the output.
2. Select `Protected Backend Ansible Apply`.
3. Set `run_mode` to `apply`.
4. Set `confirm_apply` to `backend.nutsnews.com`.
5. Approve the `production-backend` Environment gate.
6. Review the final Ansible recap.

Apply mode must never be run from a pull request branch.

Production Alloy is a code-controlled enabled desired state. Missing Grafana
write credentials fail the workflow closed. Disabling Alloy requires a
reviewed desired-state change and the exact
`backend_metrics_alloy_disable_confirmation=DISABLE_PRODUCTION_ALLOY`; do not
use credential removal as a disable mechanism.

Generic apply also accepts only
`NUTSNEWS_WORKER_UPLIFT_CUTOVER_STATE=shadow` and
`NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=false`. It cannot perform
or preserve a production owner/write transition; use only the dedicated,
reviewed cutover-control path for that state.

Routine baseline applies must not be used as a broad OS-upgrade or reboot
shortcut. The backend baseline defaults keep `dist` upgrades, package autoremove,
and reboot disabled. Use the `Backend Controlled Maintenance` workflow for
fixed-purpose `precheck`, `security-upgrade`, or `reboot` actions, each under the
same protected `production-backend` approval gate.

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

Do not use break-glass to bypass normal review or approval.

## Current Blockers

The workflow cannot apply to `65.75.201.18` until the `production-backend` Environment exists, required secrets are present, and the environment approval gate is available.
