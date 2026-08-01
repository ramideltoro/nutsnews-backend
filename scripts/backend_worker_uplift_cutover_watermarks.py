#!/usr/bin/env python3
"""Build, apply, and verify the bounded eight-stage cutover watermarks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs/worker-uplift-cutover-watermark-authorization.json"
RUNTIME_CONTRACT = ROOT / "docs/worker-uplift-stage-health-projection-authorization.json"
RUNTIME_IMPLEMENTATION = ROOT / "scripts/backend_worker_uplift_stage_health_projection.py"
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
SCHEMAS = {stage: f"worker_uplift_{stage}" for stage in STAGES}
WATERMARK_NAME = "cutover-boundary-v1"
WATERMARK_ROLE = "nutsnews_worker_uplift_watermark"
APPLY_CONFIRMATION = "refresh-worker-uplift-cutover-watermarks"
SAFE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
SECRET_RE = re.compile(
    r"(password|passwd|token|secret|cookie|private[_-]?key|"
    r"postgres(?:ql)?://|amqps?://|bearer\s|github_pat_|gh[pousr]_)",
    re.IGNORECASE,
)


class WatermarkError(RuntimeError):
    pass


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("worker_uplift_runtime_safety", RUNTIME_IMPLEMENTATION)
    if spec is None or spec.loader is None:
        raise WatermarkError("runtime safety implementation cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = load_runtime_module()


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise WatermarkError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WatermarkError(f"required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WatermarkError(f"JSON file is invalid: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatermarkError(f"JSON root must be an object: {path}")
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
    if SECRET_RE.search(canonical_json(value)):
        raise WatermarkError(f"{context} contains a forbidden credential or private-value marker")


def runtime_state(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = artifact.get("candidate", {}).get("rows", [])
    state: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise WatermarkError("runtime safety candidate contains an invalid row")
        stage = str(row.get("stage_name") or "")
        diagnostic = row.get("diagnostic_metadata")
        if not isinstance(diagnostic, dict):
            raise WatermarkError(f"runtime queue evidence is missing for {stage}")
        state[stage] = {
            "active_consumers": row.get("active_consumers"),
            "deployment_version": row.get("deployment_version"),
            "main_messages": diagnostic.get("mainQueueMessages"),
            "main_ready": diagnostic.get("mainQueueMessagesReady"),
            "main_unacknowledged": diagnostic.get("mainQueueMessagesUnacknowledged"),
            "retry_messages": diagnostic.get("retryMessages"),
            "dlq_messages": diagnostic.get("dlqMessages"),
        }
    if tuple(state) != STAGES:
        raise WatermarkError("runtime safety candidate must contain the exact stage order")
    return state


def validate_runtime_pair(
    first: dict[str, Any],
    followup: dict[str, Any],
    contract: dict[str, Any],
    *,
    now: datetime,
) -> tuple[datetime, dict[str, dict[str, Any]]]:
    runtime_contract = load_json(RUNTIME_CONTRACT)
    try:
        RUNTIME.validate_candidate_artifact(first, runtime_contract)
        RUNTIME.validate_candidate_artifact(followup, runtime_contract)
        RUNTIME.ensure_value_free(first, "runtime safety candidate")
        RUNTIME.ensure_value_free(followup, "runtime follow-up candidate")
    except Exception as exc:
        raise WatermarkError(f"runtime safety evidence failed closed: {exc}") from exc
    first_at = parse_utc(first.get("observed_at_utc"), "runtime_first.observed_at_utc")
    followup_at = parse_utc(followup.get("observed_at_utc"), "runtime_followup.observed_at_utc")
    interval = (followup_at - first_at).total_seconds()
    evidence = contract.get("evidence", {})
    if interval < int(evidence.get("queue_stability_sample_minimum_seconds", 0)):
        raise WatermarkError("runtime queue stability sample is too short")
    if interval > int(evidence.get("queue_stability_sample_maximum_seconds", 0)):
        raise WatermarkError("runtime queue stability sample is too long")
    age = (now - followup_at).total_seconds()
    if age < -60 or age > int(evidence.get("maximum_age_seconds", 0)):
        raise WatermarkError("runtime follow-up evidence is outside the authorized freshness window")
    first_state = runtime_state(first)
    followup_state = runtime_state(followup)
    if first_state != followup_state:
        raise WatermarkError("runtime, consumer, queue, DLQ, or deployment state changed during the stability sample")
    for stage, state in followup_state.items():
        expected_consumers = None if stage == "scheduler" else 1
        if state["active_consumers"] != expected_consumers:
            raise WatermarkError(f"{stage} does not have the exact authorized consumer count")
        for field in ("main_messages", "main_ready", "main_unacknowledged", "retry_messages"):
            if state[field] != 0:
                raise WatermarkError(f"{stage} {field} is not drained")
    if first.get("source_digests", {}).get("runtime_manifest_sha256") != followup.get("source_digests", {}).get("runtime_manifest_sha256"):
        raise WatermarkError("runtime manifest changed during the stability sample")
    if first.get("source_digests", {}).get("runtime_compose_sha256") != followup.get("source_digests", {}).get("runtime_compose_sha256"):
        raise WatermarkError("runtime compose changed during the stability sample")
    return followup_at, followup_state


def classify_psql_failure(stderr: str) -> str:
    """Return a value-free failure class without echoing database diagnostics."""
    lowered = stderr.lower()
    classifications = (
        ("password authentication failed", "authentication_failed"),
        ("no pg_hba.conf entry", "connection_policy_rejected"),
        ("permission denied", "insufficient_privilege"),
        ("does not exist", "missing_database_object"),
        ("syntax error", "invalid_query_shape"),
        ("connection refused", "connection_unavailable"),
        ("server closed the connection", "connection_unavailable"),
    )
    for marker, classification in classifications:
        if marker in lowered:
            return classification
    return "query_rejected"


def psql_json(sql: str, password: str, *, context: str, timeout: int = 45) -> dict[str, Any]:
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
        WATERMARK_ROLE,
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
        raise WatermarkError("psql is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise WatermarkError("bounded watermark query timed out") from exc
    except subprocess.CalledProcessError as exc:
        failure_class = classify_psql_failure(exc.stderr or "")
        raise WatermarkError(f"bounded {context} query failed closed ({failure_class})") from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise WatermarkError("bounded watermark query returned no evidence")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise WatermarkError("bounded watermark query returned invalid evidence") from exc
    if not isinstance(result, dict):
        raise WatermarkError("bounded watermark query evidence must be an object")
    return result


def stage_snapshot_sql(stage: str, schema: str) -> str:
    target = f"{schema}.reconciliation_watermarks"
    return f"""
    select json_build_object(
      'stage', '{stage}',
      'schema', '{schema}',
      'watermark_row_count', (select count(*) from {target}),
      'target_watermark_count', (select count(*) from {target} where watermark_name = '{WATERMARK_NAME}'),
      'current_target', (select json_build_object(
          'watermark_name', watermark_name,
          'cursor_value', cursor_value,
          'confirmed_message_id', confirmed_message_id,
          'confirmed_at', to_char(confirmed_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
          'lag_count', lag_count,
          'diagnostic_metadata', diagnostic_metadata,
          'redact_after', to_char(redact_after at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        ) from {target} where watermark_name = '{WATERMARK_NAME}'),
      'max_confirmed_outbox_id', (select id from {schema}.outbox where status = 'confirmed' and confirmed_at is not null order by id desc limit 1),
      'max_confirmed_message_id', (select outbox_message_id from {schema}.outbox where status = 'confirmed' and confirmed_at is not null order by id desc limit 1),
      'max_confirmed_at', (select to_char(confirmed_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') from {schema}.outbox where status = 'confirmed' and confirmed_at is not null order by id desc limit 1),
      'confirmed_outbox_count', (select count(*) from {schema}.outbox where status = 'confirmed' and confirmed_at is not null),
      'unconfirmed_outbox_count', (select count(*) from {schema}.outbox where status in ('pending', 'published', 'retrying') and confirmed_at is null),
      'retrying_outbox_count', (select count(*) from {schema}.outbox where status = 'retrying'),
      'dead_lettered_outbox_count', (select count(*) from {schema}.outbox where status = 'dead_lettered'),
      'active_inbox_count', (select count(*) from {schema}.inbox where status in ('received', 'processing')),
      'failed_or_parked_inbox_count', (select count(*) from {schema}.inbox where status in ('failed', 'parked')),
      'failed_or_parked_reason_bucket_count', (select count(distinct coalesce(nullif(sanitized_error_code, ''), 'unspecified')) from {schema}.inbox where status in ('failed', 'parked')),
      'schema_fingerprint', (select md5(coalesce(string_agg(value, '|' order by value), '')) from (
        select a.attname || ':' || pg_catalog.format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text as value
        from pg_attribute a where a.attrelid = '{target}'::regclass and a.attnum > 0 and not a.attisdropped
        union all
        select pg_get_constraintdef(c.oid, true) from pg_constraint c where c.conrelid = '{target}'::regclass
        union all
        select indexdef from pg_indexes where schemaname = '{schema}' and tablename = 'reconciliation_watermarks'
      ) fingerprint_values)
    )
    """


DB_SNAPSHOT_SQL = "select json_build_object('stages', json_build_array(" + ",".join(
    f"({stage_snapshot_sql(stage, SCHEMAS[stage])})" for stage in STAGES
) + "))::text;"


def privilege_sql() -> str:
    targets = ",".join(
        f"json_build_object('stage','{stage}',"
        f"'target_select',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','SELECT'),"
        f"'target_insert',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','INSERT'),"
        f"'target_update',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','UPDATE'),"
        f"'target_delete',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','DELETE'),"
        f"'target_truncate',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','TRUNCATE'),"
        f"'target_trigger',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','TRIGGER'),"
        f"'target_references',has_table_privilege(current_user,'{schema}.reconciliation_watermarks','REFERENCES'),"
        f"'inbox_select',has_table_privilege(current_user,'{schema}.inbox','SELECT'),"
        f"'outbox_select',has_table_privilege(current_user,'{schema}.outbox','SELECT'),"
        f"'sequence_usage',has_sequence_privilege(current_user,'{schema}.reconciliation_watermarks_id_seq','USAGE'))"
        for stage, schema in SCHEMAS.items()
    )
    allowed = " OR ".join(
        f"(table_schema = '{schema}' and table_name = 'reconciliation_watermarks' and privilege_type in ('INSERT','UPDATE'))"
        for schema in SCHEMAS.values()
    )
    return f"""
    select json_build_object(
      'current_user', current_user,
      'database_create', has_database_privilege(current_user, current_database(), 'CREATE'),
      'role_create', (select rolcreaterole from pg_roles where rolname = current_user),
      'superuser', (select rolsuper from pg_roles where rolname = current_user),
      'role_inherit', (select rolinherit from pg_roles where rolname = current_user),
      'row_level_security_bypass', (select rolbypassrls from pg_roles where rolname = current_user),
      'role_memberships', coalesce((select json_agg(granted.rolname order by granted.rolname)
        from pg_auth_members membership
        join pg_roles granted on granted.oid = membership.roleid
        join pg_roles member on member.oid = membership.member
        where member.rolname = current_user), '[]'::json),
      'schema_create_grants', coalesce((select json_agg(n.nspname order by n.nspname) from pg_namespace n
        where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' and has_schema_privilege(current_user, n.oid, 'CREATE')), '[]'::json),
      'other_mutation_grants', coalesce((select json_agg(json_build_object('schema',tables.schemaname,'table',tables.tablename,'privilege',privileges.privilege)
        order by tables.schemaname,tables.tablename,privileges.privilege)
        from pg_tables tables
        cross join unnest(array['INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER','REFERENCES']::text[]) privileges(privilege)
        where tables.schemaname !~ '^pg_' and tables.schemaname <> 'information_schema'
          and has_table_privilege(current_user, format('%I.%I', tables.schemaname, tables.tablename), privileges.privilege)
          and not ({allowed.replace('table_schema', 'tables.schemaname').replace('table_name', 'tables.tablename').replace('privilege_type', 'privileges.privilege')})), '[]'::json),
      'targets', json_build_array({targets})
    )::text;
    """


PRIVILEGE_SQL = privilege_sql()


def validate_privilege_proof(proof: dict[str, Any]) -> None:
    if proof.get("current_user") != WATERMARK_ROLE:
        raise WatermarkError("watermark workflow is not using the dedicated database role")
    for field in ("database_create", "role_create", "superuser", "role_inherit", "row_level_security_bypass"):
        if proof.get(field) is not False:
            raise WatermarkError(f"watermark role has forbidden capability: {field}")
    if proof.get("role_memberships") != [] or proof.get("schema_create_grants") != [] or proof.get("other_mutation_grants") != []:
        raise WatermarkError("watermark role has a grant outside the exact authorized tables")
    targets = proof.get("targets")
    if not isinstance(targets, list) or [item.get("stage") for item in targets if isinstance(item, dict)] != list(STAGES):
        raise WatermarkError("watermark role proof does not cover exactly eight stages")
    for item in targets:
        if not isinstance(item, dict):
            raise WatermarkError("watermark role proof contains an invalid target")
        for field in ("target_select", "target_insert", "target_update", "inbox_select", "outbox_select", "sequence_usage"):
            if item.get(field) is not True:
                raise WatermarkError(f"{item.get('stage')} is missing allowed privilege {field}")
        for field in ("target_delete", "target_truncate", "target_trigger", "target_references"):
            if item.get(field) is not False:
                raise WatermarkError(f"{item.get('stage')} has forbidden privilege {field}")


def snapshot_stages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    stages = snapshot.get("stages")
    if not isinstance(stages, list) or [item.get("stage") for item in stages if isinstance(item, dict)] != list(STAGES):
        raise WatermarkError("database snapshot must cover exactly eight stages in order")
    return stages


def validate_stage_boundary(item: dict[str, Any]) -> None:
    stage = str(item.get("stage") or "")
    if item.get("schema") != SCHEMAS.get(stage):
        raise WatermarkError(f"{stage} database schema is not exact")
    if item.get("watermark_row_count") not in (0, 1) or item.get("target_watermark_count") != item.get("watermark_row_count"):
        raise WatermarkError(f"{stage} has an unexpected reconciliation watermark row")
    for field in (
        "unconfirmed_outbox_count",
        "retrying_outbox_count",
        "dead_lettered_outbox_count",
        "active_inbox_count",
    ):
        if item.get(field) != 0:
            raise WatermarkError(f"{stage} {field} is not zero")
    confirmed_count = item.get("confirmed_outbox_count")
    if not isinstance(confirmed_count, int) or confirmed_count < 0:
        raise WatermarkError(f"{stage} confirmed outbox count is invalid")
    failed_count = item.get("failed_or_parked_inbox_count")
    reason_buckets = item.get("failed_or_parked_reason_bucket_count")
    if not isinstance(failed_count, int) or failed_count < 0 or not isinstance(reason_buckets, int) or reason_buckets < 0:
        raise WatermarkError(f"{stage} retained failure aggregates are invalid")
    if (failed_count == 0) != (reason_buckets == 0):
        raise WatermarkError(f"{stage} retained failure aggregate lacks a bounded reason classification")
    if not re.fullmatch(r"[0-9a-f]{32}", str(item.get("schema_fingerprint") or "")):
        raise WatermarkError(f"{stage} schema fingerprint is invalid")


def build_candidate(
    contract: dict[str, Any],
    runtime_first_path: Path,
    runtime_followup_path: Path,
    password: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not password or "\n" in password or "\r" in password:
        raise WatermarkError("watermark database credential is unavailable")
    current_time = now or utc_now()
    runtime_first = load_json(runtime_first_path)
    runtime_followup = load_json(runtime_followup_path)
    observed_at, runtime = validate_runtime_pair(runtime_first, runtime_followup, contract, now=current_time)
    privilege = psql_json(PRIVILEGE_SQL, password, context="privilege-proof")
    validate_privilege_proof(privilege)
    snapshot = psql_json(DB_SNAPSHOT_SQL, password, context="pre-state-snapshot")
    rows: list[dict[str, Any]] = []
    for item in snapshot_stages(snapshot):
        validate_stage_boundary(item)
        stage = item["stage"]
        current_target = item.get("current_target")
        if isinstance(current_target, dict):
            captured = current_target.get("diagnostic_metadata", {}).get("capturedAtUtc")
            existing_at = parse_utc(captured or current_target.get("confirmed_at"), f"{stage}.current_target")
            if existing_at > observed_at:
                raise WatermarkError(f"{stage} contains newer watermark evidence")
        confirmed_at = item.get("max_confirmed_at") or iso_utc(observed_at)
        message_id = item.get("max_confirmed_message_id")
        if message_id is not None and not SAFE_MESSAGE_ID_RE.fullmatch(str(message_id)):
            raise WatermarkError(f"{stage} confirmed message ID is invalid")
        max_id = item.get("max_confirmed_outbox_id")
        if max_id is not None and (not isinstance(max_id, int) or max_id < 1):
            raise WatermarkError(f"{stage} confirmed outbox cursor is invalid")
        queue = runtime[stage]
        evidence = {
            "activeInboxCount": item["active_inbox_count"],
            "capturedAtUtc": iso_utc(observed_at),
            "confirmedOutboxCount": item["confirmed_outbox_count"],
            "deadLetteredOutboxCount": item["dead_lettered_outbox_count"],
            "failedOrParkedInboxCount": item["failed_or_parked_inbox_count"],
            "failedOrParkedReasonBucketCount": item["failed_or_parked_reason_bucket_count"],
            "mainQueueMessages": queue["main_messages"],
            "maxConfirmedOutboxId": max_id,
            "rabbitmqDlqMessages": queue["dlq_messages"],
            "retainedFailureDisposition": "terminal-aggregate-only-no-automatic-replay",
            "retryQueueMessages": queue["retry_messages"],
            "retryingOutboxCount": item["retrying_outbox_count"],
            "runtimeImage": queue["deployment_version"],
            "runtimeMode": "shadow",
            "productionWritesEnabled": False,
            "unconfirmedOutboxCount": item["unconfirmed_outbox_count"],
        }
        evidence["evidenceSha256"] = sha256_json({"database": item, "runtime": queue})
        rows.append(
            {
                "stage": stage,
                "schema": SCHEMAS[stage],
                "watermark_name": WATERMARK_NAME,
                "cursor_value": str(max_id or 0),
                "confirmed_message_id": message_id,
                "confirmed_at": confirmed_at,
                "lag_count": 0,
                "diagnostic_metadata": evidence,
                "redact_after": iso_utc(observed_at + timedelta(days=90)),
            }
        )
    artifact = {
        "schema_version": 1,
        "status": "pass",
        "operation": "value_free_dry_run",
        "tracking_issue": contract.get("tracking_issue"),
        "generated_at_utc": iso_utc(current_time),
        "observed_at_utc": iso_utc(observed_at),
        "workflow": {
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "authorization": {
            "kind": "standing_bounded_authorization",
            "owner_comment_url": contract.get("authorization", {}).get("owner_comment_url"),
            "owner_comment_body_sha256": contract.get("authorization", {}).get("owner_comment_body_sha256"),
            "scope_sha256": contract.get("authorization", {}).get("scope_sha256"),
            "per_release_owner_approval_required": False,
            "first_run_owner_approval_required": False,
            "target_row_count": 8,
            "typed_confirmation": APPLY_CONFIRMATION,
        },
        "source_digests": {
            "runtime_first_sha256": sha256_file(runtime_first_path),
            "runtime_followup_sha256": sha256_file(runtime_followup_path),
            "runtime_manifest_sha256": runtime_followup["source_digests"]["runtime_manifest_sha256"],
            "runtime_compose_sha256": runtime_followup["source_digests"]["runtime_compose_sha256"],
            "database_snapshot_sha256": sha256_json(snapshot),
            "privilege_proof_sha256": sha256_json(privilege),
        },
        "pre_state": {
            item["stage"]: {
                "watermark_row_count": item["watermark_row_count"],
                "target_watermark_count": item["target_watermark_count"],
                "current_target_sha256": sha256_json(item["current_target"])
                if item.get("current_target") is not None
                else None,
            }
            for item in snapshot_stages(snapshot)
        },
        "candidate": {
            "row_count": 8,
            "watermark_name": WATERMARK_NAME,
            "rows": rows,
        },
        "retained_failure_aggregates": {
            row["stage"]: {
                "count": row["diagnostic_metadata"]["failedOrParkedInboxCount"],
                "reason_bucket_count": row["diagnostic_metadata"]["failedOrParkedReasonBucketCount"],
                "disposition": row["diagnostic_metadata"]["retainedFailureDisposition"],
            }
            for row in rows
        },
        "safety": {
            "runtime_mode": "shadow",
            "production_writes_enabled": False,
            "active_ingestion_owner": "legacy_shards",
            "legacy_ingestion_owner_unchanged": True,
            "main_and_retry_queues_drained": True,
            "rabbitmq_dlq_growth": 0,
            "article_or_domain_writes": False,
            "outbox_replay": False,
            "queue_mutation": False,
            "consumer_mutation": False,
            "scheduler_mutation": False,
            "schema_mutation": False,
            "infrastructure_mutation": False,
            "dns_or_failover_mutation": False,
        },
    }
    validate_candidate_artifact(artifact, contract)
    ensure_value_free(artifact, "watermark candidate artifact")
    return artifact


ROW_KEYS = {
    "stage",
    "schema",
    "watermark_name",
    "cursor_value",
    "confirmed_message_id",
    "confirmed_at",
    "lag_count",
    "diagnostic_metadata",
    "redact_after",
}


def validate_candidate_artifact(artifact: dict[str, Any], contract: dict[str, Any]) -> None:
    if artifact.get("schema_version") != 1 or artifact.get("status") != "pass" or artifact.get("operation") != "value_free_dry_run":
        raise WatermarkError("watermark candidate schema, status, or operation is invalid")
    if artifact.get("tracking_issue") != contract.get("tracking_issue"):
        raise WatermarkError("watermark candidate tracking issue is invalid")
    expected_authorization = {
        "kind": "standing_bounded_authorization",
        "owner_comment_url": contract["authorization"]["owner_comment_url"],
        "owner_comment_body_sha256": contract["authorization"]["owner_comment_body_sha256"],
        "scope_sha256": contract["authorization"]["scope_sha256"],
        "per_release_owner_approval_required": False,
        "first_run_owner_approval_required": False,
        "target_row_count": 8,
        "typed_confirmation": APPLY_CONFIRMATION,
    }
    if artifact.get("authorization") != expected_authorization:
        raise WatermarkError("watermark candidate authorization is not exact")
    workflow = artifact.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != {"run_id", "commit"}:
        raise WatermarkError("watermark candidate workflow identity is incomplete")
    if not re.fullmatch(r"(?:[0-9]+|local)", str(workflow.get("run_id"))):
        raise WatermarkError("watermark candidate workflow run ID is invalid")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|local)", str(workflow.get("commit"))):
        raise WatermarkError("watermark candidate workflow commit is invalid")
    observed = parse_utc(artifact.get("observed_at_utc"), "candidate.observed_at_utc")
    generated = parse_utc(artifact.get("generated_at_utc"), "candidate.generated_at_utc")
    generated_age = (generated - observed).total_seconds()
    if generated_age < -60 or generated_age > int(contract["evidence"]["maximum_age_seconds"]):
        raise WatermarkError("watermark candidate generation exceeds the freshness window")
    candidate = artifact.get("candidate")
    rows = candidate.get("rows") if isinstance(candidate, dict) else None
    if not isinstance(rows, list) or len(rows) != 8 or candidate.get("row_count") != 8 or candidate.get("watermark_name") != WATERMARK_NAME:
        raise WatermarkError("watermark candidate must contain exactly eight declared rows")
    if [row.get("stage") for row in rows if isinstance(row, dict)] != list(STAGES):
        raise WatermarkError("watermark candidate stage order or set is invalid")
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise WatermarkError("watermark candidate row columns are not exact")
        stage = row["stage"]
        if row.get("schema") != SCHEMAS[stage] or row.get("watermark_name") != WATERMARK_NAME:
            raise WatermarkError(f"{stage} watermark target is invalid")
        if not str(row.get("cursor_value") or "").isdigit() or row.get("lag_count") != 0:
            raise WatermarkError(f"{stage} cursor or lag is invalid")
        message_id = row.get("confirmed_message_id")
        if message_id is not None and not SAFE_MESSAGE_ID_RE.fullmatch(str(message_id)):
            raise WatermarkError(f"{stage} confirmed message ID is invalid")
        parse_utc(row.get("confirmed_at"), f"{stage}.confirmed_at")
        if parse_utc(row.get("redact_after"), f"{stage}.redact_after") <= observed:
            raise WatermarkError(f"{stage} redaction deadline is invalid")
        diagnostic = row.get("diagnostic_metadata")
        if not isinstance(diagnostic, dict) or diagnostic.get("capturedAtUtc") != iso_utc(observed):
            raise WatermarkError(f"{stage} diagnostic evidence is invalid")
        if diagnostic.get("runtimeMode") != "shadow" or diagnostic.get("productionWritesEnabled") is not False:
            raise WatermarkError(f"{stage} write policy evidence is invalid")
        if not SAFE_SHA_RE.fullmatch(str(diagnostic.get("evidenceSha256") or "")):
            raise WatermarkError(f"{stage} evidence digest is invalid")
        for field in ("activeInboxCount", "unconfirmedOutboxCount", "retryingOutboxCount", "deadLetteredOutboxCount", "mainQueueMessages", "retryQueueMessages"):
            if diagnostic.get(field) != 0:
                raise WatermarkError(f"{stage} {field} is not zero")
        ensure_value_free(row, f"watermark candidate row {stage}")
    expected_retained = {
        row["stage"]: {
            "count": row["diagnostic_metadata"]["failedOrParkedInboxCount"],
            "reason_bucket_count": row["diagnostic_metadata"]["failedOrParkedReasonBucketCount"],
            "disposition": "terminal-aggregate-only-no-automatic-replay",
        }
        for row in rows
    }
    if artifact.get("retained_failure_aggregates") != expected_retained:
        raise WatermarkError("retained failure aggregates are not exact")
    pre_state = artifact.get("pre_state")
    if not isinstance(pre_state, dict) or list(pre_state) != list(STAGES):
        raise WatermarkError("watermark candidate pre-state does not cover exactly eight stages")
    for stage, state in pre_state.items():
        if not isinstance(state, dict) or set(state) != {
            "watermark_row_count",
            "target_watermark_count",
            "current_target_sha256",
        }:
            raise WatermarkError(f"{stage} watermark candidate pre-state is invalid")
        if state["watermark_row_count"] not in (0, 1) or state["target_watermark_count"] != state["watermark_row_count"]:
            raise WatermarkError(f"{stage} watermark candidate pre-state row counts are invalid")
        digest = state["current_target_sha256"]
        if (digest is None) != (state["watermark_row_count"] == 0):
            raise WatermarkError(f"{stage} watermark candidate pre-state digest is inconsistent")
        if digest is not None and not SAFE_SHA_RE.fullmatch(str(digest)):
            raise WatermarkError(f"{stage} watermark candidate pre-state digest is invalid")
    source_digests = artifact.get("source_digests")
    if not isinstance(source_digests, dict) or not source_digests or not all(SAFE_SHA_RE.fullmatch(str(value)) for value in source_digests.values()):
        raise WatermarkError("watermark source digests are incomplete")
    expected_safety = {
        "runtime_mode": "shadow",
        "production_writes_enabled": False,
        "active_ingestion_owner": "legacy_shards",
        "legacy_ingestion_owner_unchanged": True,
        "main_and_retry_queues_drained": True,
        "rabbitmq_dlq_growth": 0,
        "article_or_domain_writes": False,
        "outbox_replay": False,
        "queue_mutation": False,
        "consumer_mutation": False,
        "scheduler_mutation": False,
        "schema_mutation": False,
        "infrastructure_mutation": False,
        "dns_or_failover_mutation": False,
    }
    if artifact.get("safety") != expected_safety:
        raise WatermarkError("watermark candidate safety invariants are not exact")


def sql_rows(rows: list[dict[str, Any]]) -> str:
    normalized = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "schema"}
        item["schema_name"] = row["schema"]
        normalized.append(item)
    return canonical_json(normalized)


def upsert_cte(stage: str, schema: str) -> str:
    target = f"{schema}.reconciliation_watermarks"
    cte = stage.replace("-", "_") + "_upsert"
    return f"""
{cte} as (
  insert into {target} (
    watermark_name, cursor_value, confirmed_message_id, confirmed_at,
    lag_count, diagnostic_metadata, redact_after
  )
  select watermark_name, cursor_value, confirmed_message_id, confirmed_at,
    lag_count, diagnostic_metadata, redact_after
  from candidate, exact_guard
  where stage = '{stage}' and schema_name = '{schema}' and exact_guard.allowed = 1
  on conflict (watermark_name) do update set
    cursor_value = excluded.cursor_value,
    confirmed_message_id = excluded.confirmed_message_id,
    confirmed_at = excluded.confirmed_at,
    lag_count = excluded.lag_count,
    diagnostic_metadata = excluded.diagnostic_metadata,
    redact_after = excluded.redact_after
  where coalesce(
    nullif({target}.diagnostic_metadata->>'capturedAtUtc', '')::timestamptz,
    {target}.confirmed_at
  ) <= nullif(excluded.diagnostic_metadata->>'capturedAtUtc', '')::timestamptz
  returning 1
)"""


def apply_sql(rows: list[dict[str, Any]]) -> str:
    payload = sql_rows(rows)
    if "$watermarks$" in payload:
        raise WatermarkError("watermark candidate contains a forbidden SQL delimiter")
    ctes = ",\n".join(upsert_cte(stage, SCHEMAS[stage]) for stage in STAGES)
    counts = " + ".join(f"(select count(*) from {stage}_upsert)" for stage in STAGES)
    per_stage = ",".join(f"'{stage}',(select count(*) from {stage}_upsert)" for stage in STAGES)
    allowed_stages = ",".join(f"'{stage}'" for stage in STAGES)
    return f"""
begin;
set local statement_timeout = '30s';
with candidate as (
  select * from jsonb_to_recordset($watermarks${payload}$watermarks$::jsonb) as x(
    stage text,
    schema_name text,
    watermark_name text,
    cursor_value text,
    confirmed_message_id text,
    confirmed_at timestamptz,
    lag_count bigint,
    diagnostic_metadata jsonb,
    redact_after timestamptz
  )
), exact_guard as (
  select 1 / case when count(*) = 8
    and count(distinct stage) = 8
    and count(distinct schema_name) = 8
    and bool_and(stage = any(array[{allowed_stages}]::text[]))
    and bool_and(watermark_name = '{WATERMARK_NAME}')
    and bool_and(lag_count = 0)
    then 1 else 0 end as allowed
  from candidate
),
{ctes}
select json_build_object(
  'upserted_rows', {counts},
  'expected_rows', 8,
  'per_stage', json_build_object({per_stage}),
  'exact_row_guard', 1 / case when ({counts}) = 8 then 1 else 0 end
)::text;
commit;
"""


def expected_db_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: row.get(key)
            for key in (
                "watermark_name",
                "cursor_value",
                "confirmed_message_id",
                "confirmed_at",
                "lag_count",
                "diagnostic_metadata",
                "redact_after",
            )
        }
        for row in artifact["candidate"]["rows"]
    ]


def current_db_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in snapshot_stages(snapshot):
        target = item.get("current_target")
        if not isinstance(target, dict):
            raise WatermarkError(f"{item.get('stage')} target watermark row is missing")
        rows.append(target)
    return rows


def non_target_db_state(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in item.items()
            if key not in {"watermark_row_count", "target_watermark_count", "current_target"}
        }
        for item in snapshot_stages(snapshot)
    ]


def apply_candidate(
    contract: dict[str, Any],
    artifact_path: Path,
    password: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != APPLY_CONFIRMATION:
        raise WatermarkError("apply requires the exact typed confirmation")
    if not password or "\n" in password or "\r" in password:
        raise WatermarkError("watermark database credential is unavailable")
    artifact = load_json(artifact_path)
    validate_candidate_artifact(artifact, contract)
    ensure_value_free(artifact, "watermark candidate artifact")
    observed = parse_utc(artifact.get("observed_at_utc"), "candidate.observed_at_utc")
    age = (utc_now() - observed).total_seconds()
    if age < -60 or age > int(contract["evidence"]["maximum_age_seconds"]):
        raise WatermarkError("apply candidate is outside the authorized freshness window")
    privilege = psql_json(PRIVILEGE_SQL, password, context="privilege-proof")
    validate_privilege_proof(privilege)
    before = psql_json(DB_SNAPSHOT_SQL, password, context="pre-apply-snapshot")
    before_stages = snapshot_stages(before)
    for item in before_stages:
        validate_stage_boundary(item)
        target = item.get("current_target")
        if isinstance(target, dict):
            captured = target.get("diagnostic_metadata", {}).get("capturedAtUtc")
            if parse_utc(captured or target.get("confirmed_at"), f"{item['stage']}.current_target") > observed:
                raise WatermarkError("stale evidence cannot overwrite a newer watermark")
    mutation = psql_json(
        apply_sql(artifact["candidate"]["rows"]),
        password,
        context="bounded-upsert",
    )
    if mutation.get("upserted_rows") != 8 or mutation.get("expected_rows") != 8 or mutation.get("exact_row_guard") != 1:
        raise WatermarkError("bounded watermark upsert did not affect exactly eight rows")
    if mutation.get("per_stage") != {stage: 1 for stage in STAGES}:
        raise WatermarkError("bounded watermark upsert did not affect one row per stage")
    after = psql_json(DB_SNAPSHOT_SQL, password, context="post-apply-snapshot")
    after_stages = snapshot_stages(after)
    for item in after_stages:
        validate_stage_boundary(item)
        if item.get("watermark_row_count") != 1 or item.get("target_watermark_count") != 1:
            raise WatermarkError(f"{item.get('stage')} does not contain exactly the declared watermark")
    if [item["schema_fingerprint"] for item in before_stages] != [item["schema_fingerprint"] for item in after_stages]:
        raise WatermarkError("a reconciliation watermark schema changed during apply")
    before_non_target = non_target_db_state(before)
    after_non_target = non_target_db_state(after)
    if before_non_target != after_non_target:
        raise WatermarkError("non-target database state changed during apply")
    if current_db_rows(after) != expected_db_rows(artifact):
        raise WatermarkError("database watermarks do not exactly match the authorized candidate")
    result = {
        "schema_version": 1,
        "status": "applied",
        "operation": "bounded_eight_stage_watermark_upsert",
        "tracking_issue": contract.get("tracking_issue"),
        "applied_at_utc": iso_utc(utc_now()),
        "workflow": {
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "candidate_artifact_sha256": sha256_file(artifact_path),
        "mutation": {
            "watermark_name": WATERMARK_NAME,
            "upserted_rows": 8,
            "per_stage": mutation["per_stage"],
            "delete_performed": False,
            "truncate_performed": False,
            "arbitrary_sql_available": False,
            "outbox_replay_performed": False,
            "article_or_domain_write_performed": False,
            "schema_change_performed": False,
        },
        "privilege_proof": privilege,
        "schema_fingerprints": {
            "before": [item["schema_fingerprint"] for item in before_stages],
            "after": [item["schema_fingerprint"] for item in after_stages],
            "unchanged": True,
        },
        "non_target_database_state": {
            "before_sha256": sha256_json(before_non_target),
            "after_sha256": sha256_json(after_non_target),
            "unchanged": True,
        },
        "database_evidence": {
            "row_count": 8,
            "rows": current_db_rows(after),
            "snapshot_sha256": sha256_json(after),
        },
        "safety": artifact["safety"],
    }
    ensure_value_free(result, "watermark apply evidence")
    return result


def verify_post_apply(
    contract: dict[str, Any],
    artifact_path: Path,
    apply_report_path: Path,
    post_runtime_path: Path,
    password: str,
) -> dict[str, Any]:
    artifact = load_json(artifact_path)
    validate_candidate_artifact(artifact, contract)
    apply_report = load_json(apply_report_path)
    if apply_report.get("status") != "applied" or apply_report.get("candidate_artifact_sha256") != sha256_file(artifact_path):
        raise WatermarkError("watermark apply report is not bound to the exact candidate")
    post_runtime = load_json(post_runtime_path)
    runtime_contract = load_json(RUNTIME_CONTRACT)
    try:
        RUNTIME.validate_candidate_artifact(post_runtime, runtime_contract)
        RUNTIME.ensure_value_free(post_runtime, "post-apply runtime evidence")
    except Exception as exc:
        raise WatermarkError(f"post-apply runtime evidence failed closed: {exc}") from exc
    pre_runtime_state = {
        row["stage"]: {
            "active_consumers": None if row["stage"] == "scheduler" else 1,
            "deployment_version": row["diagnostic_metadata"]["runtimeImage"],
            "main_messages": row["diagnostic_metadata"]["mainQueueMessages"],
            "main_ready": 0,
            "main_unacknowledged": 0,
            "retry_messages": row["diagnostic_metadata"]["retryQueueMessages"],
            "dlq_messages": row["diagnostic_metadata"]["rabbitmqDlqMessages"],
        }
        for row in artifact["candidate"]["rows"]
    }
    if runtime_state(post_runtime) != pre_runtime_state:
        raise WatermarkError("runtime, queue, consumer, DLQ, or deployment state changed during watermark apply")
    if artifact["source_digests"]["runtime_manifest_sha256"] != post_runtime["source_digests"]["runtime_manifest_sha256"]:
        raise WatermarkError("runtime manifest changed during watermark apply")
    if artifact["source_digests"]["runtime_compose_sha256"] != post_runtime["source_digests"]["runtime_compose_sha256"]:
        raise WatermarkError("runtime compose changed during watermark apply")
    privilege = psql_json(PRIVILEGE_SQL, password, context="privilege-proof")
    validate_privilege_proof(privilege)
    snapshot = psql_json(DB_SNAPSHOT_SQL, password, context="post-apply-snapshot")
    for item in snapshot_stages(snapshot):
        validate_stage_boundary(item)
        if item.get("watermark_row_count") != 1 or item.get("target_watermark_count") != 1:
            raise WatermarkError(f"{item.get('stage')} post-apply watermark row is missing")
    if current_db_rows(snapshot) != expected_db_rows(artifact):
        raise WatermarkError("post-apply database watermarks differ from the exact candidate")
    non_target_state = apply_report.get("non_target_database_state", {})
    if (
        non_target_state.get("unchanged") is not True
        or non_target_state.get("before_sha256") != non_target_state.get("after_sha256")
        or non_target_state.get("after_sha256") != sha256_json(non_target_db_state(snapshot))
    ):
        raise WatermarkError("non-target database state changed during or after watermark apply")
    result = {
        "schema_version": 1,
        "status": "pass",
        "operation": "post_apply_proof",
        "tracking_issue": contract.get("tracking_issue"),
        "verified_at_utc": iso_utc(utc_now()),
        "workflow": apply_report.get("workflow"),
        "candidate_artifact_sha256": sha256_file(artifact_path),
        "apply_report_sha256": sha256_file(apply_report_path),
        "database_snapshot_sha256": sha256_json(snapshot),
        "proof": {
            "exact_declared_watermark_rows": 8,
            "all_lag_counts_zero": True,
            "candidate_rows_match_database": True,
            "non_target_watermark_rows_unchanged": True,
            "non_target_database_state_unchanged": True,
            "target_schema_fingerprints_unchanged": apply_report.get("schema_fingerprints", {}).get("unchanged") is True,
            "runtime_mode_unchanged": True,
            "production_writes_enabled_false": True,
            "active_ingestion_owner_unchanged": True,
            "consumer_counts_unchanged": True,
            "queue_counts_unchanged": True,
            "runtime_manifest_digest_unchanged": True,
            "runtime_compose_digest_unchanged": True,
            "article_or_domain_mutation_privileges_available": False,
            "rabbitmq_mutation_credentials_available": False,
            "dns_or_failover_credentials_available": False,
            "infrastructure_mutation_credentials_available": False,
        },
        "guardrails": {
            "cutover_performed": False,
            "uplift_production_writes_enabled": False,
            "ingestion_owner_changed": False,
            "legacy_worker_changed": False,
            "dns_or_failover_changed": False,
            "article_or_domain_write_performed": False,
            "outbox_replay_performed": False,
            "queue_mutation_performed": False,
            "consumer_mutation_performed": False,
            "scheduler_mutation_performed": False,
            "infrastructure_mutation_performed": False,
            "schema_change_performed": False,
        },
    }
    if result["proof"] != contract.get("post_apply_proof"):
        raise WatermarkError("post-apply proof does not match the exact authorization contract")
    ensure_value_free(result, "watermark post-apply evidence")
    return result


def write_output(path: Path | None, value: dict[str, Any]) -> None:
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def password_from_env(name: str | None) -> str:
    if not name:
        raise WatermarkError("database password environment name is required")
    return os.environ.get(name, "")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("dry-run", "apply", "verify"), required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--runtime-first", type=Path)
    parser.add_argument("--runtime-followup", type=Path)
    parser.add_argument("--post-runtime", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--apply-report", type=Path)
    parser.add_argument("--db-password-env")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        contract = load_json(args.contract)
        password = password_from_env(args.db_password_env)
        if args.mode == "dry-run":
            if args.runtime_first is None or args.runtime_followup is None:
                raise WatermarkError("dry-run requires both runtime stability candidates")
            result = build_candidate(contract, args.runtime_first, args.runtime_followup, password)
        elif args.mode == "apply":
            if args.artifact is None:
                raise WatermarkError("apply requires the exact candidate artifact")
            result = apply_candidate(contract, args.artifact, password, args.confirmation)
        else:
            if args.artifact is None or args.apply_report is None or args.post_runtime is None:
                raise WatermarkError("verify requires candidate, apply report, and post-runtime evidence")
            result = verify_post_apply(contract, args.artifact, args.apply_report, args.post_runtime, password)
        write_output(args.output, result)
        return 0
    except (OSError, ValueError, WatermarkError) as exc:
        print(f"worker-uplift cutover watermark operation failed closed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
