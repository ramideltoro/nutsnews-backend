# Backend Service Baseline Attestation

This runbook covers backend issue #31 for `65.75.201.18`.

## Read-Only Evidence

Originally captured over read-only SSH on 2026-07-16 and refreshed after
protected backend applies through 2026-07-23:

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
| `0.0.0.0` | `80/tcp` | HTTP health and ACME |
| `[::]` | `80/tcp` | HTTP health and ACME |
| `0.0.0.0` | `443/tcp` | HTTPS health |
| `[::]` | `443/tcp` | HTTPS health |

Private/local listeners:

| Address | Port | Purpose |
| --- | --- | --- |
| `127.0.0.53%lo` | `53/tcp`, `53/udp` | local DNS |
| `127.0.0.54` | `53/tcp`, `53/udp` | local DNS |
| `127.0.0.1` | `323/udp` | local chrony |
| `[::1]` | `323/udp` | local chrony |
| `127.0.0.1` | `5432/tcp` | private PostgreSQL failover target |
| `127.0.0.1` | `8081/tcp` | loopback-only read-only ops dashboard |
| `127.0.0.1` | `8082/tcp` | loopback-only Adminer database dashboard |
| `127.0.0.1` | `9085/tcp` | loopback-only Adminer PHP-FPM pool |
| `127.0.0.1` | `5672/tcp` | loopback-only RabbitMQ AMQP |
| `127.0.0.1` | `15672/tcp` | loopback-only RabbitMQ management |
| `127.0.0.1` | `15692/tcp` | loopback-only RabbitMQ Prometheus |

Not deployed at attestation time:

- backend app service
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
- private PostgreSQL failover target
- loopback-only Adminer database dashboard
- backend-owned RabbitMQ broker
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

The validator fails if the attested baseline lists a public TCP port other than
`22`, `80`, or `443`, or any failed systemd units.

## Public Exposure Policy

Current expected public exposure is SSH plus Caddy-managed HTTP/HTTPS health
routing.

Application routes must remain absent until a reviewed reverse-proxy/routing PR
enables app routing, app health checks, TLS behavior, and the compatible UFW
policy.

Database, cache, search, and dashboard ports must not be public.

## Re-Attestation Trigger

Update this attestation after:

- a protected apply changes listeners or services;
- Docker, Caddy, the backend app, database, cache, search, dashboard, or backup services are added;
- a read-only audit finds drift;
- public routing changes.

Do not patch drift manually over SSH. Reconcile desired state through a PR and protected apply.
