# Worker-Uplift Service Runtime

This runbook covers tracking issue `ramideltoro/nutsnews-worker#85`.
It also records the non-AI shadow deployment from
`ramideltoro/nutsnews-worker#117` and the approval/translation shadow deployment
preparation from `ramideltoro/nutsnews-worker#118`, plus the
persistence/publication final-shadow deployment from
`ramideltoro/nutsnews-worker#119`.

## Scope

The backend service runtime framework lets the backend host deploy and operate
independent worker-uplift service containers without requiring a legacy checkout
of the `ramideltoro/nutsnews-worker` repository. The framework is disabled by
default and shadow-only by default.

Business logic for scheduler, fetcher, canonicalizer, enrichment, approval,
translation, persistence, and publication is added by later service issues.
This issue establishes the protected deployment and operations surface those
services must use.

The #117 deployment registers only the non-AI services:

| Service | Image source | Health endpoint | Shadow input/output |
| --- | --- | --- | --- |
| `scheduler` | `ramideltoro/nutsnews-worker-feed-scheduler` | `127.0.0.1:18081/ready` | publishes `nutsnews.worker.fetch.v1` |
| `fetcher` | `ramideltoro/nutsnews-worker-feed-fetcher` | `127.0.0.1:18082/ready` | consumes fetch, publishes canonicalization |
| `canonicalizer` | `ramideltoro/nutsnews-worker-article-canonicalizer` | `127.0.0.1:18083/ready` | consumes canonicalization, publishes enrichment |
| `enrichment` | `ramideltoro/nutsnews-worker-article-enrichment` | `127.0.0.1:18084/ready` | consumes enrichment, publishes the approval queue |

No approval, translation, persistence, or publication container is deployed by
#117. A successful fixture may reach `nutsnews.worker.approval.v1`, where it
must stop until the later AI approval deployment is reviewed.

The #118 deployment preparation adds the AI-backed shadow services:

| Service | Image source | Health endpoint | Shadow input/output |
| --- | --- | --- | --- |
| `approval` | `ramideltoro/nutsnews-worker-article-approval` | `127.0.0.1:18085/ready` | consumes approval, publishes translation tasks for accepted fixtures |
| `translation` | `ramideltoro/nutsnews-worker-article-translation` | `127.0.0.1:18086/ready` | consumes translation tasks, stops before persistence |

No persistence or publication container is deployed by #118. A successful
translation fixture may reach `nutsnews.worker.persistence.v1`, where it must
stop until the persistence deployment is reviewed.

The #119 deployment preparation adds the final-shadow services:

| Service | Image source | Health endpoint | Shadow input/output |
| --- | --- | --- | --- |
| `persistence` | `ramideltoro/nutsnews-worker-article-persistence` | `127.0.0.1:18087/ready` | consumes persistence, writes final shadow aggregate state, publishes publication readiness |
| `publication` | `ramideltoro/nutsnews-worker-article-publication` | `127.0.0.1:18088/ready` | consumes publication readiness, records shadow comparison decisions only |

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

#117 also allows direct `secret_env` entries for services whose released
configuration currently expects direct connection-string variables instead of
`*_FILE` variables. Those values are assembled only inside the protected
workflow from production-backend Environment secrets and rendered into root-only
service env files with `no_log: true`. The service manifest contains only the
secret names, never the values.

The non-AI services run with Docker host networking and bind their HTTP servers
to unique loopback ports. This preserves the existing backend posture where
PostgreSQL and RabbitMQ stay bound to `127.0.0.1`; no worker health port is
published publicly.

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

The protected apply builds these service credentials from existing
production-backend secrets:

- PostgreSQL URLs use the stage-specific worker-uplift roles against
  `nutsnews_primary_shadow`.
- RabbitMQ URLs use the scheduler publisher identity and each forwarding
  stage's runtime identity.
- The scheduler receives the backend API token only for shadow feed-source
  reads; backend API writes remain disabled.
- Approval and translation receive Qwen base URL/API key values only from
  production-backend `LOCAL_AI_URL` and `LOCAL_AI_API_KEY`. Protected apply fails
  closed when `LOCAL_AI_API_KEY` is absent.
- Persistence and publication receive separate scoped Worker API tokens only
  when `NUTSNEWS_BACKEND_WORKER_UPLIFT_SCOPED_TOKENS_ENABLED=true`. The
  persistence service receives `NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN`
  and the publication service receives
  `NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN`; both must be distinct from
  `NUTSNEWS_BACKEND_API_TOKEN`.

### Qwen credential inventory reconciliation

`LOCAL_AI_API_KEY` is the active protected source credential; it was not
replaced by new GitHub Environment secret names. Protected apply materializes
that source under service-specific runtime names:

| Service | Protected runtime secret | Service environment key |
| --- | --- | --- |
| approval | `approval-qwen-api-key` | `NUTSNEWS_APPROVAL_QWEN_API_KEY` |
| translation | `translation-qwen-api-key` | `NUTSNEWS_TRANSLATION_QWEN_API_KEY` |

The names are service-specific at the container boundary, but the current
deployment intentionally has one backend-owned Qwen gateway source credential.
Do not retire `LOCAL_AI_API_KEY` while this mapping remains active. A future
split into independently scoped gateway credentials must first add both
replacement secrets, update protected apply, pass protected check/apply, and
verify both services before the shared source is removed. The legacy Cloudflare
binding is independent and remains retained while the legacy Worker owns
production ingestion.

## Fixed Operations

Use `Backend Worker Runtime Operations`.

Read-only actions:

- `check`
- `status`
- `logs`
- `queue-inspect`
- `dlq-inspect`

These five actions run in the workflow's `read-only-runtime` job. That job has
no GitHub Environment reference, so it cannot create a
`production-backend` pending deployment. It uses only the repository Actions
secrets `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY` and
`NUTSNEWS_BACKEND_KNOWN_HOSTS`; the SSH username remains optional and defaults
to `rami`. Read-only dispatches require `dry_run=true`, reject
`confirm_target` and replica inputs, retain bounded output and strict SSH
host-key verification, and upload the same value-free
`backend-worker-runtime-report` artifact.

Mutating or potentially mutating actions run only in the
`protected-runtime` job and require
`confirm_target=backend.nutsnews.com` plus the `production-backend` approval
gate:

- `deploy`
- `promote`
- `restart`
- `scale`
- `rollback`
- `dlq-replay`
- `drain`
- `reconciliation`
- `smoke`

The environment reviewer rule remains enabled. The workflow first validates
the complete dispatch without secrets, then routes the action through
mutually exclusive allow-lists. A protected action cannot enter the
unprotected job, and a read-only action cannot enter the protected job.
Protected execution always passes `--confirm-action`; a missing or mismatched
typed confirmation fails before SSH.

The workflow validates action names, service names, replica counts, queue
kind, dry-run mode, typed confirmation, and log tail size before SSH. It never
accepts a free-form remote command. Both paths call only:

```text
sudo -n /usr/local/sbin/nutsnews-worker-runtime <fixed-action>
```

Reports are written under:

```text
/var/lib/nutsnews/worker-uplift-runtime/reports
```

The workflow uploads `backend-worker-runtime-report`.

Repository validation:

```bash
python3 scripts/validate_backend_worker_runtime_operations_workflow.py
python3 -m unittest tests.test_backend_worker_runtime_operations_workflow
```

After changing the workflow, dispatch `status` from merged `main` with
`dry_run=true`. The run must start without a pending deployment, complete
through `Read-only worker runtime evidence`, and emit a report with
`action=status`, `status=pass`, `mode=shadow`,
`production_writes_enabled=false`, healthy services, and positive consumers
on every required queue. A mutating action still requires the protected job;
do not exercise one merely to test the environment boundary.

### Zero-Consumer Detection And Recovery

`status` evaluates `/ready` for every configured service and RabbitMQ consumer
count for every queue listed in that service's `queues.consumes`. The scheduler
is producer-only and reports consumer readiness as `not_applicable`. A consuming
service is not ready when its main queue has zero consumers; `status` and a
main-queue `queue-inspect` return failure evidence for that condition.

Use a protected restart only when the source-controlled image, configuration,
and credentials are still correct and the evidence shows a runtime-only
consumer loss, such as broker cancellation or a dropped channel that did not
self-recover:

1. Run `status`, `queue-inspect`, and bounded `logs` through `Backend Worker
   Runtime Operations`.
2. Select `restart` for only the affected service, provide
   `confirm_target=backend.nutsnews.com`, and obtain `production-backend`
   approval.
3. Rerun `status` and `queue-inspect`; require `/ready` healthy, at least one
   main-queue consumer, no DLQ growth, and the queued work drained.

Use deployment recovery when the image or configuration must change. Merge the
service and backend manifest PRs first, run protected backend Ansible check and
apply to install the reviewed manager/manifest/Compose state, then select
`deploy` for only the affected service through `Backend Worker Runtime
Operations`. Require the same status, queue, DLQ, log, and drain proof after
deployment. Do not substitute an ad hoc SSH, Compose, or host command for either
path.

Both recovery paths preserve `production_writes_enabled=false`, leave the
legacy ingestion owner running, and do not change DNS or failover behavior.
Grafana alert changes remain owned by `ramideltoro/nutsnews-infra`.

## #117 Shadow Verification

After protected check/apply:

```bash
sudo -n /usr/local/sbin/nutsnews-worker-runtime status
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name scheduler
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name fetcher
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name canonicalizer
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name enrichment
```

Expected state:

- all four services are healthy on `backend.nutsnews.com`;
- each service uses only its declared RabbitMQ queue route and PostgreSQL stage
  schema;
- a bounded fixture advances through enrichment and stops at the approval queue;
- `NUTSNEWS_BACKEND_API_WRITES_ENABLED=false` remains present in service env
  files;
- legacy Cloudflare ingestion remains active and unchanged;
- container logs appear under the existing `nutsnews-worker-uplift-*` journald
  tags and RabbitMQ queue metrics continue to scrape from backend Alloy.

## #118 AI Shadow Verification

Before applying #118, confirm `LOCAL_AI_API_KEY` exists in the
production-backend Environment. Do not use the backend maintenance
`OPENAI_API_KEY`; OpenAI fallback must remain disabled.

After protected check/apply:

```bash
sudo -n /usr/local/sbin/nutsnews-worker-runtime status
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name approval
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name translation
sudo -n /usr/local/sbin/nutsnews-worker-runtime smoke --service-name approval --confirm-action
sudo -n /usr/local/sbin/nutsnews-worker-runtime smoke --service-name translation --confirm-action
```

Expected state:

- approval and translation are healthy and independently restartable;
- approval accepted fixtures create required translation tasks for
  `fr,ja,de-CH,de,el`;
- rejected approval fixtures do not create translation tasks;
- translation outputs are durable shadow state only and stop before the
  persistence queue;
- Qwen slowdown produces bounded backlog through `CONCURRENCY=1`, `PREFETCH=2`,
  and approval `QWEN_MAX_QUEUED_CALLS=1`;
- `NUTSNEWS_APPROVAL_OPENAI_FALLBACK_ENABLED=false` remains present;
- logs include provider/model/prompt metadata but no prompt or article body
  leakage;
- legacy Cloudflare ingestion, legacy AI, and failover remain active and
  unchanged.

The approval smoke report records redacted `db_checks` for
`accepted_decisions`, `accepted_translation_outbox`, `rejected_decisions`,
`rejected_translation_outbox`, `processed_inbox`, and `provider_metadata`.
The translation smoke report records `accepted_language_records`,
`distinct_languages`, `persistence_outbox`, `processed_inbox`, and
`provider_metadata`. These checks use synthetic `example.test` references and
do not query or print production article bodies.

For model outage, drain only the affected AI service, inspect its queue/DLQ, and
leave non-AI services running unless queue backpressure requires a wider pause.
Rotate `LOCAL_AI_API_KEY` in production-backend, rerun protected check/apply,
then restart `approval` and `translation` through fixed runtime operations.

## #119 Final Shadow Verification

Before applying #119, confirm these production-backend variables/secrets exist:

```text
NUTSNEWS_BACKEND_WORKER_API_ENABLED=true
NUTSNEWS_BACKEND_WORKER_UPLIFT_SCOPED_TOKENS_ENABLED=true
NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN
NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN
```

After protected check/apply:

```bash
sudo -n /usr/local/sbin/nutsnews-worker-runtime status
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name persistence
sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect --service-name publication
sudo -n /usr/local/sbin/nutsnews-worker-runtime logs --service-name persistence --tail 200
sudo -n /usr/local/sbin/nutsnews-worker-runtime logs --service-name publication --tail 200
```

Expected state:

- persistence and publication are healthy and independently restartable;
- persistence uses the persistence database role, persistence RabbitMQ consumer,
  and `worker-uplift-persistence` backend API identity;
- publication uses the publication database role, publication RabbitMQ consumer,
  and `worker-uplift-publication` backend API identity;
- publication remains in `shadow_comparison` mode and receives no
  production-write confirmation value;
- persistence production writes remain disabled and final aggregates stay in
  worker-uplift shadow state;
- permission checks continue to deny direct public schema writes and unrelated
  backend API operations;
- legacy Cloudflare ingestion remains the only production writer and public
  visibility/snapshot state is unchanged.

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
