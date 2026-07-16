# Backend Deployment Safety Gates

This runbook covers backend issue #45 for `65.75.201.18`.

## Changing Workflows

Backend-changing workflows are:

- `Protected Backend Ansible Apply`
- `Backend Cloudflare Routing` when `run_mode` is `apply` or `rollback`
- `Backend Controlled Maintenance` when `action` is `security-upgrade` or `reboot`

Read-only workflows such as `Backend Drift Check`, `Backend Health Report`, and
`Backend Credential Readiness` do not mutate the host or providers.

## Gate Model

Safety gate script:

```text
scripts/backend_deployment_safety.py
```

The gate runs fixed read-only checks only. It validates secret presence by name
only and never prints values.

Critical pre/post surfaces include:

- failed systemd units;
- running kernel and latest installed kernel;
- root disk and inode pressure;
- pending reboot state;
- Caddy service and Caddy config validation;
- Docker health when Docker is configured;
- backend local health and public HTTPS health;
- backup freshness and restore verification state;
- active critical alert state;
- required secret presence by name only.

The gate emits JSON and Markdown reports. Missing or unknown critical evidence
fails closed when the workflow is enforcing the gate.

## Workflow Coverage

### Protected Backend Ansible Apply

Check mode runs the deployment gate as a non-blocking dry run. Apply mode runs
the same gate as an enforced preflight before Ansible and an enforced postcheck
after Ansible.

Rollback path:

1. Revert the reviewed change or open a dedicated rollback PR.
2. Run protected check mode.
3. Run protected apply mode only after approval.
4. Verify the postcheck report, drift report, and live read-only SSH evidence.

Break-glass SSH mutation is emergency-only and must be reconciled back into this
repository.

### Backend Cloudflare Routing

Check mode plans DNS state and runs the routing safety gate as a non-blocking
dry run. Apply and rollback modes require confirmation text, Environment
approval, enforced preflight, and enforced postcheck.

Apply rollback path:

1. Run `Backend Cloudflare Routing` with `run_mode=rollback`.
2. Set `confirm_apply=backend.nutsnews.com`.
3. Verify the routing artifact and DNS result.
4. Verify the backend origin health separately through read-only SSH or direct
   origin resolution.

Rollback mode does not require the public endpoint to remain healthy after DNS
deletion, but it still requires host health and required secrets.

### Backend Controlled Maintenance

The controlled maintenance workflow has its own fixed pre/post-check runner:

```text
scripts/backend_controlled_maintenance.py
```

Allowed actions are only `precheck`, `security-upgrade`, and `reboot`.
Mutating actions require `confirm_target=backend.nutsnews.com` and
`production-backend` Environment approval.

Recovery path:

- Security updates are not generally rolled back in place. Recover the affected
  service first, document changed packages, and reconcile the fix through this
  repo.
- Reboot failure recovery starts with provider console or SSH access, then
  verifies boot ID, kernel, failed units, Caddy health, disk pressure, backup
  state, and alert state before any further mutation.

## Current Expected Non-Configured Surfaces

Until the related issues land, backup freshness, restore verification, Docker,
the backend app, and active-alert state may be reported as `not_configured`.
They remain visible in reports. Profiles only block on the surfaces that are
critical for the specific workflow action, so foundational work can still deploy
the missing safety components through the protected path.

## Validation

Run locally:

```bash
python3 -m unittest discover -s tests
git diff --check
actionlint .github/workflows/*.yml
python3 scripts/validate_no_secret_files.py
```

Live validation should use:

- protected check-mode dry run for `Protected Backend Ansible Apply`;
- Cloudflare routing check mode;
- controlled maintenance `precheck`;
- read-only SSH verification after any approved mutation.
