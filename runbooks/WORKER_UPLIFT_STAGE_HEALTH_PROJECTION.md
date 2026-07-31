# Worker-Uplift Stage Health Projection

This runbook owns the bounded refresh of the authenticated admin health read
model tracked by `ramideltoro/nutsnews-worker#169`.

The refresh does not authorize cutover. Legacy `nutsnews-worker` remains the
production ingestion owner, every uplift service remains shadow-only, and
`production_writes_enabled` must remain `false`.

## Standing authorization

The canonical machine-readable policy is
`docs/worker-uplift-stage-health-projection-authorization.json`. It permits
current and future candidate revisions without a per-release or first-run
named-owner decision only while the validator proves all of these conditions:

- the sole mutation target is
  `worker_uplift_final.stage_health_projections`;
- exactly the declared scheduler, fetcher, canonicalizer, enrichment,
  approval, translation, persistence, and publication rows are upserted;
- the operation is idempotent and evidence older than the stored row cannot
  overwrite newer evidence;
- the scheduler remains explicitly non-consuming and the other seven stages
  each have at least one main-queue consumer;
- the candidate is generated from fresh, value-free runtime status, immutable
  manifest/compose, and main/retry/DLQ queue snapshots;
- the apply job uses the fixed `production-backend` workflow, exact typed
  confirmation, a 30-second SQL timeout, and the dedicated projection role;
- the dedicated role can only select, insert, and update the projection table;
- post-apply proof preserves shadow mode, disabled production writes, legacy
  ownership, consumers, queue counts, deployment digests, and schema shape.

Any contract, table, column, stage-set, operation, privilege, write-policy, or
ownership change invalidates the standing authorization and requires a new
reviewed policy change.

## Access bootstrap

The protected backend Ansible baseline owns the login role
`nutsnews_worker_uplift_projection`. It reuses the existing protected migration
validation password source without copying the value into source or artifacts.
The role is distinct from validation, app, Worker API, and service-stage roles.

The baseline explicitly revokes mutation rights on public, every stage schema,
and all final tables before granting only:

- schema usage on `worker_uplift_final`;
- `SELECT`, `INSERT`, and `UPDATE` on `stage_health_projections`;
- sequence usage for `stage_health_projections_id_seq`.

It does not receive delete, truncate, trigger, references, schema-create,
database-create, role-create, superuser, article/domain, or other table
mutation privileges. Provision or reconcile this role only through the normal
protected backend Ansible check/apply workflow after the source change merges.

## Operation classes

### Read-only collection

The dry-run job has no GitHub Environment. It uses the existing read-only
runtime command boundary to collect:

- `nutsnews-worker-runtime status`;
- fixed `queue-inspect` calls for each declared stage and queue kind;
- the deployed runtime manifest and compose document.

The job does not receive PostgreSQL write, RabbitMQ mutation, Cloudflare,
failover, or infrastructure credentials.

### Value-free dry run

Dispatch `Backend Worker-Uplift Stage Health Projection` with:

```text
mode=dry-run
confirmation=
```

Inspect `backend-worker-uplift-stage-health-projection-dry-run`. The JSON must
report `status=pass`, `row_count=8`, `runtime_mode=shadow`,
`production_writes_enabled=false`, `active_ingestion_owner=legacy_shards`, and
no missing or unverifiable consumer. Validate it with:

```bash
python3 scripts/validate_worker_uplift_stage_health_projection.py \
  --artifact projection-candidate.json
sha256sum --check projection-candidate.sha256
```

The dry-run artifact contains image digests, safe counts, timestamps, and
cryptographic source digests only. It must not contain credentials, connection
strings, private endpoints, message payloads, article content, or personal
data.

### Protected apply

Dispatch the same workflow from the exact merged commit with:

```text
mode=apply
confirmation=refresh-worker-uplift-stage-health-projections
```

The approval-free dry-run job always runs first. The protected job downloads
that exact artifact, verifies its SHA-256, revalidates the policy and typed
confirmation, proves the database-role privilege boundary, and performs one
fixed eight-row `INSERT ... ON CONFLICT (stage_name) DO UPDATE` transaction.

An exact `production-backend` environment wait for this workflow can be
approved through the GitHub API under the standing authorization. Do not
approve a different workflow, confirmation, target, row set, or source commit
under this policy.

## Artifact inspection

Inspect the retained
`backend-worker-uplift-stage-health-projection` artifact, not only the workflow
conclusion:

- `projection-candidate.json` binds the exact runtime candidate;
- `projection-candidate.sha256` binds the dry-run file;
- `projection-apply-report.json` records the fixed target, row count,
  least-privilege proof, unchanged schema fingerprint, and exact database rows;
- `projection-post-apply-proof.json` compares pre/post runtime, consumers,
  queue counts, manifest, compose, write policy, and ownership;
- `projection-evidence.sha256` binds the retained evidence files.

The apply is successful only when all three JSON reports pass, the database
contains exactly eight candidate-matching rows, and all SHA-256 checks pass.
After downloading the artifact, verify its portable digest manifest with
`sha256sum --check projection-evidence.sha256`.

## Failure and recovery

The workflow fails before mutation when evidence is stale, a service is not
healthy, a required consumer is zero/unverifiable, a version is mutable, the
row set differs, the role has another mutation grant, or the confirmation is
wrong.

If apply fails before the transaction commits, fix the evidence or role drift
and rerun from a new dry run. Do not use arbitrary SQL. If post-apply proof
fails, preserve the artifacts, run the approval-free worker runtime status, and
diagnose the reported invariant. A later current artifact may safely replay the
upsert; older evidence is rejected by both the preflight and SQL conflict
guard.

This path never calls restart, deploy, scale, drain, replay, cutover, DNS, or
failover operations. Use the existing protected worker-runtime workflows for
an independently authorized recovery action.

## #163 completion

After a passing projection apply, rerun the protected read-only authenticated
production-admin evidence from the exact deployed frontend candidate. Close
`#169` and `#163` only after the sanitized admin artifact proves authorized
access, unauthenticated rejection, current eight-stage health, consumer and
queue/DLQ state, exact candidates, disabled uplift writes, and legacy ingestion
ownership.

This evidence does not change the `#125` NO-GO decision and does not authorize
`#150`, cutover, or production domain writes.
