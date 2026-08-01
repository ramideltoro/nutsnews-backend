# Backend Observability Failure Drills

This runbook covers the protected backend half of Grafana alert fire-and-resolve
verification. The workflow is dry-run-first and exposes no command, service,
queue, database, or path input.

## Safety Boundary

The five allowed drill identifiers are:

- `worker-unavailable`
- `rabbitmq-zero-consumer`
- `rabbitmq-growing-dlq`
- `postgres-relay-lag`
- `backend-readiness-failed`

`worker-unavailable` is the only drill that changes a running component. It may
stop only the `translation` worker after proving that the runtime, cutover,
backend API, and PostgreSQL write paths are all shadow/write-disabled. Recovery
uses the already installed image (`--pull never`), starts only that service with
no dependencies, and requires its loopback `/live` endpoint before clearing
the drill signal.

The other four drills are telemetry fixtures. They do not create RabbitMQ
queues, publish messages, change relay state, modify PostgreSQL, or alter the
backend API. Grafana rules combine the exact fixture with their normal condition
so the notification path can be verified without production-data mutation.

The local textfile contains exactly five bounded series and no run identifier:

```promql
nutsnews_observability_failure_drill_active{drill="<exact-drill>"} 0|1
```

Alloy's backend-host target adds `deployment_environment="production"`,
`host="backend.nutsnews.com"`, `job="nutsnews-backend-host"`,
`instance="backend.nutsnews.com"`, `service_namespace="nutsnews"`,
`service="host"`, and `environment="production"`. Grafana fixture selectors
must include every one of those exact labels plus the fixed `drill` value. Run
or evidence IDs must never become metric labels.

## Protected Workflow Contract

The workflow is `.github/workflows/backend-observability-failure-drills.yml`.
Its run name is:

```text
Backend observability drill / <drill> / <evidence_id>
```

It accepts exactly these inputs:

| Input | Required value |
| --- | --- |
| `drill` | One of the five identifiers above |
| `evidence_id` | `nnobs-<10-20 digits>-<8 lowercase hex>` |
| `dry_run` | Boolean; defaults to `true` |
| `confirm_repository` | `ramideltoro/nutsnews-backend` |
| `confirm_environment` | `production-backend` |
| `confirm_target` | `backend.nutsnews.com` |
| `confirm_drill` | Exact selected drill identifier |

Both jobs require the canonical repository, a full commit revision, and
`refs/heads/main`. The mutating job uses the protected `production-backend`
environment and a fixed IP, fixed installed hook, fixed 900-second observation
window, and pinned SSH trust material.

## Recovery Guarantees

Before an injection, the host hook writes bounded persistent state and schedules
a fixed transient systemd recovery. The workflow also arms an `EXIT`, `INT`, and
`TERM` recovery trap before calling the inject action. A root-owned persistent
one-minute watchdog recovers an expired state after runner loss or host reboot.

Only one active state is allowed. Recovery must carry the matching evidence ID,
so an old timer cannot clear a newer drill. The hook restores the worker, when
applicable, before atomically returning all five fixture series to zero. A
failed worker recovery leaves the fixture active and returns failure rather than
claiming resolution.

## Evidence

Hook reports are safe-metadata-only JSON. The workflow verifies the action,
drill, evidence ID, schema, status, scalar types, timestamps, and a maximum of 32
bounded check names. Raw SSH output is never uploaded. The combined evidence is
limited to 256 KiB and retained for 120 days.

The infrastructure executor dispatches asynchronously using the evidence ID,
observes the exact Grafana instances while the backend holds the fixture, waits
for backend recovery, and then observes resolution. The backend workflow itself
does not hold Grafana credentials.

## Fail-Closed Conditions

Injection fails without changing state when any of these is true:

- the workflow is not the canonical repository's `main` revision;
- a typed confirmation or evidence ID is invalid;
- another drill is active;
- automatic recovery cannot be scheduled;
- the textfile/state paths or files are unsafe;
- `worker-unavailable` cannot prove every shadow/write-disabled invariant;
- the exact translation worker is not healthy before injection.

No live drill is part of deployment. First apply the backend Ansible role so the
validated hook, zero-state metrics, and watchdog exist. Then deploy the matching
Grafana rules and infrastructure executor. The repository or organization must
allow the workflow's 120-day artifact retention setting.

## Validation

```bash
python3 -m unittest \
  tests.test_backend_observability_failure_drill \
  tests.test_backend_observability_failure_drill_ansible \
  tests.test_backend_observability_failure_drill_workflow
```

Run the protected workflow with `dry_run=true` before requesting approval for a
live drill. Never bypass the protected environment or invoke the host hook with
ad hoc arguments.
