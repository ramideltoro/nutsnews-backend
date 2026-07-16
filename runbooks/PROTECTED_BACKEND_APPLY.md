# Protected Backend Apply Runbook

Use this runbook for the manual GitHub Actions workflow that applies backend host changes through the protected `production-backend` Environment.

## What This Owns

The protected backend workflow is the normal mutation path for `65.75.201.18`.

It must own or orchestrate backend host changes for:

- package updates and reboot handling
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

Optional secrets:

| Secret | Purpose |
| --- | --- |
| `NUTSNEWS_BACKEND_ANSIBLE_USER` | Overrides the inventory SSH user if the pipeline uses a dedicated automation account |
| `NUTSNEWS_BACKEND_BECOME_PASSWORD` | Sudo password for the Ansible user if passwordless sudo has not been provisioned |

Prefer a dedicated automation user with tightly scoped key-based SSH and passwordless sudo for the reviewed Ansible command set. If a sudo password is used during early bootstrap, rotate it after the automation user exists.

Do not commit secret values.

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
