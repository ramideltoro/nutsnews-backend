# Backend Drift Check

Use this runbook for the protected read-only drift check workflow.

## Workflow

```text
Backend Drift Check
```

The workflow is manual and uses the `production-backend` GitHub Environment so it can access the backend SSH key only after approval.

## What It Checks

The checker runs a fixed set of read-only SSH probes. It cannot run arbitrary commands and does not attempt remediation.

It checks:

- hostname;
- public TCP listeners;
- failed systemd units;
- whether the current SSH user can use passwordless sudo;
- current SSH effective authentication policy where readable;
- Docker, Caddy, backend service, PostgreSQL, and Redis/Valkey presence;
- swap and reboot-required state;
- UFW status when readable;
- managed file presence for repo-owned baseline files;
- `rabbitmq_drift` when the backend-owned RabbitMQ broker is provisioned.

RabbitMQ drift is collected through:

```bash
sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe drift \
  --env /etc/nutsnews-rabbitmq/rabbitmq.env \
  --credentials-env /etc/nutsnews-rabbitmq/topology.env \
  --definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json \
  --metadata /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json
```

The RabbitMQ report checks image digest, Compose/config/topology checksums,
topology, policies, user/permission metadata, listeners/network posture, health,
and backup freshness. It is read-only and does not publish messages, restart the
broker, or alter queue state.

## Classification

Each surface is classified as:

- `expected`;
- `missing`;
- `unexpected`;
- `unknown`.

Unexpected public TCP exposure or failed systemd units are high priority and make the workflow fail.
RabbitMQ high-priority drift also makes the workflow fail.

Missing repo-managed files are expected until the protected backend apply path succeeds. They are reported with `acceptable_until: #10 protected apply succeeds`.

## Secret Handling

The checker uses fixed commands and redacts stdout/stderr before classification. It must not print environment values, private keys, tokens, cookies, connection strings, or arbitrary process output.

The workflow writes:

- a GitHub Step Summary;
- `backend-drift-report.json` as an artifact;
- the same JSON in the workflow log for quick inspection.

RabbitMQ smoke evidence is separate. It is written by the protected
`Backend RabbitMQ Smoke` workflow to
`/var/lib/nutsnews/rabbitmq-probes/last-smoke.json`.

## Rollback

The drift workflow is read-only, so it has no server rollback step.

If the report finds drift, fix it through a repository PR and, where mutation is required, the protected backend apply workflow. Do not patch drift manually over SSH except for documented break-glass recovery.
