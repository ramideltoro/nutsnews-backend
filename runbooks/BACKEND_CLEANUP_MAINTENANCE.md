# Backend Cleanup Maintenance

This runbook covers backend issue #41 for safe disk and Docker cleanup.

## What It Does

The `Backend Cleanup Maintenance` workflow runs a fixed-purpose cleanup script
with one of three actions:

- `report`: inventory disk, inode, Docker, temp, apt-cache, log, Caddy, and
  backup cleanup surfaces without running cleanup commands.
- `dry-run`: run only non-mutating candidate listing commands.
- `apply`: run the fixed cleanup command set after `production-backend`
  approval and `confirm_apply=backend.nutsnews.com`.

The workflow does not accept arbitrary remote commands.

## Safe Targets

The allowlisted cleanup targets are:

- stale files older than 7 days in `/tmp` and `/var/tmp`;
- downloaded apt package archives from `/var/cache/apt/archives`;
- dangling Docker images;
- Docker build cache older than 7 days.

The current live inventory on 2026-07-17 showed root disk at `8%`, root inode
use at `2%`, Docker inactive/not installed, no stale temp-file candidates, and
about `469 MB` of apt package archives.

## Protected State

The cleanup script asserts that fixed commands do not touch:

- `/etc`;
- `/home`;
- `/root`;
- `/opt/nutsnews`;
- `/var/lib/caddy`;
- `/var/lib/docker/volumes`;
- `/var/lib/nutsnews/backups`;
- `/var/lib/postgresql`;
- `/var/www/nutsnews-ops-dashboard`.

It never runs `docker volume prune` or `docker system prune --volumes`.

## Reporting

Every workflow run uploads `backend-cleanup-maintenance-report.json` and writes
a GitHub step summary.

The backend health report also checks:

```text
/var/lib/nutsnews/cleanup/last-cleanup.json
```

If an approved apply writes that state file, the health report exposes
`cleanup_last_run`. Until then, the status is `not_configured`.

## Commands

Report only:

```bash
gh workflow run backend-cleanup-maintenance.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=report
```

Dry run:

```bash
gh workflow run backend-cleanup-maintenance.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=dry-run
```

Apply:

```bash
gh workflow run backend-cleanup-maintenance.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f action=apply \
  -f confirm_apply=backend.nutsnews.com
```

Do not run `apply` until the report and dry-run artifacts have been reviewed.

## Validation

```bash
python3 -m unittest tests.test_backend_cleanup_maintenance tests.test_backend_health_report
actionlint .github/workflows/backend-cleanup-maintenance.yml
python3 scripts/backend_cleanup_maintenance.py \
  --action report \
  --ssh-host 65.75.201.18 \
  --ssh-user rami \
  --ssh-key ~/.ssh/servercheap_65_75_201_18 \
  --known-hosts ~/.ssh/known_hosts \
  --output /tmp/backend-cleanup-report.json
```

## Rollback

Preferred rollback is a git revert followed by no further cleanup applies.

If an approved cleanup apply creates an incorrect last-run state file, replace
or remove `/var/lib/nutsnews/cleanup/last-cleanup.json` through a reviewed
protected maintenance change and document the correction in the issue.
