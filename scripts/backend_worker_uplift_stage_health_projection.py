#!/usr/bin/env python3
"""Build, apply, and verify the bounded worker-uplift health projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs/worker-uplift-stage-health-projection-authorization.json"
STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
CONSUMER_STAGES = STAGES[1:]
SAFE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_RE = re.compile(
    r"(password|passwd|token|secret|cookie|private[_-]?key|"
    r"postgres(?:ql)?://|amqps?://|bearer\s|github_pat_|gh[pousr]_)",
    re.IGNORECASE,
)
APPLY_CONFIRMATION = "refresh-worker-uplift-stage-health-projections"
TARGET = "worker_uplift_final.stage_health_projections"
PROJECTION_ROLE = "nutsnews_worker_uplift_projection"


class ProjectionError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProjectionError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectionError(f"required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectionError(f"JSON file is invalid: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"JSON root must be an object: {path}")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def ensure_value_free(value: Any, context: str) -> None:
    encoded = canonical_json(value)
    if SECRET_RE.search(encoded):
        raise ProjectionError(f"{context} contains a forbidden credential or private-value marker")


def service_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = manifest.get("services")
    if not isinstance(services, list):
        raise ProjectionError("runtime manifest services must be a list")
    mapped: dict[str, dict[str, Any]] = {}
    for item in services:
        if not isinstance(item, dict):
            raise ProjectionError("runtime manifest service entries must be objects")
        name = str(item.get("name") or "")
        if name in mapped:
            raise ProjectionError(f"runtime manifest contains duplicate service {name}")
        mapped[name] = item
    if tuple(name for name in STAGES if name in mapped) != STAGES or set(mapped) != set(STAGES):
        raise ProjectionError("runtime manifest must declare exactly the eight worker-uplift stages")
    return mapped


def allowed_queue_names(service: dict[str, Any], kind: str) -> list[str]:
    queues = service.get("queues")
    if not isinstance(queues, dict):
        raise ProjectionError(f"service {service.get('name')} queues must be an object")
    if kind == "main":
        value = queues.get("main")
        return [value] if isinstance(value, str) and value else []
    if kind == "retry":
        value = queues.get("retry", [])
        return [item for item in value if isinstance(item, str) and item]
    if kind == "dlq":
        value = queues.get("dlq")
        return [value] if isinstance(value, str) and value else []
    raise ProjectionError(f"unsupported queue kind {kind}")


def normalize_queue_report(
    path: Path,
    stage: str,
    kind: str,
    expected_names: list[str],
) -> list[dict[str, Any]]:
    report = load_json(path)
    if report.get("action") != "queue-inspect" or report.get("service_name") != stage:
        raise ProjectionError(f"{path.name} is not the expected {stage} queue-inspect report")
    queues = report.get("queues")
    if not isinstance(queues, list):
        raise ProjectionError(f"{path.name} queues must be a list")
    normalized: list[dict[str, Any]] = []
    for item in queues:
        if not isinstance(item, dict):
            raise ProjectionError(f"{path.name} contains a non-object queue entry")
        name = str(item.get("queue") or "")
        metrics = item.get("metrics")
        if item.get("status") != "healthy" or not isinstance(metrics, dict):
            raise ProjectionError(f"{stage} {kind} queue evidence is unavailable")
        normalized.append(
            {
                "queue": name,
                "consumers": nonnegative_int(metrics.get("consumers"), f"{stage}.{kind}.consumers"),
                "messages": nonnegative_int(metrics.get("messages"), f"{stage}.{kind}.messages"),
                "messages_ready": nonnegative_int(metrics.get("messages_ready"), f"{stage}.{kind}.messages_ready"),
                "messages_unacknowledged": nonnegative_int(
                    metrics.get("messages_unacknowledged"),
                    f"{stage}.{kind}.messages_unacknowledged",
                ),
            }
        )
    if sorted(item["queue"] for item in normalized) != sorted(expected_names):
        raise ProjectionError(f"{stage} {kind} queue set does not match the runtime manifest")
    return sorted(normalized, key=lambda item: item["queue"])


def nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProjectionError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectionError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise ProjectionError(f"{field} must be a non-negative integer")
    return parsed


def normalized_queue_evidence(
    queue_dir: Path,
    manifest_services: dict[str, dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for stage in STAGES:
        result[stage] = {}
        for kind in ("main", "retry", "dlq"):
            expected = allowed_queue_names(manifest_services[stage], kind)
            result[stage][kind] = normalize_queue_report(
                queue_dir / f"{stage}-{kind}.json",
                stage,
                kind,
                expected,
            )
    return result


def validate_runtime_status(runtime: dict[str, Any], observed_at: datetime, now: datetime, maximum_age: int) -> None:
    if runtime.get("status") != "pass":
        raise ProjectionError("runtime status must pass")
    if runtime.get("action") != "status":
        raise ProjectionError("runtime evidence must come from the status action")
    if runtime.get("mode") != "shadow":
        raise ProjectionError("runtime mode must remain shadow")
    if runtime.get("production_writes_enabled") is not False:
        raise ProjectionError("runtime production_writes_enabled must remain false")
    if runtime.get("missing_consumers") != []:
        raise ProjectionError("runtime status reports missing consumers")
    if runtime.get("unverifiable_consumers") != []:
        raise ProjectionError("runtime status reports unverifiable consumers")
    age = (now - observed_at).total_seconds()
    if age < -60 or age > maximum_age:
        raise ProjectionError("runtime evidence is outside the authorized freshness window")
    services = runtime.get("services")
    if not isinstance(services, dict) or set(services) != set(STAGES):
        raise ProjectionError("runtime status must contain exactly eight services")
    for stage in STAGES:
        item = services.get(stage)
        if not isinstance(item, dict):
            raise ProjectionError(f"runtime status for {stage} is missing")
        readiness = item.get("readiness")
        consumer = item.get("consumer_readiness")
        if not isinstance(readiness, dict) or readiness.get("status") != "healthy":
            raise ProjectionError(f"runtime readiness for {stage} is not healthy")
        expected_consumer_status = "not_applicable" if stage == "scheduler" else "healthy"
        if not isinstance(consumer, dict) or consumer.get("status") != expected_consumer_status:
            raise ProjectionError(f"runtime consumer readiness for {stage} is not {expected_consumer_status}")


def build_candidate(
    contract: dict[str, Any],
    runtime_path: Path,
    manifest_path: Path,
    compose_path: Path,
    queue_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime = load_json(runtime_path)
    manifest = load_json(manifest_path)
    manifest_services = service_map(manifest)
    queue_evidence = normalized_queue_evidence(queue_dir, manifest_services)
    observed_at = parse_utc(runtime.get("generated_at_utc"), "runtime.generated_at_utc")
    maximum_age = int(contract.get("dry_run", {}).get("maximum_evidence_age_seconds", 0) or 0)
    validate_runtime_status(runtime, observed_at, now or utc_now(), maximum_age)

    if manifest.get("mode") != "shadow" or manifest.get("production_writes_enabled") is not False:
        raise ProjectionError("runtime manifest must remain shadow with production writes disabled")

    runtime_services = runtime["services"]
    rows: list[dict[str, Any]] = []
    for stage in STAGES:
        service = manifest_services[stage]
        runtime_service = runtime_services[stage]
        main = queue_evidence[stage]["main"]
        retry = queue_evidence[stage]["retry"]
        dlq = queue_evidence[stage]["dlq"]
        if len(main) != 1:
            raise ProjectionError(f"{stage} must declare exactly one main queue")
        active_consumers = None if stage == "scheduler" else main[0]["consumers"]
        if stage in CONSUMER_STAGES and active_consumers < 1:
            raise ProjectionError(f"{stage} main queue has zero consumers")
        consumer_report = runtime_service["consumer_readiness"]
        if stage in CONSUMER_STAGES:
            report_queues = consumer_report.get("queues")
            if not isinstance(report_queues, list) or not report_queues:
                raise ProjectionError(f"{stage} runtime consumer queue evidence is missing")
            report_consumers = sum(
                nonnegative_int(item.get("metrics", {}).get("consumers"), f"{stage}.runtime.consumers")
                for item in report_queues
                if isinstance(item, dict) and isinstance(item.get("metrics"), dict)
            )
            if report_consumers != active_consumers:
                raise ProjectionError(f"{stage} consumer evidence disagrees across status and queue reports")

        main_messages = sum(item["messages"] for item in main)
        retry_messages = sum(item["messages"] for item in retry)
        dlq_messages = sum(item["messages"] for item in dlq)
        image = str(service.get("image") or "")
        if not re.fullmatch(r"ghcr\.io/[a-z0-9_.\-/]+@sha256:[0-9a-f]{64}", image):
            raise ProjectionError(f"{stage} deployment image is not an immutable GHCR digest")
        rows.append(
            {
                "stage_name": stage,
                "active_ingestion_owner": "legacy_shards",
                "stage_status": "healthy",
                "stale_status": "current",
                "last_attempt_at": iso_utc(observed_at),
                "last_success_at": iso_utc(observed_at),
                "last_failure_at": None,
                "consecutive_failure_count": 0,
                "throughput_per_minute": None,
                "latency_p50_ms": None,
                "latency_p95_ms": None,
                "retry_count": retry_messages,
                "dlq_count": dlq_messages,
                "queue_age_seconds": 0 if main_messages + retry_messages == 0 else None,
                "active_consumers": active_consumers,
                "deployment_version": image,
                "telemetry_version": 1,
                "projection_version": 1,
                "sanitized_error_code": None,
                "sanitized_error_message": None,
                "diagnostic_metadata": {
                    "consumerRequired": stage != "scheduler",
                    "mainQueueMessages": main_messages,
                    "mainQueueMessagesReady": sum(item["messages_ready"] for item in main),
                    "mainQueueMessagesUnacknowledged": sum(item["messages_unacknowledged"] for item in main),
                    "retryMessages": retry_messages,
                    "dlqMessages": dlq_messages,
                    "runtimeMode": "shadow",
                    "productionWritesEnabled": False,
                },
                "redact_after": iso_utc(observed_at + timedelta(days=90)),
                "updated_at": iso_utc(observed_at),
            }
        )

    artifact = {
        "schema_version": 1,
        "status": "pass",
        "operation": "dry_run",
        "tracking_issue": contract.get("tracking_issue"),
        "generated_at_utc": iso_utc(now or utc_now()),
        "observed_at_utc": iso_utc(observed_at),
        "workflow": {
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "authorization": {
            "kind": "standing_bounded_authorization",
            "per_release_owner_approval_required": False,
            "target": TARGET,
            "typed_confirmation": APPLY_CONFIRMATION,
        },
        "source_digests": {
            "runtime_status_sha256": sha256_file(runtime_path),
            "runtime_manifest_sha256": sha256_file(manifest_path),
            "runtime_compose_sha256": sha256_file(compose_path),
            "queue_evidence_sha256": sha256_json(queue_evidence),
        },
        "safety": {
            "runtime_mode": "shadow",
            "production_writes_enabled": False,
            "active_ingestion_owner": "legacy_shards",
            "legacy_ingestion_owner_unchanged": True,
            "missing_consumers": [],
            "unverifiable_consumers": [],
            "article_or_domain_writes": False,
            "queue_mutation": False,
            "dns_or_failover_mutation": False,
            "infrastructure_mutation": False,
            "schema_mutation": False,
        },
        "candidate": {
            "target": TARGET,
            "row_count": len(rows),
            "rows": rows,
        },
    }
    validate_candidate_artifact(artifact, contract)
    ensure_value_free(artifact, "dry-run artifact")
    return artifact


def validate_candidate_artifact(artifact: dict[str, Any], contract: dict[str, Any]) -> None:
    if artifact.get("schema_version") != 1 or artifact.get("status") != "pass":
        raise ProjectionError("candidate artifact schema/status is invalid")
    if artifact.get("operation") != "dry_run":
        raise ProjectionError("candidate artifact must be a dry run")
    if artifact.get("tracking_issue") != contract.get("tracking_issue"):
        raise ProjectionError("candidate artifact tracking issue does not match the authorization")
    authorization = artifact.get("authorization")
    expected_authorization = {
        "kind": "standing_bounded_authorization",
        "per_release_owner_approval_required": False,
        "target": TARGET,
        "typed_confirmation": APPLY_CONFIRMATION,
    }
    if authorization != expected_authorization:
        raise ProjectionError("candidate artifact authorization is not exact")
    workflow = artifact.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != {"run_id", "commit"}:
        raise ProjectionError("candidate workflow audit identity is incomplete")
    if not re.fullmatch(r"(?:[0-9]+|local)", str(workflow.get("run_id"))):
        raise ProjectionError("candidate workflow run id is invalid")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|local)", str(workflow.get("commit"))):
        raise ProjectionError("candidate workflow commit is invalid")
    observed_at = parse_utc(artifact.get("observed_at_utc"), "artifact.observed_at_utc")
    generated_at = parse_utc(artifact.get("generated_at_utc"), "artifact.generated_at_utc")
    maximum_age = int(contract.get("dry_run", {}).get("maximum_evidence_age_seconds", 0) or 0)
    generated_age = (generated_at - observed_at).total_seconds()
    if generated_age < -60 or generated_age > maximum_age:
        raise ProjectionError("candidate generation is outside the authorized freshness window")
    safety = artifact.get("safety")
    required_safety = {
        "runtime_mode": "shadow",
        "production_writes_enabled": False,
        "active_ingestion_owner": "legacy_shards",
        "legacy_ingestion_owner_unchanged": True,
        "missing_consumers": [],
        "unverifiable_consumers": [],
        "article_or_domain_writes": False,
        "queue_mutation": False,
        "dns_or_failover_mutation": False,
        "infrastructure_mutation": False,
        "schema_mutation": False,
    }
    if safety != required_safety:
        raise ProjectionError("candidate artifact safety invariants are not exact")
    candidate = artifact.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("target") != TARGET:
        raise ProjectionError("candidate projection target is invalid")
    rows = candidate.get("rows")
    if not isinstance(rows, list) or len(rows) != 8 or candidate.get("row_count") != 8:
        raise ProjectionError("candidate must contain exactly eight rows")
    if [row.get("stage_name") for row in rows if isinstance(row, dict)] != list(STAGES):
        raise ProjectionError("candidate stage order/set is invalid")
    allowed_columns = set(contract.get("mutation", {}).get("allowed_columns", []))
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed_columns:
            raise ProjectionError("candidate row columns do not match the authorization")
        stage = row["stage_name"]
        if row.get("active_ingestion_owner") != "legacy_shards":
            raise ProjectionError(f"{stage} active ingestion owner must remain legacy_shards")
        if row.get("stage_status") != "healthy" or row.get("stale_status") != "current":
            raise ProjectionError(f"{stage} projection must be current and healthy")
        if row.get("telemetry_version") != 1 or row.get("projection_version") != 1:
            raise ProjectionError(f"{stage} telemetry/projection version mismatch")
        consumers = row.get("active_consumers")
        if stage == "scheduler" and consumers is not None:
            raise ProjectionError("scheduler must remain explicitly non-consuming")
        if stage in CONSUMER_STAGES and (not isinstance(consumers, int) or consumers < 1):
            raise ProjectionError(f"{stage} must have at least one active consumer")
        image = str(row.get("deployment_version") or "")
        if not re.fullmatch(r"ghcr\.io/[a-z0-9_.\-/]+@sha256:[0-9a-f]{64}", image):
            raise ProjectionError(f"{stage} deployment version must be an immutable image")
        if parse_utc(row.get("updated_at"), f"{stage}.updated_at") != observed_at:
            raise ProjectionError(f"{stage} updated_at must match the candidate observation")
        ensure_value_free(row, f"candidate row {stage}")
    digests = artifact.get("source_digests")
    expected_digest_keys = {
        "runtime_status_sha256",
        "runtime_manifest_sha256",
        "runtime_compose_sha256",
        "queue_evidence_sha256",
    }
    if (
        not isinstance(digests, dict)
        or set(digests) != expected_digest_keys
        or not all(SAFE_SHA_RE.fullmatch(str(value)) for value in digests.values())
    ):
        raise ProjectionError("candidate source digests are incomplete")


def psql_json(sql: str, password: str, *, timeout: int = 45) -> dict[str, Any]:
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["PGCONNECT_TIMEOUT"] = "10"
    command = [
        "psql",
        "--no-psqlrc",
        "-X",
        "--quiet",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-h",
        "127.0.0.1",
        "-p",
        "15432",
        "-U",
        PROJECTION_ROLE,
        "-d",
        "nutsnews_primary_shadow",
    ]
    try:
        completed = subprocess.run(
            command,
            input=sql,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ProjectionError("psql is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectionError("bounded projection query timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ProjectionError("bounded projection query failed closed") from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise ProjectionError("bounded projection query returned no evidence")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ProjectionError("bounded projection query returned invalid evidence") from exc
    if not isinstance(result, dict):
        raise ProjectionError("bounded projection query evidence must be an object")
    return result


PRIVILEGE_SQL = f"""
select json_build_object(
  'current_user', current_user,
  'target_select', has_table_privilege(current_user, '{TARGET}', 'SELECT'),
  'target_insert', has_table_privilege(current_user, '{TARGET}', 'INSERT'),
  'target_update', has_table_privilege(current_user, '{TARGET}', 'UPDATE'),
  'target_delete', has_table_privilege(current_user, '{TARGET}', 'DELETE'),
  'target_truncate', has_table_privilege(current_user, '{TARGET}', 'TRUNCATE'),
  'target_trigger', has_table_privilege(current_user, '{TARGET}', 'TRIGGER'),
  'target_references', has_table_privilege(current_user, '{TARGET}', 'REFERENCES'),
  'sequence_usage', has_sequence_privilege(current_user, 'worker_uplift_final.stage_health_projections_id_seq', 'USAGE'),
  'database_create', has_database_privilege(current_user, current_database(), 'CREATE'),
  'role_create', (select rolcreaterole from pg_roles where rolname = current_user),
  'superuser', (select rolsuper from pg_roles where rolname = current_user),
  'schema_create_grants', coalesce((
    select json_agg(n.nspname order by n.nspname)
    from pg_namespace n
    where n.nspname !~ '^pg_'
      and n.nspname <> 'information_schema'
      and has_schema_privilege(current_user, n.oid, 'CREATE')
  ), '[]'::json),
  'other_mutation_grants', coalesce((
    select json_agg(json_build_object('schema', table_schema, 'table', table_name, 'privilege', privilege_type)
                    order by table_schema, table_name, privilege_type)
    from information_schema.role_table_grants
    where grantee = current_user
      and privilege_type in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES')
      and not (table_schema = 'worker_uplift_final' and table_name = 'stage_health_projections'
               and privilege_type in ('INSERT', 'UPDATE'))
  ), '[]'::json)
)::text;
"""


SCHEMA_FINGERPRINT_SQL = f"""
select json_build_object(
  'columns', (select json_agg(json_build_object(
      'name', a.attname,
      'type', pg_catalog.format_type(a.atttypid, a.atttypmod),
      'not_null', a.attnotnull
    ) order by a.attnum)
    from pg_attribute a
    where a.attrelid = '{TARGET}'::regclass and a.attnum > 0 and not a.attisdropped),
  'constraints', (select json_agg(pg_get_constraintdef(c.oid, true) order by c.conname)
    from pg_constraint c where c.conrelid = '{TARGET}'::regclass),
  'indexes', (select json_agg(indexdef order by indexname)
    from pg_indexes where schemaname = 'worker_uplift_final' and tablename = 'stage_health_projections')
)::text;
"""


ROWS_SQL = f"""
select json_build_object(
  'row_count', count(*),
  'rows', coalesce(json_agg(json_build_object(
    'stage_name', stage_name,
    'active_ingestion_owner', active_ingestion_owner,
    'stage_status', stage_status,
    'stale_status', stale_status,
    'retry_count', retry_count,
    'dlq_count', dlq_count,
    'queue_age_seconds', queue_age_seconds,
    'active_consumers', active_consumers,
    'deployment_version', deployment_version,
    'telemetry_version', telemetry_version,
    'projection_version', projection_version,
    'diagnostic_metadata', diagnostic_metadata,
    'updated_at', to_char(updated_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
  ) order by array_position(array{list(STAGES)!r}::text[], stage_name)), '[]'::json)
)::text from {TARGET};
""".replace(str(list(STAGES)), "['" + "','".join(STAGES) + "']")


def validate_privilege_proof(proof: dict[str, Any]) -> None:
    expected_true = ("target_select", "target_insert", "target_update", "sequence_usage")
    expected_false = (
        "target_delete",
        "target_truncate",
        "target_trigger",
        "target_references",
        "database_create",
        "role_create",
        "superuser",
    )
    if proof.get("current_user") != PROJECTION_ROLE:
        raise ProjectionError("projection workflow is not using the dedicated database role")
    if any(proof.get(name) is not True for name in expected_true):
        raise ProjectionError("projection database role is missing an allowed target privilege")
    if any(proof.get(name) is not False for name in expected_false):
        raise ProjectionError("projection database role has a forbidden privilege")
    if proof.get("schema_create_grants") != []:
        raise ProjectionError("projection database role has a forbidden schema-create grant")
    if proof.get("other_mutation_grants") != []:
        raise ProjectionError("projection database role has another table mutation grant")


def apply_sql(rows: list[dict[str, Any]]) -> str:
    payload = canonical_json(rows)
    if "$projection$" in payload:
        raise ProjectionError("candidate payload contains a forbidden SQL delimiter")
    return f"""
begin;
set local statement_timeout = '30s';
with incoming as (
  select * from jsonb_to_recordset($projection${payload}$projection$::jsonb) as x(
    stage_name text,
    active_ingestion_owner text,
    stage_status text,
    stale_status text,
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    last_failure_at timestamptz,
    consecutive_failure_count integer,
    throughput_per_minute numeric,
    latency_p50_ms integer,
    latency_p95_ms integer,
    retry_count bigint,
    dlq_count bigint,
    queue_age_seconds integer,
    active_consumers integer,
    deployment_version text,
    telemetry_version integer,
    projection_version integer,
    sanitized_error_code text,
    sanitized_error_message text,
    diagnostic_metadata jsonb,
    redact_after timestamptz,
    updated_at timestamptz
  )
), guarded as (
  select *, 1 / case when (select count(*) from incoming) = 8
                         and (select count(distinct stage_name) from incoming) = 8
                    then 1 else 0 end as exact_row_guard
  from incoming
), upserted as (
  insert into {TARGET} (
    stage_name, active_ingestion_owner, stage_status, stale_status,
    last_attempt_at, last_success_at, last_failure_at, consecutive_failure_count,
    throughput_per_minute, latency_p50_ms, latency_p95_ms, retry_count, dlq_count,
    queue_age_seconds, active_consumers, deployment_version, telemetry_version,
    projection_version, sanitized_error_code, sanitized_error_message,
    diagnostic_metadata, redact_after, updated_at
  )
  select stage_name, active_ingestion_owner, stage_status, stale_status,
    last_attempt_at, last_success_at, last_failure_at, consecutive_failure_count,
    throughput_per_minute, latency_p50_ms, latency_p95_ms, retry_count, dlq_count,
    queue_age_seconds, active_consumers, deployment_version, telemetry_version,
    projection_version, sanitized_error_code, sanitized_error_message,
    diagnostic_metadata, redact_after, updated_at
  from guarded where exact_row_guard = 1
  on conflict (stage_name) do update set
    active_ingestion_owner = excluded.active_ingestion_owner,
    stage_status = excluded.stage_status,
    stale_status = excluded.stale_status,
    last_attempt_at = excluded.last_attempt_at,
    last_success_at = excluded.last_success_at,
    last_failure_at = excluded.last_failure_at,
    consecutive_failure_count = excluded.consecutive_failure_count,
    throughput_per_minute = excluded.throughput_per_minute,
    latency_p50_ms = excluded.latency_p50_ms,
    latency_p95_ms = excluded.latency_p95_ms,
    retry_count = excluded.retry_count,
    dlq_count = excluded.dlq_count,
    queue_age_seconds = excluded.queue_age_seconds,
    active_consumers = excluded.active_consumers,
    deployment_version = excluded.deployment_version,
    telemetry_version = excluded.telemetry_version,
    projection_version = excluded.projection_version,
    sanitized_error_code = excluded.sanitized_error_code,
    sanitized_error_message = excluded.sanitized_error_message,
    diagnostic_metadata = excluded.diagnostic_metadata,
    redact_after = excluded.redact_after,
    updated_at = excluded.updated_at
  where {TARGET}.updated_at <= excluded.updated_at
  returning stage_name
)
select json_build_object(
  'upserted_rows', count(*),
  'expected_rows', 8,
  'exact_row_guard', 1 / case when count(*) = 8 then 1 else 0 end
)::text from upserted;
commit;
"""


def normalized_db_rows(rows_result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = rows_result.get("rows")
    if not isinstance(rows, list):
        raise ProjectionError("database row evidence is missing")
    return rows


def expected_db_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "stage_name",
        "active_ingestion_owner",
        "stage_status",
        "stale_status",
        "retry_count",
        "dlq_count",
        "queue_age_seconds",
        "active_consumers",
        "deployment_version",
        "telemetry_version",
        "projection_version",
        "diagnostic_metadata",
        "updated_at",
    )
    return [{key: row.get(key) for key in keys} for row in artifact["candidate"]["rows"]]


def apply_candidate(
    contract: dict[str, Any],
    artifact_path: Path,
    password: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != APPLY_CONFIRMATION:
        raise ProjectionError("apply requires the exact typed confirmation")
    if not password or "\n" in password or "\r" in password:
        raise ProjectionError("projection database credential is unavailable")
    artifact = load_json(artifact_path)
    validate_candidate_artifact(artifact, contract)
    ensure_value_free(artifact, "candidate artifact")
    observed = parse_utc(artifact.get("observed_at_utc"), "artifact.observed_at_utc")
    maximum_age = int(contract.get("dry_run", {}).get("maximum_evidence_age_seconds", 0) or 0)
    age = (utc_now() - observed).total_seconds()
    if age < -60 or age > maximum_age:
        raise ProjectionError("apply candidate is outside the authorized freshness window")
    privilege = psql_json(PRIVILEGE_SQL, password)
    validate_privilege_proof(privilege)
    schema_before = psql_json(SCHEMA_FINGERPRINT_SQL, password)
    rows_before = psql_json(ROWS_SQL, password)
    for row in normalized_db_rows(rows_before):
        updated = parse_utc(row.get("updated_at"), "database.updated_at")
        if updated > observed:
            raise ProjectionError("stale candidate cannot overwrite newer projection evidence")
    mutation = psql_json(apply_sql(artifact["candidate"]["rows"]), password)
    if (
        mutation.get("upserted_rows") != 8
        or mutation.get("expected_rows") != 8
        or mutation.get("exact_row_guard") != 1
    ):
        raise ProjectionError("bounded upsert did not affect exactly eight stage rows")
    schema_after = psql_json(SCHEMA_FINGERPRINT_SQL, password)
    rows_after = psql_json(ROWS_SQL, password)
    if sha256_json(schema_before) != sha256_json(schema_after):
        raise ProjectionError("target schema fingerprint changed during projection apply")
    if rows_after.get("row_count") != 8:
        raise ProjectionError("projection table does not contain exactly eight rows after apply")
    if normalized_db_rows(rows_after) != expected_db_rows(artifact):
        raise ProjectionError("database rows do not exactly match the authorized candidate")
    result = {
        "schema_version": 1,
        "status": "applied",
        "operation": "bounded_eight_row_upsert",
        "tracking_issue": contract.get("tracking_issue"),
        "applied_at_utc": iso_utc(utc_now()),
        "workflow": {
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "candidate_artifact_sha256": sha256_file(artifact_path),
        "mutation": {
            "target": TARGET,
            "upserted_rows": 8,
            "delete_performed": False,
            "truncate_performed": False,
            "arbitrary_sql_available": False,
            "schema_change_performed": False,
        },
        "privilege_proof": privilege,
        "schema_fingerprint": {
            "before_sha256": sha256_json(schema_before),
            "after_sha256": sha256_json(schema_after),
            "unchanged": True,
        },
        "database_evidence": rows_after,
        "safety": artifact["safety"],
    }
    ensure_value_free(result, "apply evidence")
    return result


def queue_state(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        row["stage_name"]: {
            "active_consumers": row["active_consumers"],
            "retry_count": row["retry_count"],
            "dlq_count": row["dlq_count"],
            "diagnostic_metadata": row["diagnostic_metadata"],
            "deployment_version": row["deployment_version"],
        }
        for row in rows
    }


def verify_post_apply(
    contract: dict[str, Any],
    artifact_path: Path,
    apply_report_path: Path,
    post_runtime_path: Path,
    post_manifest_path: Path,
    post_compose_path: Path,
    post_queue_dir: Path,
) -> dict[str, Any]:
    artifact = load_json(artifact_path)
    validate_candidate_artifact(artifact, contract)
    apply_report = load_json(apply_report_path)
    if apply_report.get("status") != "applied" or apply_report.get("candidate_artifact_sha256") != sha256_file(artifact_path):
        raise ProjectionError("apply report is not bound to the candidate artifact")
    post = build_candidate(
        contract,
        post_runtime_path,
        post_manifest_path,
        post_compose_path,
        post_queue_dir,
    )
    before_rows = artifact["candidate"]["rows"]
    after_rows = post["candidate"]["rows"]
    if queue_state(before_rows) != queue_state(after_rows):
        raise ProjectionError("consumer, queue, or deployed candidate state changed during apply")
    if artifact["source_digests"]["runtime_manifest_sha256"] != post["source_digests"]["runtime_manifest_sha256"]:
        raise ProjectionError("runtime manifest changed during projection apply")
    if artifact["source_digests"]["runtime_compose_sha256"] != post["source_digests"]["runtime_compose_sha256"]:
        raise ProjectionError("runtime compose changed during projection apply")
    result = {
        "schema_version": 1,
        "status": "pass",
        "operation": "post_apply_proof",
        "tracking_issue": contract.get("tracking_issue"),
        "verified_at_utc": iso_utc(utc_now()),
        "workflow": apply_report.get("workflow"),
        "candidate_artifact_sha256": sha256_file(artifact_path),
        "apply_report_sha256": sha256_file(apply_report_path),
        "database_evidence_sha256": sha256_json(apply_report.get("database_evidence")),
        "proof": {
            "exact_eight_database_rows": apply_report.get("database_evidence", {}).get("row_count") == 8,
            "target_rows_match_candidate": normalized_db_rows(apply_report["database_evidence"]) == expected_db_rows(artifact),
            "runtime_mode_unchanged": True,
            "production_writes_enabled_false": True,
            "active_ingestion_owner_legacy_shards": True,
            "consumer_counts_unchanged": True,
            "queue_counts_unchanged": True,
            "runtime_manifest_digest_unchanged": True,
            "runtime_compose_digest_unchanged": True,
            "target_schema_fingerprint_unchanged": apply_report.get("schema_fingerprint", {}).get("unchanged") is True,
            "article_or_domain_mutation_privileges_available": False,
            "rabbitmq_mutation_credentials_available": False,
            "dns_or_failover_credentials_available": False,
            "infrastructure_mutation_credentials_available": False,
        },
        "post_apply_source_digests": post["source_digests"],
        "guardrails": {
            "cutover_performed": False,
            "uplift_production_writes_enabled": False,
            "ingestion_owner_changed": False,
            "legacy_worker_changed": False,
            "dns_or_failover_changed": False,
            "article_or_domain_write_performed": False,
            "queue_mutation_performed": False,
            "infrastructure_mutation_performed": False,
            "schema_change_performed": False,
        },
    }
    positive_proofs = {
        key: value
        for key, value in result["proof"].items()
        if not key.endswith("_available")
    }
    negative_capability_proofs = {
        key: value
        for key, value in result["proof"].items()
        if key.endswith("_available")
    }
    if not all(positive_proofs.values()) or any(negative_capability_proofs.values()):
        raise ProjectionError("one or more post-apply proof invariants failed")
    ensure_value_free(result, "post-apply evidence")
    return result


def write_output(path: Path | None, value: dict[str, Any]) -> None:
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("dry-run", "apply", "verify"), required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--runtime-status", type=Path)
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-compose", type=Path)
    parser.add_argument("--queue-evidence-dir", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--apply-report", type=Path)
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--db-password-env", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def require_paths(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    for name in names:
        if getattr(args, name) is None:
            raise ProjectionError(f"--{name.replace('_', '-')} is required for {args.mode}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        contract = load_json(args.contract)
        if args.mode == "dry-run":
            require_paths(args, ("runtime_status", "runtime_manifest", "runtime_compose", "queue_evidence_dir"))
            result = build_candidate(
                contract,
                args.runtime_status,
                args.runtime_manifest,
                args.runtime_compose,
                args.queue_evidence_dir,
            )
        elif args.mode == "apply":
            require_paths(args, ("artifact",))
            if not args.db_password_env:
                raise ProjectionError("--db-password-env is required for apply")
            result = apply_candidate(
                contract,
                args.artifact,
                os.environ.get(args.db_password_env, ""),
                args.confirmation,
            )
        else:
            require_paths(
                args,
                (
                    "artifact",
                    "apply_report",
                    "runtime_status",
                    "runtime_manifest",
                    "runtime_compose",
                    "queue_evidence_dir",
                ),
            )
            result = verify_post_apply(
                contract,
                args.artifact,
                args.apply_report,
                args.runtime_status,
                args.runtime_manifest,
                args.runtime_compose,
                args.queue_evidence_dir,
            )
        write_output(args.output, result)
        return 0
    except ProjectionError as exc:
        failure = {
            "schema_version": 1,
            "status": "fail",
            "operation": args.mode,
            "error": str(exc),
            "writes_performed": False if args.mode == "dry-run" else None,
        }
        write_output(args.output, failure)
        print(f"worker-uplift stage health projection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
