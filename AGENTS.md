# Agent Instructions


## Project


This repository is the source of truth for the NutsNews backend server at `backend.nutsnews.com` / `65.75.201.18`.


- Repository: https://github.com/ramideltoro/nutsnews-backend
- Primary branch: `main`
- Owns backend host bootstrap, host hardening, backend runtime deployment, backend-specific monitoring, backend DNS/routing implementation, database failover design, and backend runbooks.
- Does not own the public web app, primary VPS platform, or shared documentation.


## Repository Boundaries


- App work belongs in `ramideltoro/nutsnews`.
- VPS/GitOps/shared infrastructure work belongs in `ramideltoro/nutsnews-infra`.
- Shared docs/runbooks belong in `ramideltoro/nutsnews-docs`.
- Backend server work for `65.75.201.18` belongs here.


## Before Editing


- Read this file and the relevant README, docs, runbook, issue, or workflow before making changes.
- Run `git status --short` before editing.
- Preserve user changes. Do not overwrite, delete, or revert work you did not make unless explicitly instructed.
- Keep repo boundaries explicit when a change touches another NutsNews repository.


## Repository Rules


- Do not add secrets to this repository.
- Do not commit private keys, tokens, passwords, Terraform state, `.tfvars`, local environment files, database dumps, provider credentials, or generated server fact snapshots.
- All routine backend host changes must flow through commit, pull request, checks, merge, and protected pipeline apply.
- Do not manually configure the backend server over SSH. Use SSH only for read-only verification or documented break-glass diagnostics.
- Use `ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18` only for read-only backend verification unless a repo-managed pipeline is applying the change.
- Keep server mutations repeatable, idempotent, and pipeline-managed.
- Use GitHub repository or Environment secrets for runtime credentials. Document required secret names, but never commit secret values.
- Require a non-mutating check or dry-run path before server mutation where the chosen tool supports it.
- Require explicit approval for production/backend mutation through a protected GitHub Environment or equivalent approval gate.
- Keep the backend lightweight enough for a cheap solo-maintained VPS. Avoid Kubernetes and heavyweight self-hosted observability unless explicitly approved.
- Keep PostgreSQL, management dashboards, and admin surfaces private or behind a reviewed access boundary.
- Do not expose database ports publicly unless a separate reviewed issue approves the network model.
- Do not prefix PR titles, branch names, commit messages, docs, headings, or generated content with agent branding.
- Every backend operations change must include a matching update in `ramideltoro/nutsnews-docs` unless it is truly local scaffolding with no operational impact yet.


## Validation


- Ansible changes must pass syntax checks and relevant local validation.
- GitHub Actions changes must be reviewed for least privilege, pinned or intentionally versioned actions, secret handling, and safe triggers.
- Shell scripts must use strict mode and idempotent behavior when mutating server state.
- Dashboard and collector changes must validate schema, redaction, and read-only behavior.
- Database changes must include backup, restore, access-control, and rollback considerations before production use.
- Documentation-only changes must pass `git diff --check` at minimum if no stronger validation exists.


## Documentation


- Keep short operational pointers, bootstrap steps, and repo-owned runbooks here.
- Put shared learning, diagrams, recovery context, and cross-repo operating guides in `ramideltoro/nutsnews-docs`.

## Isolated Git Workflow and Cleanup

- Before changing files, fetch the latest remote default branch and create a new task-specific branch in a disposable clone or isolated `git worktree`. Never make task changes in a shared checkout or directly on `main` or `master`.
- Use a fresh branch, worktree, and directory for every task. Do not reuse a prior task's branch or checkout.
- Keep the task checkout isolated from unrelated repositories and user work. Preserve all pre-existing changes.
- After the work is safely committed and pushed, the pull request is opened or merged as required, and validation results are recorded, remove the disposable local checkout to avoid consuming disk space.
- For a disposable clone, verify `git status --short` is clean and all required commits exist on the remote, then delete only that exact clone directory. For a worktree, run `git worktree remove <exact-path>` from the owning repository and then `git worktree prune`.
- Delete the local task branch only after confirming it is merged or no longer needed and no unpushed commits remain.
- Never delete a shared or canonical clone, the current working directory, an unverified path, or a checkout containing uncommitted, untracked, unpushed, or unrelated work. If cleanup cannot be proven safe, stop and report the exact path and blocker instead of deleting it.
