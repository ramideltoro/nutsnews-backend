# Worker-Uplift Service Runtime

This runbook covers tracking issue `ramideltoro/nutsnews-worker#85`.

## Scope

The backend service runtime framework lets the backend host deploy and operate
independent worker-uplift service containers without requiring a legacy checkout
of the `ramideltoro/nutsnews-worker` repository. The framework is disabled by
default and shadow-only by default.

Business logic for scheduler, fetcher, canonicalizer, enrichment, approval,
translation, persistence, and publication is added by later service issues.
This issue establishes the protected deployment and operations surface those
services must use.

## Source-Controlled Contract

The Ansible role is:

```text
ansible/roles/backend_worker_runtime
```

The role installs:

```text
/usr/local/sbin/nutsnews-worker-runtime
/etc/nutsnews-worker-uplift/services.json
/opt/nutsnews-worker-uplift/compose.yml
/var/lib/nutsnews/worker-uplift-runtime/reports
```

Validate the framework with:

```bash
python3 scripts/validate_worker_uplift_service_runtime.py
python3 -m unittest tests.test_worker_runtime_manager
```

## Image And Provenance Rules

Every service image must use a lower-case GHCR digest reference:

```text
ghcr.io/<allow-listed-repository>/<service>@sha256:<64 hex>
```

Mutable tags are rejected. Untrusted repositories are rejected. Each service
entry must include signed provenance metadata whose `subject_digest` matches the
image digest and whose source repository is allow-listed.

Secret values are not stored in the service manifest, Compose file, image
layers, or workflow artifacts. Service secrets are mounted as root-owned files
under `/run/secrets/...` and service environment variables may point at those
paths with `*_FILE` names.

## Protected Apply

Enable the framework only through a reviewed backend PR and protected apply:

```text
NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED=true
```

The protected apply still refuses production writes:

```text
NUTSNEWS_BACKEND_WORKER_RUNTIME_PRODUCTION_WRITES_ENABLED=false
```

Production domain writes require a later protected backend API cutover state.
The current framework does not enable production writes by default and does not
depend on a legacy Worker checkout.

Any one service can be added or updated by editing its source-controlled service
entry and running:

```text
Protected Backend Ansible Apply -> check -> apply after production-backend approval
```

RabbitMQ and unrelated service containers are not redeployed by the service
runtime operations workflow. Service deploy, restart, scale, and rollback use
`docker compose up/restart` scoped to the selected service name.

## Fixed Operations

Use `Backend Worker Runtime Operations`.

Read-only actions:

- `check`
- `status`
- `logs`
- `queue-inspect`
- `dlq-inspect`

Mutating actions require `confirm_target=backend.nutsnews.com` and the
`production-backend` approval gate:

- `deploy`
- `promote`
- `restart`
- `scale`
- `rollback`
- `dlq-replay`
- `drain`
- `reconciliation`
- `smoke`

The workflow validates action names, service names, replica counts, queue kind,
and log tail size before SSH. It never accepts a free-form remote command. It
calls only:

```text
sudo -n /usr/local/sbin/nutsnews-worker-runtime <fixed-action>
```

Reports are written under:

```text
/var/lib/nutsnews/worker-uplift-runtime/reports
```

The workflow uploads `backend-worker-runtime-report`.

## Queue, DLQ, Drain, And Reconciliation

Queue and DLQ inspection reads only declared queues from the source-controlled
service manifest. Promote fails closed until the backend API protected cutover
state exists. DLQ replay and reconciliation fail closed unless a later
service-specific replayer/reconciler is approved with idempotency evidence.

Drain scales only the selected service to zero. Operators should inspect queue
depths and DLQ state before drain, then resume only after service health and
smoke evidence are green.

## Grafana And Failover Boundaries

No backend workflow provisions Grafana resources. Grafana folders, dashboards,
alerts, synthetics, contact points, quotas, and drift stay owned by
`ramideltoro/nutsnews-infra`.

DNS failover controller operations also remain outside this runtime. They stay
owned by the infrastructure failover controller path.

## Rollback

Preferred rollback is a backend PR that reverts or replaces the affected service
manifest entry, followed by protected check/apply. The `rollback` action may
then redeploy only the selected service from the source-controlled rollback
metadata.

Before cutover, the active legacy ingestion path remains unchanged and remains
the rollback path for production ingestion.
