# Backend Bootstrap Runbook

Use this runbook for the backend server at `65.75.201.18`.

## Acceptance Criteria Covered

- The repo has a clear bootstrap and deployment entry point.
- The intended runtime and reverse-proxy shape are explicitly chosen.
- A fresh Ubuntu 26.04 server preparation path is documented.
- Manual-only host changes are disallowed except documented break-glass recovery.
- Validation and rollback notes are included.

## Current State

- Hostname: `backend`
- IPv4: `65.75.201.18`
- Target OS: Ubuntu 26.04 LTS
- Read-only SSH command: `ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18`
- No backend app runtime is deployed yet.
- No reverse proxy is deployed yet.
- No PostgreSQL failover target is deployed yet.

## Chosen Implementation Path

Use Ansible for backend host configuration and GitHub Actions for protected check/apply runs.

The normal path is:

1. Add or update backend desired state in this repository.
2. Open a pull request.
3. Run local and CI validation.
4. Merge only after checks pass.
5. Run the protected backend apply workflow in check mode.
6. Review the check output.
7. Run apply mode only after the GitHub Environment approval gate.
8. Verify using read-only SSH and HTTP/browser checks where applicable.

Do not manually install packages, edit system files, change firewall rules, or deploy services over SSH as the routine path.

## Runtime Decision

The backend runtime will use Docker Compose because it is lightweight, reviewable, easy to back up, and adequate for a small solo-maintained VPS.

Caddy is the selected reverse proxy because it keeps TLS and simple route configuration compact. HTTP and HTTPS should not be exposed until the firewall policy, DNS routing, TLS strategy, and health endpoint are reviewed.

The first backend app health endpoint should be `/healthz`. Until an app exists, host-level smoke checks must report app state as `not deployed`.

## Fresh Server Preparation

For a fresh Ubuntu 26.04 host:

1. Confirm the operator has a working key-based SSH path and a provider-console break-glass path.
2. Confirm the protected GitHub Environment exists for backend applies.
3. Add required Environment secrets outside git.
4. Run the backend Ansible workflow in check mode.
5. Review the planned changes.
6. Run apply mode only after the protected approval gate.
7. Keep a second SSH session or provider console available while SSH and firewall changes apply.
8. Run the documented read-only verification commands.

The protected workflow and exact secret list are documented in [PROTECTED_BACKEND_APPLY.md](PROTECTED_BACKEND_APPLY.md).

## Read-Only Verification Commands

Use these commands only for evidence gathering:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && uname -a'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'systemctl --failed --no-pager'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'ss -tulpen 2>/dev/null || ss -tulpn'
```

Commands requiring sudo belong in the protected pipeline or a documented break-glass session.

## Validation

Local scaffold validation:

```bash
git diff --check
```

Ansible syntax validation:

```bash
cd ansible
ansible-playbook playbooks/bootstrap.yml --syntax-check
```

## Rollback Notes

For documentation and scaffold changes, rollback is a normal git revert.

For future server changes:

- Prefer reverting the repo change and rerunning the protected pipeline.
- If SSH/firewall changes risk lockout, keep provider console access available before apply.
- If break-glass SSH or provider console mutation is required, record the incident and reconcile the final server state back into this repository through a pull request.
