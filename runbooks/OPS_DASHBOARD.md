# Read-Only Ops Dashboard

This runbook covers backend issue #12 for `65.75.201.18`.

## Access Boundary

The dashboard is intentionally not public. Caddy serves it only on:

```text
http://127.0.0.1:8081/
```

Operators access it through an SSH tunnel:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 -L 8081:127.0.0.1:8081 rami@65.75.201.18
```

Then open:

```text
http://127.0.0.1:8081/
```

This keeps dashboard access behind SSH key authentication and avoids exposing a
public admin route before Cloudflare Access or app auth is reviewed.

## Components

- Static assets: `/var/www/nutsnews-ops-dashboard/`
- Sanitized status snapshot: `/var/www/nutsnews-ops-dashboard/status.json`
- Collector: `/usr/local/bin/nutsnews-ops-dashboard-collect`
- systemd service: `nutsnews-ops-dashboard-collect.service`
- systemd timer: `nutsnews-ops-dashboard-collect.timer`
- Caddy route: loopback-only HTTP on `127.0.0.1:8081`

The collector runs fixed read-only local probes only. It does not accept remote
commands or dashboard input, and the UI contains no mutation controls.

## Status Model

The snapshot distinguishes:

- `healthy`
- `warning`
- `critical`
- `not_configured`
- `unknown`

It includes host uptime, boot time, OS/kernel, load, memory, swap, root disk,
root inodes, pending reboot, package update count, service states, backup
freshness, backup verification, restore-drill status, failed units, relevant
timers, backend `/healthz`, PostgreSQL failover restore readiness, replication
lag status for the selected topology, and public TCP listener summaries.

The snapshot excludes secrets, full environment output, private keys, tokens,
raw auth logs, and unrestricted command output.

## Deploy

Deploy only through the protected backend pipeline:

1. Merge the reviewed dashboard PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Approve and run `Protected Backend Ansible Apply` in `apply` mode.
4. Verify the collector timer and snapshot with read-only SSH.
5. Verify the dashboard over an SSH tunnel with HTTP/browser tooling.

## Verification

Read-only server checks:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'systemctl is-active nutsnews-ops-dashboard-collect.timer && test -s /var/www/nutsnews-ops-dashboard/status.json'
```

HTTP check through SSH tunnel:

```bash
curl -fsS http://127.0.0.1:8081/status.json
```

Browser check:

```text
http://127.0.0.1:8081/
```

Expected public TCP ports remain SSH, HTTP, and HTTPS only; port `8081` must be
bound to loopback.

## Rollback

Preferred rollback is a git revert followed by the protected check/apply path.
Because the route is loopback-only, rollback does not require Cloudflare or DNS
changes.
