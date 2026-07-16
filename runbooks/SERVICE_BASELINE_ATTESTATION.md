# Backend Service Baseline Attestation

This runbook covers backend issue #31 for `65.75.201.18`.

## Read-Only Evidence

Captured over read-only SSH on 2026-07-16:

```text
host: backend
ssh user: rami
kernel: 7.0.0-28-generic
failed systemd units: 0
cloud-init status: done
```

Public listeners:

| Address | Port | Purpose |
| --- | --- | --- |
| `0.0.0.0` | `22/tcp` | SSH |
| `[::]` | `22/tcp` | SSH |

Private/local listeners:

| Address | Port | Purpose |
| --- | --- | --- |
| `127.0.0.53%lo` | `53/tcp`, `53/udp` | local DNS |
| `127.0.0.54` | `53/tcp`, `53/udp` | local DNS |
| `127.0.0.1` | `323/udp` | local chrony |
| `[::1]` | `323/udp` | local chrony |

Not deployed at attestation time:

- Docker Engine
- Docker Compose
- Caddy
- backend app service
- public HTTP/HTTPS
- PostgreSQL
- Redis or Valkey
- search service

## Repo-Managed Desired State

The repository now represents desired state for:

- SSH hardening
- OS package maintenance and reboot handling
- UFW firewall baseline
- fail2ban SSH protection
- swap safety buffer
- monitoring and log retention
- read-only ops dashboard
- service-aware backup policy and desired state
- protected apply workflow

Live enforcement still requires the protected backend workflow and explicit approval.

## Machine-Readable Inventory

Baseline inventory:

```text
docs/backend-service-baseline.json
```

Validation:

```bash
python3 scripts/validate_service_baseline.py
```

The validator fails if the attested baseline lists a public TCP port other than `22` or any failed systemd units.

## Public Exposure Policy

Current expected public exposure is SSH only.

HTTP/HTTPS must remain closed until a reviewed reverse-proxy/routing PR enables Caddy, health checks, TLS strategy, and the compatible UFW policy.

Database, cache, search, and dashboard ports must not be public.

## Re-Attestation Trigger

Update this attestation after:

- a protected apply changes listeners or services;
- Docker, Caddy, the backend app, database, cache, search, dashboard, or backup services are added;
- a read-only audit finds drift;
- public routing changes.

Do not patch drift manually over SSH. Reconcile desired state through a PR and protected apply.
