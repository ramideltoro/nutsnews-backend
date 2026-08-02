#!/usr/bin/env python3
"""Fixed-command worker-uplift runtime manager.

The script intentionally exposes a closed action set. It validates the
source-controlled manifest before mutating Docker Compose state and writes
redacted JSON reports for protected workflow artifacts.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request


IMAGE_RE = re.compile(r"^(?P<repo>ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*)@sha256:(?P<digest>[0-9a-f]{64})$")
SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
SECRET_KEY_RE = re.compile(r"(PASSWORD|PASS|TOKEN|SECRET|PRIVATE|KEY|COOKIE)", re.IGNORECASE)
TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)

READ_ONLY_ACTIONS = {"check", "status", "logs", "queue-inspect", "dlq-inspect"}
MUTATING_ACTIONS = {"deploy", "promote", "restart", "scale", "rollback", "dlq-replay", "drain", "reconciliation", "smoke"}
ALL_ACTIONS = sorted(READ_ONLY_ACTIONS | MUTATING_ACTIONS)

WORKER_MAIN_EXCHANGE = "nutsnews.worker.v1"
WORKER_ENVELOPE_SCHEMA_ID = "nutsnews.worker.envelope.v1"
WORKER_MAX_ATTEMPTS = 4
STAGE_PAYLOAD_SCHEMA_VERSION = 1
STAGE_PAYLOAD_SCHEMA_IDS = {
    "feed_fetch_request": "nutsnews.worker.payload.feed-fetch-request.v1",
    "enrichment_result": "nutsnews.worker.payload.enrichment-result.v1",
    "persistence_command": "nutsnews.worker.payload.persistence-command.v1",
    "publication_readiness": "nutsnews.worker.payload.publication-readiness.v1",
    "translation_task": "nutsnews.worker.payload.translation-task.v1",
}
SMOKE_TARGET_LANGUAGES = ["fr", "ja", "de-CH", "de", "el"]
SMOKE_PIPELINE_TIMEOUT_SECONDS = 420
SERVICE_HTTP_PORTS = {
    "scheduler": 18081,
    "fetcher": 18082,
    "canonicalizer": 18083,
    "enrichment": 18084,
    "approval": 18085,
    "translation": 18086,
    "persistence": 18087,
    "publication": 18088,
}
SERVICE_DATABASE_ENV_KEYS = {
    "scheduler": "NUTSNEWS_SCHEDULER_DATABASE_URL",
    "fetcher": "NUTSNEWS_FETCHER_DATABASE_URL",
    "canonicalizer": "NUTSNEWS_CANONICALIZER_DATABASE_URL",
    "enrichment": "NUTSNEWS_ENRICHMENT_DATABASE_URL",
    "approval": "NUTSNEWS_APPROVAL_DATABASE_URL",
    "persistence": "NUTSNEWS_PERSISTENCE_DATABASE_URL",
    "publication": "NUTSNEWS_PUBLICATION_DATABASE_URL",
    "translation": "NUTSNEWS_TRANSLATION_DATABASE_URL",
}
STAGE_SCHEMAS = {
    "scheduler": "worker_uplift_scheduler",
    "fetcher": "worker_uplift_fetcher",
    "canonicalizer": "worker_uplift_canonicalizer",
    "enrichment": "worker_uplift_enrichment",
    "approval": "worker_uplift_approval",
    "translation": "worker_uplift_translation",
    "persistence": "worker_uplift_persistence",
    "publication": "worker_uplift_publication",
}
PIPELINE_SMOKE_STAGES = ["fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"]
RECONCILIATION_STALE_AFTER_SECONDS = 900
RECONCILIATION_MAX_CANDIDATES = 100
RECONCILIATION_ENDPOINT_PATH = "/reconcile/outbox"
RECONCILIATION_CONFIRMATIONS = {
    "scheduler": "scheduler:fail-closed:v1",
    "fetcher": "fetcher:fail-closed:v1",
    "canonicalizer": "canonicalizer:fail-closed:v1",
    "enrichment": "enrichment:fail-closed:v1",
    "approval": "approval:replay-outbox:v1",
    "translation": "translation:replay-outbox:v1",
    "persistence": "persistence:replay-outbox:v1",
    "publication": "publication:terminal-reconcile:v1",
}
TRACKING_QUERY_PARAMETER_NAMES = {
    "cmp",
    "cmpid",
    "dclid",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "oly_anon_id",
    "oly_enc_id",
    "ref",
    "ref_src",
    "spm",
    "twclid",
    "vero_id",
}


class RuntimeErrorWithReport(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    value = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    value = TOKEN_RE.sub("<redacted-token>", value)
    value = URL_SECRET_RE.sub(r"\1<redacted>\3", value)
    return value


def redacted_json(value: Any) -> Any:
    try:
        encoded = json.dumps(value)
        return json.loads(redact(encoded))
    except (TypeError, json.JSONDecodeError):
        return redact(str(value))


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeErrorWithReport(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeErrorWithReport(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeErrorWithReport("manifest root must be a JSON object")
    return data


def service_image_parts(image: str) -> tuple[str, str]:
    match = IMAGE_RE.match(image)
    if not match:
        raise ValueError("image must be a lower-case GHCR digest reference: ghcr.io/...@sha256:<64 hex>")
    return match.group("repo"), f"sha256:{match.group('digest')}"


def repository_allowed(repository: str, allowed: list[str]) -> bool:
    return any(repository == item.rstrip("/") or repository.startswith(item) for item in allowed)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if manifest.get("tracking_issue") != 85:
        errors.append("tracking_issue must be 85")
    production_writes_enabled = manifest.get("production_writes_enabled") is True
    cutover_state = manifest.get("cutover_state")
    if manifest.get("mode") != "shadow" and not production_writes_enabled:
        errors.append("runtime mode must default to shadow before protected cutover")
    if cutover_state not in {"shadow", "cutover-approved"}:
        errors.append("cutover_state must be shadow or cutover-approved")
    if production_writes_enabled and cutover_state != "cutover-approved":
        errors.append("production writes require cutover_state=cutover-approved")
    backend_api = manifest.get("backend_api", {})
    if production_writes_enabled and not backend_api.get("writes_enabled"):
        errors.append("production writes require backend_api.writes_enabled=true")

    allowed_repos = manifest.get("allowed_image_repositories", [])
    if not isinstance(allowed_repos, list) or not allowed_repos:
        errors.append("allowed_image_repositories must be a non-empty list")
        allowed_repos = []
    allowed_sources = set(manifest.get("allowed_source_repositories", []))
    allowed_stages = set(manifest.get("allowed_stages", []))
    max_replicas = int(manifest.get("max_replicas_per_service", 0) or 0)
    if max_replicas < 1:
        errors.append("max_replicas_per_service must be at least 1")

    services = manifest.get("services", [])
    if not isinstance(services, list):
        errors.append("services must be a list")
        services = []
    seen_names: set[str] = set()
    for service in services:
        if not isinstance(service, dict):
            errors.append("service entries must be objects")
            continue
        name = str(service.get("name") or "")
        if not SERVICE_RE.match(name):
            errors.append(f"service {name or '<missing>'} has invalid name")
        if name in seen_names:
            errors.append(f"duplicate service name: {name}")
        seen_names.add(name)
        stage = str(service.get("stage") or "")
        if stage not in allowed_stages:
            errors.append(f"service {name} has unsupported stage: {stage}")
        try:
            repository, digest = service_image_parts(str(service.get("image") or ""))
        except ValueError as exc:
            errors.append(f"service {name} image invalid: {exc}")
            repository = ""
            digest = ""
        if repository and not repository_allowed(repository, allowed_repos):
            errors.append(f"service {name} uses untrusted image repository: {repository}")
        if ":" in str(service.get("image") or "").split("@", 1)[0]:
            errors.append(f"service {name} image must not include a mutable tag")
        if service.get("runtime_mode", "shadow") != "shadow":
            errors.append(f"service {name} runtime_mode must remain shadow")
        network_mode = service.get("network_mode")
        if network_mode not in {None, "bridge", "host"}:
            errors.append(f"service {name} network_mode must be bridge or host")
        replicas = int(service.get("replicas", 0) or 0)
        if replicas < 0 or replicas > max_replicas:
            errors.append(f"service {name} replicas must be between 0 and {max_replicas}")
        resources = service.get("resources", {})
        if not isinstance(resources, dict) or not resources.get("memory") or not resources.get("cpus"):
            errors.append(f"service {name} must declare memory and CPU limits")
        if not service.get("healthcheck"):
            errors.append(f"service {name} must declare a healthcheck")
        provenance = service.get("provenance", {})
        if not isinstance(provenance, dict):
            errors.append(f"service {name} provenance must be an object")
            provenance = {}
        if provenance.get("required") is not True:
            errors.append(f"service {name} provenance.required must be true")
        if provenance.get("signed") is not True:
            errors.append(f"service {name} provenance.signed must be true")
        if digest and provenance.get("subject_digest") != digest:
            errors.append(f"service {name} provenance subject_digest must match image digest")
        if provenance.get("source_repository") not in allowed_sources:
            errors.append(f"service {name} provenance source_repository is not allow-listed")
        env = service.get("env", {})
        if not isinstance(env, dict):
            errors.append(f"service {name} env must be an object")
            env = {}
        for key, value in env.items():
            if SECRET_KEY_RE.search(str(key)) and value not in {"", None} and not str(key).endswith("_FILE"):
                errors.append(f"service {name} env key {key} looks secret-bearing; use secret_files or *_FILE")
        for secret in service.get("secret_files", []):
            if not isinstance(secret, dict):
                errors.append(f"service {name} secret file entries must be objects")
                continue
            if not SERVICE_RE.match(str(secret.get("name") or "")):
                errors.append(f"service {name} secret file name is invalid")
            if not str(secret.get("env_key") or "").endswith("_FILE"):
                errors.append(f"service {name} secret file {secret.get('name')} must expose an *_FILE env key")
            if secret.get("value") not in {"", None}:
                errors.append(f"service {name} secret file {secret.get('name')} must not store values in manifest")
            if not str(secret.get("path") or "").startswith("/run/secrets/"):
                errors.append(f"service {name} secret file {secret.get('name')} must mount under /run/secrets")
            if not str(secret.get("host_path") or "").startswith("/etc/nutsnews-worker-uplift/services/"):
                errors.append(f"service {name} secret file {secret.get('name')} must use a root-owned service host_path")
        for secret in service.get("secret_env", []):
            if not isinstance(secret, dict):
                errors.append(f"service {name} secret env entries must be objects")
                continue
            if not SERVICE_RE.match(str(secret.get("name") or "")):
                errors.append(f"service {name} secret env name is invalid")
            env_key = str(secret.get("env_key") or "")
            if not ENV_KEY_RE.match(env_key):
                errors.append(f"service {name} secret env {secret.get('name')} has invalid env_key")
            if env_key.endswith("_FILE"):
                errors.append(f"service {name} secret env {secret.get('name')} must expose the direct env key, not *_FILE")
            if secret.get("value") not in {"", None}:
                errors.append(f"service {name} secret env {secret.get('name')} must not store values in manifest")
    return errors


def service_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {service["name"]: service for service in manifest.get("services", []) if isinstance(service, dict) and service.get("name")}


def require_service(manifest: dict[str, Any], name: str | None) -> dict[str, Any]:
    if not name:
        raise RuntimeErrorWithReport("service_name is required for this action")
    services = service_map(manifest)
    if name not in services:
        raise RuntimeErrorWithReport(f"unknown service_name: {name}")
    return services[name]


def run_command(argv: list[str], timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    return {
        "argv": argv[:3] + ["<args-redacted>"] if argv and argv[0] == "docker" else argv,
        "returncode": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def compose_base(args: argparse.Namespace) -> list[str]:
    return ["docker", "compose", "-f", str(args.compose), "--project-name", args.project]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def service_env(args: argparse.Namespace, service: dict[str, Any]) -> dict[str, str]:
    env_path = args.manifest.parent / "services" / f"{service['name']}.env"
    return read_env(env_path)


def service_database_url(args: argparse.Namespace, service: dict[str, Any]) -> str:
    env = service_env(args, service)
    key = SERVICE_DATABASE_ENV_KEYS.get(str(service.get("name") or ""), "")
    db_url = env.get(key, "")
    if not db_url:
        raise RuntimeErrorWithReport(f"{key or 'service database URL'} is not configured for smoke")
    return db_url


def service_reconciliation_token(args: argparse.Namespace, service: dict[str, Any]) -> str:
    env = service_env(args, service)
    stage = str(service.get("stage") or service.get("name") or "").upper().replace("-", "_")
    for key in (f"NUTSNEWS_{stage}_RECONCILIATION_TOKEN", "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN"):
        token = env.get(key, "").strip()
        if token:
            return token
    raise RuntimeErrorWithReport(f"reconciliation token is not configured for service {service.get('name')}")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def psql_key_values(db_url: str, query: str, timeout: int = 30) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeErrorWithReport("psql is not installed on backend host") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeErrorWithReport("psql smoke query timed out") from exc
    if completed.returncode != 0:
        raise RuntimeErrorWithReport(f"psql smoke query failed: {redact(completed.stderr).strip()}")

    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def reconciliation_plan_query(schema: str) -> str:
    stage_schema = sql_identifier(schema)
    stale_after = f"{RECONCILIATION_STALE_AFTER_SECONDS} seconds"
    return f"""
select 'received_inbox=' || count(*)::text
from {stage_schema}.inbox
where status in ('received', 'processing')
union all
select 'stale_unprocessed_inbox=' || count(*)::text
from {stage_schema}.inbox
where status in ('received', 'processing')
  and received_at < now() - interval {sql_literal(stale_after)}
union all
select 'failed_or_parked_inbox=' || count(*)::text
from {stage_schema}.inbox
where status in ('failed', 'parked')
union all
select 'unconfirmed_outbox=' || count(*)::text
from {stage_schema}.outbox
where status in ('pending', 'published', 'retrying')
  and confirmed_at is null
union all
select 'stale_unconfirmed_outbox=' || count(*)::text
from {stage_schema}.outbox
where status in ('pending', 'published', 'retrying')
  and confirmed_at is null
  and created_at < now() - interval {sql_literal(stale_after)}
union all
select 'dead_lettered_outbox=' || count(*)::text
from {stage_schema}.outbox
where status = 'dead_lettered'
union all
select 'oldest_unconfirmed_outbox_age_seconds=' || coalesce(floor(extract(epoch from now() - min(created_at)))::bigint, 0)::text
from {stage_schema}.outbox
where status in ('pending', 'published', 'retrying')
  and confirmed_at is null
union all
select 'watermark_rows=' || count(*)::text
from {stage_schema}.reconciliation_watermarks
union all
select 'watermark_lag_total=' || coalesce(sum(lag_count), 0)::text
from {stage_schema}.reconciliation_watermarks
union all
select 'sample_pipeline_count=' || count(distinct pipeline_run_id)::text
from (
  select pipeline_run_id
  from {stage_schema}.inbox
  where status in ('received', 'processing', 'failed', 'parked')
  order by received_at asc
  limit {RECONCILIATION_MAX_CANDIDATES}
) candidate_inbox
union all
select 'sample_outbox_pipeline_count=' || count(distinct pipeline_run_id)::text
from (
  select pipeline_run_id
  from {stage_schema}.outbox
  where status in ('pending', 'published', 'retrying', 'dead_lettered')
    and confirmed_at is null
  order by created_at asc
  limit {RECONCILIATION_MAX_CANDIDATES}
) candidate_outbox;
"""


def int_value(values: dict[str, str], key: str) -> int:
    try:
        return int(values.get(key, "0"))
    except ValueError:
        return 0


def reconciliation_planned_actions(values: dict[str, str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if int_value(values, "stale_unprocessed_inbox") > 0:
        actions.append({
            "id": "service-owned-inbox-reconcile",
            "reason": "stale received/processing inbox rows exist",
            "candidate_count": int_value(values, "stale_unprocessed_inbox"),
            "apply_status": "blocked_until_service_reconciler_exists",
        })
    if int_value(values, "stale_unconfirmed_outbox") > 0 or int_value(values, "unconfirmed_outbox") > 0:
        actions.append({
            "id": "service-owned-outbox-republish",
            "reason": "unconfirmed outbox rows exist",
            "candidate_count": int_value(values, "unconfirmed_outbox"),
            "apply_status": "blocked_until_service_replayer_exists",
        })
    if int_value(values, "failed_or_parked_inbox") > 0 or int_value(values, "dead_lettered_outbox") > 0:
        actions.append({
            "id": "operator-review-failure-bucket",
            "reason": "failed, parked, or dead-lettered rows require bounded replay policy review",
            "candidate_count": int_value(values, "failed_or_parked_inbox") + int_value(values, "dead_lettered_outbox"),
            "apply_status": "manual_review_required",
        })
    if int_value(values, "watermark_lag_total") > 0:
        actions.append({
            "id": "watermark-verification",
            "reason": "reconciliation watermark lag is non-zero",
            "candidate_count": int_value(values, "watermark_lag_total"),
            "apply_status": "blocked_until_watermark_owner_signoff",
        })
    if not actions:
        actions.append({
            "id": "no-op",
            "reason": "no stale inbox, unconfirmed outbox, failure bucket, or watermark lag candidates found",
            "candidate_count": 0,
            "apply_status": "not_required",
        })
    return actions


def build_reconciliation_plan(args: argparse.Namespace, service: dict[str, Any]) -> dict[str, Any]:
    stage = str(service.get("stage") or service.get("name") or "")
    schema = STAGE_SCHEMAS.get(stage)
    if not schema:
        raise RuntimeErrorWithReport(f"no reconciliation schema is registered for stage {stage}")
    query = reconciliation_plan_query(schema)
    values = psql_key_values(service_database_url(args, service), query, timeout=45)
    return {
        "stage": stage,
        "schema": schema,
        "safe_metadata_only": True,
        "writes_performed": False,
        "production_visibility_enabled": False,
        "legacy_runtime_required": False,
        "stale_after_seconds": RECONCILIATION_STALE_AFTER_SECONDS,
        "candidate_limit": RECONCILIATION_MAX_CANDIDATES,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "values": values,
        "planned_actions": reconciliation_planned_actions(values),
        "apply_requirements": [
            "service-owned reconciler/replayer entry point",
            "service-owned payload_ref hydration",
            "new message IDs with preserved idempotency, causation, correlation, and audit metadata",
            "bounded protected confirmation and stop switch",
        ],
    }


def reconciliation_request_body(args: argparse.Namespace, service: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    stage = str(service.get("stage") or service.get("name") or "")
    mode = "dry-run" if args.dry_run else "apply"
    run_id = f"backend:{stage}:reconciliation:{uuid.uuid4()}"
    body: dict[str, Any] = {
        "mode": mode,
        "runId": run_id,
        "reason": "backend-worker-runtime-protected-reconciliation",
        "maxItems": int(plan.get("candidate_limit", RECONCILIATION_MAX_CANDIDATES)),
        "minAgeSeconds": int(plan.get("stale_after_seconds", RECONCILIATION_STALE_AFTER_SECONDS)),
    }
    if mode == "apply":
        confirmation = RECONCILIATION_CONFIRMATIONS.get(stage)
        if not confirmation:
            raise RuntimeErrorWithReport(f"no protected reconciliation confirmation is registered for stage {stage}")
        body["protectedConfirmation"] = confirmation
    return body


def invoke_service_reconciliation(args: argparse.Namespace, service: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    stage = str(service.get("stage") or service.get("name") or "")
    port = SERVICE_HTTP_PORTS.get(stage)
    if port is None:
        raise RuntimeErrorWithReport(f"no reconciliation HTTP port is registered for stage {stage}")

    body = reconciliation_request_body(args, service, plan)
    token = service_reconciliation_token(args, service)
    response = http_post_json_local(RECONCILIATION_ENDPOINT_PATH, port, body, token)
    service_report = response.get("body")
    return {
        "endpoint": f"http://127.0.0.1:{port}{RECONCILIATION_ENDPOINT_PATH}",
        "request": {
            "mode": body["mode"],
            "runId": body["runId"],
            "reason": body["reason"],
            "maxItems": body["maxItems"],
            "minAgeSeconds": body["minAgeSeconds"],
            "protectedConfirmationProvided": "protectedConfirmation" in body,
        },
        "response": response,
        "service_report": service_report if isinstance(service_report, dict) else {},
    }


def service_reconciliation_outcome(invocation: dict[str, Any]) -> tuple[bool, str]:
    response = invocation.get("response", {})
    if not isinstance(response, dict) or response.get("status") != "received":
        return False, "service-owned reconciliation endpoint did not return a usable response"

    service_report = invocation.get("service_report", {})
    if not isinstance(service_report, dict):
        return False, "service-owned reconciliation endpoint did not return a JSON report"

    service_status = service_report.get("status")
    writes_performed = service_report.get("writesPerformed") is True
    selected_count = int(service_report.get("selectedCount", 0) or 0)
    production_visibility_enabled = service_report.get("productionVisibilityEnabled") is True
    legacy_runtime_required = service_report.get("legacyRuntimeRequired") is True

    if production_visibility_enabled or legacy_runtime_required:
        return False, "service reconciliation report enabled production visibility or required legacy runtime"
    if service_status in {"dry_run", "applied"}:
        return True, "service-owned reconciliation endpoint completed"
    if service_status == "failed_closed" and not writes_performed and selected_count == 0:
        return True, "service-owned reconciliation endpoint failed closed before selecting replay candidates"
    if service_status == "kill_switch_active":
        return False, "service-owned reconciliation stop switch is active"
    return False, f"service-owned reconciliation ended with status {service_status or '<missing>'}"


def wait_for_psql_values(
    db_url: str,
    query: str,
    expected: dict[str, str],
    timeout_seconds: int = 90,
    interval_seconds: float = 3.0,
) -> tuple[dict[str, str], bool]:
    deadline = time.monotonic() + timeout_seconds
    last_values: dict[str, str] = {}
    while time.monotonic() < deadline:
        last_values = psql_key_values(db_url, query)
        if all(last_values.get(key) == value for key, value in expected.items()):
            return last_values, True
        time.sleep(interval_seconds)
    return last_values, False


def http_get_local(path: str, port: int, timeout: int = 10) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "status": "healthy" if 200 <= response.status < 300 else "critical",
                "http_status": response.status,
                "body": redact(body[:2048]),
            }
    except error.HTTPError as exc:
        return {
            "status": "critical",
            "http_status": exc.code,
            "body": redact(exc.read().decode("utf-8", errors="replace")[:2048]),
        }
    except OSError as exc:
        return {
            "status": "critical",
            "error": type(exc).__name__,
        }


def http_post_json_local(path: str, port: int, payload: dict[str, Any], token: str, timeout: int = 30) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with request.urlopen(req, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return {
                "status": "received" if 200 <= response.status < 300 else "critical",
                "http_status": response.status,
                "body": parse_json_body(response_body),
            }
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "received" if exc.code in {400, 409, 423} else "critical",
            "http_status": exc.code,
            "body": parse_json_body(response_body),
        }
    except OSError as exc:
        return {
            "status": "critical",
            "error": type(exc).__name__,
            "summary": "service-owned reconciliation endpoint is not reachable",
        }


def parse_json_body(body: str) -> Any:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return redact(body[:2048])
    return redacted_json(parsed)


def rabbitmq_admin_env(args: argparse.Namespace) -> tuple[str, str, str]:
    env = read_env(args.rabbitmq_env)
    user = env.get("RABBITMQ_DEFAULT_USER", "")
    password = env.get("RABBITMQ_DEFAULT_PASS", "")
    vhost = env.get("RABBITMQ_DEFAULT_VHOST", args.vhost)
    if not user or not password:
        raise RuntimeErrorWithReport("RabbitMQ admin env not readable")
    return user, password, vhost


def rabbitmq_get_json(args: argparse.Namespace, queue: str) -> dict[str, Any]:
    try:
        user, password, vhost = rabbitmq_admin_env(args)
    except RuntimeErrorWithReport:
        return {"status": "not_configured", "queue": queue, "summary": "RabbitMQ admin env not readable"}
    path = f"/api/queues/{parse.quote(vhost, safe='')}/{parse.quote(queue, safe='')}"
    url = f"http://127.0.0.1:15672{path}"
    req = request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return {"status": "critical", "queue": queue, "http_status": exc.code, "summary": "RabbitMQ queue API failed"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "unknown", "queue": queue, "summary": f"RabbitMQ queue API unavailable: {type(exc).__name__}"}
    keep = ("name", "vhost", "state", "messages", "messages_ready", "messages_unacknowledged", "consumers", "consumer_capacity")
    return {"status": "healthy", "queue": queue, "metrics": {key: data.get(key) for key in keep if key in data}}


def rabbitmq_publish(
    args: argparse.Namespace,
    envelope: dict[str, Any],
    payload: dict[str, Any],
    routing_key: str,
) -> dict[str, Any]:
    user, password, vhost = rabbitmq_admin_env(args)
    path = f"/api/exchanges/{parse.quote(vhost, safe='')}/{parse.quote(WORKER_MAIN_EXCHANGE, safe='')}/publish"
    url = f"http://127.0.0.1:15672{path}"
    body = json.dumps(
        {
            "properties": {
                "delivery_mode": 2,
                "content_type": "application/json",
                "content_encoding": "utf-8",
                "message_id": envelope["messageId"],
                "correlation_id": envelope["correlationId"],
                "timestamp": int(dt.datetime.fromisoformat(envelope["occurredAt"].replace("Z", "+00:00")).timestamp()),
                "headers": {
                    "schemaId": envelope["schemaId"],
                    "schemaVersion": envelope["schemaVersion"],
                    "route": envelope["route"],
                    "attemptCount": envelope["attempt"]["count"],
                    "idempotencyKey": envelope["idempotencyKey"],
                    "payloadCarrier": "envelope-plus-payload",
                    "traceparent": envelope["traceparent"],
                },
            },
            "routing_key": routing_key,
            "payload": json.dumps({"envelope": envelope, "payload": payload}, separators=(",", ":")),
            "payload_encoding": "string",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return {
            "status": "critical",
            "http_status": exc.code,
            "message_id": envelope["messageId"],
            "routing_key": routing_key,
            "summary": "RabbitMQ publish API failed",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "critical",
            "error": type(exc).__name__,
            "message_id": envelope["messageId"],
            "routing_key": routing_key,
            "summary": "RabbitMQ publish API unavailable",
        }
    routed = data.get("routed") is True
    return {
        "status": "healthy" if routed else "critical",
        "routed": routed,
        "message_id": envelope["messageId"],
        "routing_key": routing_key,
    }


def declared_queues(service: dict[str, Any], kind: str) -> list[str]:
    queues = service.get("queues", {})
    if not isinstance(queues, dict):
        return []
    if kind == "main":
        value = queues.get("main")
        return [value] if isinstance(value, str) else []
    if kind == "retry":
        value = queues.get("retry", [])
        return [item for item in value if isinstance(item, str)]
    if kind == "dlq":
        value = queues.get("dlq")
        return [value] if isinstance(value, str) else []
    return []


def declared_consumed_queues(service: dict[str, Any]) -> list[str]:
    queues = service.get("queues", {})
    if not isinstance(queues, dict):
        return []
    value = queues.get("consumes", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def queue_messages(queue: dict[str, Any]) -> int:
    metrics = queue.get("metrics", {})
    if not isinstance(metrics, dict):
        return 0
    value = metrics.get("messages")
    return int(value) if isinstance(value, int) else 0


def queue_consumers(queue: dict[str, Any]) -> int:
    metrics = queue.get("metrics", {})
    if not isinstance(metrics, dict):
        return 0
    value = metrics.get("consumers")
    return int(value) if isinstance(value, int) else 0


def stage_queue_snapshot(args: argparse.Namespace, manifest: dict[str, Any], stages: list[str], kind: str) -> dict[str, Any]:
    services = service_map(manifest)
    items: dict[str, Any] = {}
    for stage in stages:
        service = services.get(stage)
        if service is None:
            items[stage] = {"status": "missing_service"}
            continue
        queues = declared_queues(service, kind)
        items[stage] = [rabbitmq_get_json(args, queue) for queue in queues]
    return items


def missing_pipeline_consumers(queue_snapshot: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for stage, queues in queue_snapshot.items():
        if not isinstance(queues, list) or not queues:
            missing.append(stage)
            continue
        for queue in queues:
            if queue.get("status") != "healthy" or queue_consumers(queue) < 1:
                missing.append(stage)
                break
    return missing


def service_consumer_readiness(
    args: argparse.Namespace,
    service: dict[str, Any],
) -> dict[str, Any]:
    queues = declared_consumed_queues(service)
    if not queues:
        return {
            "status": "not_applicable",
            "required": False,
            "queues": [],
            "summary": "service does not consume a RabbitMQ queue",
        }

    snapshots = [rabbitmq_get_json(args, queue) for queue in queues]
    zero_consumer_queues = [
        str(snapshot["queue"])
        for snapshot in snapshots
        if snapshot.get("status") == "healthy" and queue_consumers(snapshot) < 1
    ]
    unavailable_queues = [
        str(snapshot["queue"])
        for snapshot in snapshots
        if snapshot.get("status") != "healthy"
    ]
    if zero_consumer_queues:
        status = "critical"
        summary = "one or more required RabbitMQ queues have zero active consumers"
    elif unavailable_queues:
        status = "unknown"
        summary = "RabbitMQ consumer count could not be verified"
    else:
        status = "healthy"
        summary = "all required RabbitMQ queues have active consumers"
    return {
        "status": status,
        "required": True,
        "queues": snapshots,
        "zero_consumer_queues": zero_consumer_queues,
        "unavailable_queues": unavailable_queues,
        "summary": summary,
    }


def dlq_growth(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    growth: dict[str, int] = {}
    for stage, after_queues in after.items():
        if not isinstance(after_queues, list):
            continue
        before_queues = before.get(stage, [])
        before_by_name = {
            item.get("queue"): queue_messages(item)
            for item in before_queues
            if isinstance(item, dict)
        } if isinstance(before_queues, list) else {}
        for queue in after_queues:
            name = queue.get("queue")
            if not isinstance(name, str):
                continue
            delta = queue_messages(queue) - int(before_by_name.get(name, 0))
            if delta > 0:
                growth[name] = delta
    return growth


def smoke_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def smoke_uuid(parts: list[str]) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(parts)))


def smoke_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


class PipelineFixtureServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    fixture: dict[str, str]
    hits: dict[str, int]


class PipelineFixtureHandler(http.server.BaseHTTPRequestHandler):
    server: PipelineFixtureServer

    def do_GET(self) -> None:
        token = self.server.fixture["token"]
        base_path = f"/worker-uplift-shadow-smoke/{token}"
        if self.path == f"{base_path}/feed.xml":
            self.server.hits["feed"] = self.server.hits.get("feed", 0) + 1
            self.write_response("application/rss+xml; charset=utf-8", self.server.fixture["feed_xml"].encode("utf-8"))
            return
        if self.path == f"{base_path}/article.html":
            self.server.hits["article"] = self.server.hits.get("article", 0) + 1
            self.write_response("text/html; charset=utf-8", self.server.fixture["article_html"].encode("utf-8"))
            return
        if self.path == f"{base_path}/image.png":
            self.server.hits["image"] = self.server.hits.get("image", 0) + 1
            self.write_response("image/png", base64.b64decode(self.server.fixture["image_png_base64"]))
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def write_response(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def advertised_fixture_host(args: argparse.Namespace) -> str:
    value = (args.fixture_host or os.environ.get("NUTSNEWS_WORKER_SMOKE_FIXTURE_HOST") or "").strip()
    return value or "65.75.201.18"


def start_pipeline_fixture_server(args: argparse.Namespace, fixture_id: str) -> tuple[PipelineFixtureServer, threading.Thread, dict[str, str]]:
    server = PipelineFixtureServer(("0.0.0.0", 0), PipelineFixtureHandler)
    port = server.server_address[1]
    host = advertised_fixture_host(args)
    token = hashlib.sha256(f"{fixture_id}:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:24]
    base_url = f"http://{host}:{port}/worker-uplift-shadow-smoke/{token}"
    feed_url = f"{base_url}/feed.xml"
    article_url = f"{base_url}/article.html"
    image_url = f"{base_url}/image.png"
    title = "Community volunteers restore a school library after weekend flood damage"
    description = "A deterministic NutsNews shadow fixture about neighbors repairing shelves, donating books, and reopening a school library."
    published_at = "2026-07-26T12:00:00.000Z"
    source_item_id = f"shadow-smoke-{fixture_id}"
    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>NutsNews Worker Shadow Smoke</title>
    <link>{xml_escape(base_url)}</link>
    <description>Deterministic local worker-uplift feed fixture.</description>
    <item>
      <guid isPermaLink="false">{xml_escape(source_item_id)}</guid>
      <title>{xml_escape(title)}</title>
      <link>{xml_escape(article_url)}</link>
      <description>{xml_escape(description)}</description>
      <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
      <enclosure url="{xml_escape(image_url)}" length="67" type="image/png" />
      <media:thumbnail url="{xml_escape(image_url)}" width="1200" height="675" />
    </item>
  </channel>
</rss>
"""
    article_html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>{xml_escape(title)}</title>
    <meta name="description" content="{xml_escape(description)}">
    <meta property="og:title" content="{xml_escape(title)}">
    <meta property="og:description" content="{xml_escape(description)}">
    <meta property="og:image" content="{xml_escape(image_url)}">
    <meta property="og:type" content="article">
    <link rel="canonical" href="{xml_escape(article_url)}">
  </head>
  <body>
    <article>
      <h1>{xml_escape(title)}</h1>
      <p>{xml_escape(description)}</p>
      <p>Students and parents returned Monday to a dry, safe reading room with new donated books.</p>
      <img src="{xml_escape(image_url)}" width="1200" height="675" alt="Restored school library">
    </article>
  </body>
</html>
"""
    fixture = {
        "token": token,
        "feed_id": f"worker-shadow-smoke-{fixture_id}",
        "feed_url": feed_url,
        "article_url": article_url,
        "image_url": image_url,
        "source_item_id": source_item_id,
        "title": title,
        "description": description,
        "published_at": published_at,
        "feed_xml": feed_xml,
        "article_html": article_html,
        "image_png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADElEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    }
    server.fixture = fixture
    server.hits = {}
    thread = threading.Thread(target=server.serve_forever, name="pipeline-fixture-http", daemon=True)
    thread.start()
    return server, thread, fixture


def normalize_article_url(value: str) -> str:
    url = parse.urlsplit(value)
    scheme = url.scheme.lower()
    hostname = (url.hostname or "").lower()
    netloc = hostname
    if url.port is not None and not ((scheme == "https" and url.port == 443) or (scheme == "http" and url.port == 80)):
        netloc = f"{netloc}:{url.port}"
    path = url.path or "/"
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    retained: list[tuple[str, str]] = []
    for key, item_value in parse.parse_qsl(url.query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_PARAMETER_NAMES:
            continue
        retained.append((key, item_value))
    retained.sort()
    query = parse.urlencode(retained)
    return parse.urlunsplit((scheme, netloc, path, query, ""))


def stable_article_id(article_url: str) -> str:
    normalized = normalize_article_url(article_url)
    return "article_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def pipeline_fetch_payload(fixture: dict[str, str], fixture_id: str, occurred_at: str) -> dict[str, Any]:
    message_id = smoke_uuid(["worker-runtime-pipeline-smoke", fixture_id, "fetch-message"])
    idempotency_key = f"smoke:pipeline:feed:{fixture_id}"
    return {
        "schemaId": STAGE_PAYLOAD_SCHEMA_IDS["feed_fetch_request"],
        "schemaVersion": STAGE_PAYLOAD_SCHEMA_VERSION,
        "pipelineRunId": smoke_uuid(["worker-runtime-pipeline-smoke", fixture_id, "pipeline"]),
        "stageExecutionId": smoke_uuid(["worker-runtime-pipeline-smoke", fixture_id, "scheduler-stage"]),
        "sourceMessageId": message_id,
        "idempotencyKey": idempotency_key,
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "producedAt": occurred_at,
        "feedId": fixture["feed_id"],
        "feedUrl": fixture["feed_url"],
        "shardIndex": 0,
        "shardCount": 1,
        "fetchReason": "scheduled",
        "limits": {
            "timeoutMs": 15000,
            "maxItems": 1,
            "scheduleWindowStart": "2026-07-26T12:00:00.000Z",
            "scheduleWindowEnd": "2026-07-26T12:05:00.000Z",
            "priority": "shadow-smoke",
        },
    }


def pipeline_fetch_envelope(fixture: dict[str, str], fixture_id: str, payload: dict[str, Any], occurred_at: str) -> dict[str, Any]:
    idempotency_key = str(payload["idempotencyKey"])
    message_id = str(payload["sourceMessageId"])
    return {
        "schemaId": WORKER_ENVELOPE_SCHEMA_ID,
        "schemaVersion": 1,
        "route": "fetch",
        "messageId": message_id,
        "causationId": message_id,
        "correlationId": smoke_uuid(["worker-runtime-pipeline-smoke", fixture_id, "correlation"]),
        "traceparent": str(payload["traceparent"]),
        "idempotencyKey": idempotency_key,
        "aggregate": {
            "type": "feed",
            "id": fixture["feed_id"],
            "version": 1,
        },
        "occurredAt": occurred_at,
        "attempt": {
            "count": 1,
            "max": WORKER_MAX_ATTEMPTS,
            "firstAttemptAt": occurred_at,
        },
        "producer": {
            "name": "scheduler",
            "version": "0.1.0",
            "instanceId": "backend-worker-runtime-smoke",
        },
        "payloadRef": {
            "kind": "backend-record",
            "uri": f"backend://worker-uplift/scheduler/{parse.quote(idempotency_key, safe='')}",
            "mediaType": "application/json",
            "sizeBytes": payload_size(payload),
            "digest": sha256_json(payload),
        },
    }


def build_pipeline_fetch_publication(args: argparse.Namespace, fixture: dict[str, str], fixture_id: str, occurred_at: str) -> dict[str, Any]:
    payload = pipeline_fetch_payload(fixture, fixture_id, occurred_at)
    envelope = pipeline_fetch_envelope(fixture, fixture_id, payload, occurred_at)
    publish = rabbitmq_publish(args, envelope, payload, "nutsnews.worker.fetch.v1")
    return {
        "fixture": "scheduler-feed-fetch-request",
        "message_id": envelope["messageId"],
        "correlation_id": envelope["correlationId"],
        "pipeline_run_id": payload["pipelineRunId"],
        "idempotency_key": payload["idempotencyKey"],
        "routing_key": "nutsnews.worker.fetch.v1",
        "publish": publish,
    }


def smoke_envelope(
    stage: str,
    producer: str,
    article_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    message_id = smoke_uuid(["worker-runtime-smoke", stage, article_id, idempotency_key])
    return {
        "schemaId": WORKER_ENVELOPE_SCHEMA_ID,
        "schemaVersion": 1,
        "route": stage,
        "messageId": message_id,
        "causationId": smoke_uuid(["worker-runtime-smoke-cause", stage, article_id]),
        "correlationId": smoke_uuid(["worker-runtime-smoke-correlation", article_id]),
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "idempotencyKey": idempotency_key,
        "aggregate": {
            "type": "article",
            "id": article_id,
            "version": 1,
        },
        "occurredAt": occurred_at,
        "attempt": {
            "count": 1,
            "max": WORKER_MAX_ATTEMPTS,
            "firstAttemptAt": occurred_at,
        },
        "producer": {
            "name": producer,
            "version": "0.1.0",
        },
        "payloadRef": {
            "kind": "backend-record",
            "uri": f"backend://worker-uplift/smoke/{stage}/{parse.quote(article_id, safe='')}",
            "mediaType": "application/json",
            "sizeBytes": payload_size(payload),
            "digest": sha256_json(payload),
        },
    }


def approval_smoke_payload(article_id: str, fixture: str, idempotency_key: str, occurred_at: str) -> dict[str, Any]:
    hydrated = fixture == "accepted"
    return {
        "schemaId": STAGE_PAYLOAD_SCHEMA_IDS["enrichment_result"],
        "schemaVersion": STAGE_PAYLOAD_SCHEMA_VERSION,
        "pipelineRunId": smoke_uuid(["worker-runtime-smoke-pipeline", article_id]),
        "stageExecutionId": smoke_uuid(["worker-runtime-smoke-stage", article_id, fixture]),
        "sourceMessageId": smoke_uuid(["worker-runtime-smoke-source", article_id]),
        "idempotencyKey": idempotency_key,
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "producedAt": occurred_at,
        "candidateId": f"candidate-{article_id}",
        "canonicalUrl": f"https://articles.example.test/shadow/{article_id}",
        "imageStatus": "hydrated" if hydrated else "no_thumbnail",
        **({"imageUrl": f"https://images.example.test/shadow/{article_id}.jpg"} if hydrated else {}),
        "articleMetadataRef": {
            "kind": "backend-record",
            "uri": f"backend://worker-uplift/smoke/approval/{article_id}/metadata",
            "mediaType": "application/json",
            "contentFingerprint": f"fingerprint-{article_id}",
            "canonicalArticleId": article_id,
            "articleVersion": 1,
            "title": "Community volunteers build a new accessible playground for local children",
            "description": "A synthetic public-interest fixture about a positive civic project with clear community benefit.",
            "language": "en",
        },
    }


def translation_smoke_payload(article_id: str, idempotency_key: str, occurred_at: str) -> dict[str, Any]:
    return {
        "schemaId": STAGE_PAYLOAD_SCHEMA_IDS["translation_task"],
        "schemaVersion": STAGE_PAYLOAD_SCHEMA_VERSION,
        "pipelineRunId": smoke_uuid(["worker-runtime-smoke-pipeline", article_id]),
        "stageExecutionId": smoke_uuid(["worker-runtime-smoke-stage", article_id, "translation"]),
        "sourceMessageId": smoke_uuid(["worker-runtime-smoke-source", article_id]),
        "idempotencyKey": idempotency_key,
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "producedAt": occurred_at,
        "articleId": article_id,
        "sourceLanguage": "en",
        "targetLanguages": SMOKE_TARGET_LANGUAGES,
        "reason": "new_article",
        "existingLanguageCodes": [],
    }


def smoke_guardrails(service: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    name = str(service.get("name") or "")
    checks: dict[str, Any] = {
        "runtime_mode": service.get("runtime_mode"),
        "production_writes_enabled": service.get("postgres", {}).get("production_write_path") is True,
        "replicas": service.get("replicas"),
        "prefetch": env.get(f"NUTSNEWS_{name.upper()}_PREFETCH"),
        "concurrency": env.get(f"NUTSNEWS_{name.upper()}_CONCURRENCY"),
        "shadow_mode": env.get(f"NUTSNEWS_{name.upper()}_SHADOW_MODE"),
    }
    if name == "approval":
        checks.update({
            "qwen_max_parallel_calls": env.get("NUTSNEWS_APPROVAL_QWEN_MAX_PARALLEL_CALLS"),
            "qwen_max_queued_calls": env.get("NUTSNEWS_APPROVAL_QWEN_MAX_QUEUED_CALLS"),
            "qwen_backpressure_retry_after_ms": env.get("NUTSNEWS_APPROVAL_QWEN_BACKPRESSURE_RETRY_AFTER_MS"),
            "openai_fallback_enabled": env.get("NUTSNEWS_APPROVAL_OPENAI_FALLBACK_ENABLED"),
            "openai_fallback_budget_usd": env.get("NUTSNEWS_APPROVAL_OPENAI_FALLBACK_BUDGET_USD"),
            "openai_secret_present": any(key.startswith("OPENAI_") for key in env),
        })
    if name == "translation":
        checks.update({
            "per_language_concurrency": env.get("NUTSNEWS_TRANSLATION_PER_LANGUAGE_CONCURRENCY"),
            "quality_min_score": env.get("NUTSNEWS_TRANSLATION_QUALITY_MIN_SCORE"),
            "quality_reprompt_max_attempts": env.get("NUTSNEWS_TRANSLATION_QUALITY_REPROMPT_MAX_ATTEMPTS"),
        })
    return checks


def approval_smoke_query(accepted_article_id: str, rejected_article_id: str) -> str:
    accepted = sql_literal(accepted_article_id)
    rejected = sql_literal(rejected_article_id)
    translation_routing = sql_literal("nutsnews.worker.translation.v1")
    return f"""
select 'accepted_decisions=' || count(*)::text
from worker_uplift_approval.approval_decisions
where article_identity_hash = {accepted}
  and decision = 'approved'
  and ai_provider = 'local_ai'
union all
select 'accepted_translation_outbox=' || count(*)::text
from worker_uplift_approval.outbox
where entity_id = {accepted}
  and destination_stage = 'translation'
  and routing_key = {translation_routing}
  and status = 'confirmed'
union all
select 'rejected_decisions=' || count(*)::text
from worker_uplift_approval.approval_decisions
where article_identity_hash = {rejected}
  and decision = 'rejected'
  and diagnostic_metadata->>'rejectionReason' = 'no_thumbnail'
union all
select 'rejected_translation_outbox=' || count(*)::text
from worker_uplift_approval.outbox
where entity_id = {rejected}
  and destination_stage = 'translation'
union all
select 'processed_inbox=' || count(*)::text
from worker_uplift_approval.inbox
where entity_id in ({accepted}, {rejected})
  and status = 'processed'
union all
select 'provider_metadata=' || count(*)::text
from worker_uplift_approval.approval_decisions
where article_identity_hash = {accepted}
  and ai_model = 'qwen2.5:3b'
  and prompt_version = 'editorial-approval-v1:0.1.0'
  and model_metadata ? 'latencyMs';
"""


def translation_smoke_query(article_id: str) -> str:
    article = sql_literal(article_id)
    language_array = ",".join(sql_literal(language) for language in SMOKE_TARGET_LANGUAGES)
    persistence_routing = sql_literal("nutsnews.worker.persistence.v1")
    return f"""
select 'accepted_language_records=' || count(*)::text
from worker_uplift_translation.translation_records
where article_identity_hash = {article}
  and quality_status = 'accepted'
  and ai_provider = 'local_ai'
  and ai_model = 'qwen2.5:3b'
  and language_code in ({language_array})
union all
select 'distinct_languages=' || count(distinct language_code)::text
from worker_uplift_translation.translation_records
where article_identity_hash = {article}
  and quality_status = 'accepted'
  and language_code in ({language_array})
union all
select 'persistence_outbox=' || count(*)::text
from worker_uplift_translation.outbox
where entity_id = {article}
  and destination_stage = 'persistence'
  and routing_key = {persistence_routing}
  and status = 'confirmed'
union all
select 'processed_inbox=' || count(*)::text
from worker_uplift_translation.inbox
where entity_id = {article}
  and status = 'processed'
union all
select 'provider_metadata=' || count(*)::text
from worker_uplift_translation.translation_records
where article_identity_hash = {article}
  and model_metadata ? 'latencyMs'
  and model_metadata ? 'summaryRef';
"""


def final_shadow_smoke_query(article_id: str) -> str:
    article = sql_literal(article_id)
    publication_routing = sql_literal("nutsnews.worker.publication.v1")
    return f"""
select 'final_shadow_aggregate=' || count(*)::text
from worker_uplift_final.article_shadow_aggregates
where article_identity_hash = {article}
  and aggregate_version = 1
  and publication_status = 'ready'
union all
select 'persistence_publication_outbox=' || count(*)::text
from worker_uplift_persistence.outbox
where entity_id = {article}
  and destination_stage = 'publication'
  and routing_key = {publication_routing}
  and status = 'confirmed';
"""


def publication_smoke_query(article_id: str) -> str:
    article = sql_literal(article_id)
    return f"""
select 'publication_readiness=' || count(*)::text
from worker_uplift_publication.publication_readiness
where article_identity_hash = {article}
  and readiness_version = 1
  and approved is true
  and translations_complete is true
  and status = 'ready'
union all
select 'publication_shadow_comparison=' || count(*)::text
from worker_uplift_publication.publication_decisions
where article_identity_hash = {article}
  and decision_version = 1
  and backend_api_operation = 'shadow-publication-comparison'
  and decision in ('refresh_snapshot', 'block');
"""


def pipeline_approval_smoke_query(article_id: str) -> str:
    article = sql_literal(article_id)
    translation_routing = sql_literal("nutsnews.worker.translation.v1")
    return f"""
select 'approved_decision=' || count(*)::text
from worker_uplift_approval.approval_decisions
where article_identity_hash = {article}
  and decision = 'approved'
  and ai_provider = 'local_ai'
union all
select 'rejected_decision=' || count(*)::text
from worker_uplift_approval.approval_decisions
where article_identity_hash = {article}
  and decision = 'rejected'
union all
select 'translation_outbox=' || count(*)::text
from worker_uplift_approval.outbox
where entity_id = {article}
  and destination_stage = 'translation'
  and routing_key = {translation_routing}
  and status = 'confirmed'
union all
select 'processed_inbox=' || count(*)::text
from worker_uplift_approval.inbox
where entity_id = {article}
  and status = 'processed';
"""


def pipeline_api_audit_query(article_id: str) -> str:
    article = sql_literal(article_id)
    return f"""
select 'shadow_api_requests=' || count(*)::text
from worker_uplift_persistence.write_requests
where article_identity_hash = {article}
  and status = 'accepted'
union all
select 'failed_api_requests=' || count(*)::text
from worker_uplift_persistence.write_requests
where article_identity_hash = {article}
  and status = 'failed';
"""


def persistence_smoke_diagnostic_query(article_id: str) -> str:
    article = sql_literal(article_id)
    return f"""
select 'persistence_inbox=' || coalesce(jsonb_agg(to_jsonb(row) order by row.received_at desc), '[]'::jsonb)::text
from (
  select idempotency_key, status, sanitized_error_code, sanitized_error_message, diagnostic_metadata, received_at, processed_at
  from worker_uplift_persistence.inbox
  where entity_id = {article}
  order by received_at desc
  limit 20
) row
union all
select 'persistence_outbox=' || coalesce(jsonb_agg(to_jsonb(row) order by row.created_at desc), '[]'::jsonb)::text
from (
  select idempotency_key, destination_stage, routing_key, status, sanitized_error_code, sanitized_error_message, diagnostic_metadata, created_at, confirmed_at
  from worker_uplift_persistence.outbox
  where entity_id = {article}
  order by created_at desc
  limit 20
) row
union all
select 'final_shadow_aggregates=' || coalesce(jsonb_agg(to_jsonb(row) order by row.updated_at desc), '[]'::jsonb)::text
from (
  select article_identity_hash, aggregate_version, publication_status, diagnostic_metadata, created_at, updated_at
  from worker_uplift_final.article_shadow_aggregates
  where article_identity_hash = {article}
  order by updated_at desc
  limit 5
) row;
"""


def publication_smoke_diagnostic_query(article_id: str) -> str:
    article = sql_literal(article_id)
    return f"""
select 'publication_readiness_rows=' || coalesce(jsonb_agg(to_jsonb(row) order by row.checked_at desc), '[]'::jsonb)::text
from (
  select article_identity_hash, readiness_version, approved, translations_complete, shadow_aggregate_version, status, diagnostic_metadata, checked_at
  from worker_uplift_publication.publication_readiness
  where article_identity_hash = {article}
  order by checked_at desc
  limit 10
) row
union all
select 'publication_decision_rows=' || coalesce(jsonb_agg(to_jsonb(row) order by row.decided_at desc), '[]'::jsonb)::text
from (
  select article_identity_hash, decision_version, decision, reason_code, backend_api_operation, diagnostic_metadata, decided_at
  from worker_uplift_publication.publication_decisions
  where article_identity_hash = {article}
  order by decided_at desc
  limit 10
) row;
"""


def psql_diagnostics(db_url: str, query: str) -> dict[str, str]:
    try:
        return psql_key_values(db_url, query)
    except RuntimeErrorWithReport as exc:
        return {"diagnostic_error": str(exc)}


def build_smoke_publication(args: argparse.Namespace, service: dict[str, Any], article_id: str, fixture: str) -> dict[str, Any]:
    occurred_at = smoke_now()
    if service["name"] == "approval":
        idempotency_key = f"smoke:approval:{fixture}:{article_id}"
        payload = approval_smoke_payload(article_id, fixture, idempotency_key, occurred_at)
        envelope = smoke_envelope("approval", "enrichment", article_id, idempotency_key, payload, occurred_at)
    else:
        idempotency_key = f"smoke:translation:{article_id}"
        payload = translation_smoke_payload(article_id, idempotency_key, occurred_at)
        envelope = smoke_envelope("translation", "approval", article_id, idempotency_key, payload, occurred_at)
    routing_key = str(service.get("queues", {}).get("main") or f"nutsnews.worker.{service['name']}.v1")
    publish = rabbitmq_publish(args, envelope, payload, routing_key)
    return {
        "fixture": fixture,
        "article_id": article_id,
        "message_id": envelope["messageId"],
        "idempotency_key": idempotency_key,
        "publish": publish,
    }


def run_approval_smoke(args: argparse.Namespace, manifest: dict[str, Any], service: dict[str, Any], report: dict[str, Any]) -> None:
    env = service_env(args, service)
    db_url = service_database_url(args, service)
    accepted_article_id = smoke_id("approval-accepted")
    rejected_article_id = smoke_id("approval-rejected")
    report["smoke"] = {
        "fixtures": [
            build_smoke_publication(args, service, accepted_article_id, "accepted"),
            build_smoke_publication(args, service, rejected_article_id, "rejected"),
        ],
        "guardrails": smoke_guardrails(service, env),
        "health": http_get_local("/ready", SERVICE_HTTP_PORTS["approval"]),
        "metrics": {
            "status": http_get_local("/metrics", SERVICE_HTTP_PORTS["approval"]).get("status"),
        },
    }
    if any(item["publish"]["status"] != "healthy" for item in report["smoke"]["fixtures"]):
        report["status"] = "fail"
        report["errors"].append("approval smoke fixture publish failed")
        return
    values, ok = wait_for_psql_values(
        db_url,
        approval_smoke_query(accepted_article_id, rejected_article_id),
        {
            "accepted_decisions": "1",
            "accepted_translation_outbox": "1",
            "rejected_decisions": "1",
            "rejected_translation_outbox": "0",
            "processed_inbox": "2",
            "provider_metadata": "1",
        },
    )
    report["smoke"]["db_checks"] = values
    report["smoke"]["expected_target_languages"] = SMOKE_TARGET_LANGUAGES
    if not ok:
        report["status"] = "fail"
        report["errors"].append("approval smoke did not observe expected accepted/rejected shadow state")
        return

    try:
        persistence_service = require_service(manifest, "persistence")
        publication_service = require_service(manifest, "publication")
        persistence_db_url = service_database_url(args, persistence_service)
        publication_db_url = service_database_url(args, publication_service)
    except RuntimeErrorWithReport as exc:
        report["status"] = "fail"
        report["errors"].append(f"approval downstream final-shadow smoke prerequisites failed: {exc}")
        return

    final_values, final_ok = wait_for_psql_values(
        persistence_db_url,
        final_shadow_smoke_query(accepted_article_id),
        {
            "final_shadow_aggregate": "1",
            "persistence_publication_outbox": "1",
        },
        timeout_seconds=240,
        interval_seconds=5.0,
    )
    publication_values, publication_ok = wait_for_psql_values(
        publication_db_url,
        publication_smoke_query(accepted_article_id),
        {
            "publication_readiness": "1",
            "publication_shadow_comparison": "1",
        },
        timeout_seconds=240,
        interval_seconds=5.0,
    )
    report["smoke"]["downstream_db_checks"] = {
        "accepted_article_id": accepted_article_id,
        "persistence": final_values,
        "publication": publication_values,
    }
    if not (final_ok and publication_ok):
        report["smoke"]["downstream_diagnostics"] = {
            "persistence": psql_diagnostics(persistence_db_url, persistence_smoke_diagnostic_query(accepted_article_id)),
            "publication": psql_diagnostics(publication_db_url, publication_smoke_diagnostic_query(accepted_article_id)),
        }
        report["status"] = "fail"
        report["errors"].append("approval smoke did not observe downstream final-shadow materialization and publication comparison")


def run_translation_smoke(args: argparse.Namespace, service: dict[str, Any], report: dict[str, Any]) -> None:
    env = service_env(args, service)
    db_url = service_database_url(args, service)
    article_id = smoke_id("translation")
    report["smoke"] = {
        "fixtures": [
            build_smoke_publication(args, service, article_id, "translation-task"),
        ],
        "guardrails": smoke_guardrails(service, env),
        "health": http_get_local("/ready", SERVICE_HTTP_PORTS["translation"]),
        "metrics": {
            "status": http_get_local("/metrics", SERVICE_HTTP_PORTS["translation"]).get("status"),
        },
        "expected_target_languages": SMOKE_TARGET_LANGUAGES,
    }
    if report["smoke"]["fixtures"][0]["publish"]["status"] != "healthy":
        report["status"] = "fail"
        report["errors"].append("translation smoke fixture publish failed")
        return
    expected_language_count = str(len(SMOKE_TARGET_LANGUAGES))
    values, ok = wait_for_psql_values(
        db_url,
        translation_smoke_query(article_id),
        {
            "accepted_language_records": expected_language_count,
            "distinct_languages": expected_language_count,
            "persistence_outbox": str(len(SMOKE_TARGET_LANGUAGES) + 1),
            "processed_inbox": "1",
            "provider_metadata": expected_language_count,
        },
        timeout_seconds=150,
    )
    report["smoke"]["db_checks"] = values
    if not ok:
        report["status"] = "fail"
        report["errors"].append("translation smoke did not observe expected per-language shadow state")


def pipeline_approval_diagnostic_query(article_id: str) -> str:
    article = sql_literal(article_id)
    return f"""
select 'approval_decisions=' || coalesce(jsonb_agg(to_jsonb(row) order by row.reviewed_at desc), '[]'::jsonb)::text
from (
  select article_identity_hash, decision, ai_provider, ai_model, diagnostic_metadata, reviewed_at
  from worker_uplift_approval.approval_decisions
  where article_identity_hash = {article}
  order by reviewed_at desc
  limit 10
) row
union all
select 'approval_inbox=' || coalesce(jsonb_agg(to_jsonb(row) order by row.received_at desc), '[]'::jsonb)::text
from (
  select idempotency_key, status, sanitized_error_code, sanitized_error_message, diagnostic_metadata, received_at, processed_at
  from worker_uplift_approval.inbox
  where entity_id = {article}
  order by received_at desc
  limit 10
) row
union all
select 'approval_outbox=' || coalesce(jsonb_agg(to_jsonb(row) order by row.created_at desc), '[]'::jsonb)::text
from (
  select idempotency_key, destination_stage, routing_key, status, sanitized_error_code, sanitized_error_message, diagnostic_metadata, created_at, confirmed_at
  from worker_uplift_approval.outbox
  where entity_id = {article}
  order by created_at desc
  limit 10
) row;
"""


def run_pipeline_shadow_smoke(args: argparse.Namespace, manifest: dict[str, Any], service: dict[str, Any], report: dict[str, Any]) -> None:
    required_services: dict[str, dict[str, Any]] = {"scheduler": service}
    for name in ["approval", "translation", "persistence", "publication", *PIPELINE_SMOKE_STAGES]:
        if name in required_services:
            continue
        try:
            required_services[name] = require_service(manifest, name)
        except RuntimeErrorWithReport as exc:
            report["status"] = "fail"
            report["errors"].append(f"pipeline smoke prerequisite failed: {exc}")
            return

    try:
        approval_db_url = service_database_url(args, required_services["approval"])
        translation_db_url = service_database_url(args, required_services["translation"])
        persistence_db_url = service_database_url(args, required_services["persistence"])
        publication_db_url = service_database_url(args, required_services["publication"])
    except RuntimeErrorWithReport as exc:
        report["status"] = "fail"
        report["errors"].append(f"pipeline smoke database prerequisite failed: {exc}")
        return

    fixture_id = smoke_id("pipeline")
    fixture_server: PipelineFixtureServer | None = None
    fixture_thread: threading.Thread | None = None
    fixture: dict[str, str] = {}
    try:
        fixture_server, fixture_thread, fixture = start_pipeline_fixture_server(args, fixture_id)
        article_id = stable_article_id(fixture["article_url"])
        queue_before = stage_queue_snapshot(args, manifest, PIPELINE_SMOKE_STAGES, "main")
        dlq_before = stage_queue_snapshot(args, manifest, PIPELINE_SMOKE_STAGES, "dlq")
        missing_consumers = missing_pipeline_consumers(queue_before)
        report["smoke"] = {
            "service": service["name"],
            "contract": "scheduler-feed-to-final-shadow-v1",
            "trigger": "scheduler-compatible-feed-fetch-request",
            "legacy_ingestion_endpoints_invoked": False,
            "timeout_seconds": SMOKE_PIPELINE_TIMEOUT_SECONDS,
            "fixture": {
                "fixture_id": fixture_id,
                "feed_id": fixture["feed_id"],
                "feed_url": fixture["feed_url"],
                "article_url": fixture["article_url"],
                "article_id": article_id,
                "source_item_id": fixture["source_item_id"],
            },
            "versions": {
                name: {
                    "image": required_services[name].get("image"),
                    "contract_version": required_services[name].get("contract_version"),
                    "runtime_package_version": required_services[name].get("runtime_package_version"),
                    "runtime_mode": required_services[name].get("runtime_mode"),
                }
                for name in ["scheduler", *PIPELINE_SMOKE_STAGES]
                if name in required_services or name == "scheduler"
            },
            "guardrails": {
                name: smoke_guardrails(required_services[name], service_env(args, required_services[name]))
                for name in PIPELINE_SMOKE_STAGES
                if name in required_services
            },
            "health": {
                name: http_get_local("/ready", SERVICE_HTTP_PORTS[name])
                for name in ["scheduler", *PIPELINE_SMOKE_STAGES]
                if name in SERVICE_HTTP_PORTS
            },
            "queues_before": queue_before,
            "dlqs_before": dlq_before,
            "missing_consumers": missing_consumers,
        }
        if missing_consumers:
            report["status"] = "fail"
            report["errors"].append("pipeline smoke preflight found RabbitMQ stages without active consumers")
            return

        fetch_occurred_at = smoke_now()
        first = build_pipeline_fetch_publication(args, fixture, fixture_id, fetch_occurred_at)
        second = build_pipeline_fetch_publication(args, fixture, fixture_id, fetch_occurred_at)
        report["smoke"]["fixtures"] = [first, second]
        if first["publish"]["status"] != "healthy":
            report["status"] = "fail"
            report["errors"].append("pipeline smoke initial fetch publish failed")
            return
        if second["publish"]["status"] != "healthy":
            report["status"] = "fail"
            report["errors"].append("pipeline smoke duplicate fetch publish failed")
            return

        approval_values, approval_ok = wait_for_psql_values(
            approval_db_url,
            pipeline_approval_smoke_query(article_id),
            {
                "approved_decision": "1",
                "rejected_decision": "0",
                "translation_outbox": "1",
                "processed_inbox": "1",
            },
            timeout_seconds=SMOKE_PIPELINE_TIMEOUT_SECONDS,
            interval_seconds=5.0,
        )
        translation_values, translation_ok = wait_for_psql_values(
            translation_db_url,
            translation_smoke_query(article_id),
            {
                "accepted_language_records": str(len(SMOKE_TARGET_LANGUAGES)),
                "distinct_languages": str(len(SMOKE_TARGET_LANGUAGES)),
                "persistence_outbox": str(len(SMOKE_TARGET_LANGUAGES) + 1),
                "processed_inbox": "1",
                "provider_metadata": str(len(SMOKE_TARGET_LANGUAGES)),
            },
            timeout_seconds=SMOKE_PIPELINE_TIMEOUT_SECONDS,
            interval_seconds=5.0,
        )
        final_values, final_ok = wait_for_psql_values(
            persistence_db_url,
            final_shadow_smoke_query(article_id),
            {
                "final_shadow_aggregate": "1",
                "persistence_publication_outbox": "1",
            },
            timeout_seconds=SMOKE_PIPELINE_TIMEOUT_SECONDS,
            interval_seconds=5.0,
        )
        publication_values, publication_ok = wait_for_psql_values(
            publication_db_url,
            publication_smoke_query(article_id),
            {
                "publication_readiness": "1",
                "publication_shadow_comparison": "1",
            },
            timeout_seconds=SMOKE_PIPELINE_TIMEOUT_SECONDS,
            interval_seconds=5.0,
        )
        api_audit = psql_diagnostics(persistence_db_url, pipeline_api_audit_query(article_id))
        time.sleep(10)
        queue_after = stage_queue_snapshot(args, manifest, PIPELINE_SMOKE_STAGES, "main")
        dlq_after = stage_queue_snapshot(args, manifest, PIPELINE_SMOKE_STAGES, "dlq")
        growth = dlq_growth(dlq_before, dlq_after)
        report["smoke"]["db_checks"] = {
            "approval": approval_values,
            "translation": translation_values,
            "persistence": final_values,
            "publication": publication_values,
            "api_audit": api_audit,
        }
        report["smoke"]["idempotency"] = {
            "duplicate_publish_idempotency_key": second["idempotency_key"],
            "expected_single_final_shadow_result": "1",
            "observed_after_duplicate": {
                "approval": psql_diagnostics(approval_db_url, pipeline_approval_smoke_query(article_id)),
                "translation": psql_diagnostics(translation_db_url, translation_smoke_query(article_id)),
                "persistence": psql_diagnostics(persistence_db_url, final_shadow_smoke_query(article_id)),
                "publication": psql_diagnostics(publication_db_url, publication_smoke_query(article_id)),
            },
        }
        report["smoke"]["queues_after"] = queue_after
        report["smoke"]["dlqs_after"] = dlq_after
        report["smoke"]["dlq_growth"] = growth
        report["smoke"]["fixture_hits"] = dict(fixture_server.hits)
        if not (approval_ok and translation_ok and final_ok and publication_ok) or growth:
            report["status"] = "fail"
            if not (approval_ok and translation_ok and final_ok and publication_ok):
                report["errors"].append("pipeline smoke did not observe the expected policy-valid final shadow result")
            if growth:
                report["errors"].append("pipeline smoke increased one or more DLQs")
            report["smoke"]["diagnostics"] = {
                "approval": psql_diagnostics(approval_db_url, pipeline_approval_diagnostic_query(article_id)),
                "persistence": psql_diagnostics(persistence_db_url, persistence_smoke_diagnostic_query(article_id)),
                "publication": psql_diagnostics(publication_db_url, publication_smoke_diagnostic_query(article_id)),
            }
            return

        if api_audit.get("failed_api_requests") not in {None, "0"}:
            report["status"] = "fail"
            report["errors"].append("pipeline smoke observed a failed backend API shadow request")
    finally:
        if fixture_server is not None:
            fixture_server.shutdown()
            fixture_server.server_close()
        if fixture_thread is not None:
            fixture_thread.join(timeout=2)


def run_service_smoke(args: argparse.Namespace, manifest: dict[str, Any], service: dict[str, Any], report: dict[str, Any]) -> None:
    if args.dry_run:
        report["status"] = "dry_run"
        report["summary"] = "Runtime smoke would publish sanitized service fixtures and verify redacted shadow state."
        report["smoke"] = {
            "service": service["name"],
            "fixtures": ["approval accepted/rejected"] if service["name"] == "approval" else (
                ["translation per-language task"] if service["name"] == "translation" else ["scheduler-compatible feed-to-final pipeline fixture"]
            ),
            "expected_target_languages": SMOKE_TARGET_LANGUAGES if service["name"] in {"approval", "translation"} else [],
            "pipeline_stages": PIPELINE_SMOKE_STAGES if service["name"] == "scheduler" else [],
        }
        return
    if service["name"] == "scheduler":
        run_pipeline_shadow_smoke(args, manifest, service, report)
        return
    if service["name"] == "approval":
        run_approval_smoke(args, manifest, service, report)
        return
    if service["name"] == "translation":
        run_translation_smoke(args, service, report)
        return
    report["status"] = "blocked"
    report["summary"] = "Runtime smoke requires an approved service-specific smoke contract."
    report["errors"].append(f"smoke is not implemented for service {service['name']}")


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o640)


def build_report(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    report: dict[str, Any] = {
        "status": "pass" if not errors else "fail",
        "action": args.action,
        "service_name": args.service_name,
        "generated_at_utc": utc_now(),
        "tracking_issue": 85,
        "mode": manifest.get("mode"),
        "production_writes_enabled": manifest.get("production_writes_enabled"),
        "errors": errors,
        "commands": [],
    }
    if errors:
        return report

    if args.action == "check":
        return report

    if args.action in MUTATING_ACTIONS and not args.confirm_action:
        report["status"] = "fail"
        report["errors"].append("mutating action requires --confirm-action")
        return report

    if args.action in {"deploy", "promote", "restart", "scale", "rollback", "logs", "queue-inspect", "dlq-inspect", "dlq-replay", "drain", "reconciliation", "smoke"}:
        service = require_service(manifest, args.service_name)
    else:
        service = {}

    if args.action == "status":
        if not manifest.get("services"):
            report["summary"] = "worker runtime framework is installed; no services are configured"
        elif not args.compose.exists():
            report["status"] = "fail"
            report["errors"].append(f"compose file not found: {args.compose}")
        else:
            command = compose_base(args) + ["ps", "--format", "json"]
            report["commands"].append(run_command(command))
            service_status: dict[str, Any] = {}
            missing_consumers: list[str] = []
            unverifiable_consumers: list[str] = []
            unhealthy_readiness: list[str] = []
            for configured_service in manifest["services"]:
                name = str(configured_service["name"])
                consumer_readiness = service_consumer_readiness(args, configured_service)
                port = SERVICE_HTTP_PORTS.get(str(configured_service.get("stage")))
                readiness = http_get_local("/ready", port) if port is not None else {"status": "not_configured"}
                service_status[name] = {
                    "readiness": readiness,
                    "consumer_readiness": consumer_readiness,
                }
                if readiness["status"] != "healthy":
                    unhealthy_readiness.append(name)
                if consumer_readiness["status"] == "critical":
                    missing_consumers.append(name)
                elif consumer_readiness["status"] == "unknown":
                    unverifiable_consumers.append(name)
            report["services"] = service_status
            report["missing_consumers"] = missing_consumers
            report["unverifiable_consumers"] = unverifiable_consumers
            report["unhealthy_readiness"] = unhealthy_readiness
            if missing_consumers or unverifiable_consumers or unhealthy_readiness:
                report["status"] = "fail"
            if missing_consumers or unverifiable_consumers:
                report["errors"].append("required RabbitMQ consumer readiness is not healthy")
            if unhealthy_readiness:
                report["errors"].append("required service readiness is not healthy")
    elif args.action == "logs":
        command = compose_base(args) + ["logs", "--no-color", "--tail", str(args.tail), service["name"]]
        report["commands"].append(run_command(command))
    elif args.action == "deploy":
        report["commands"].append(run_command(compose_base(args) + ["pull", service["name"]], timeout=300))
        report["commands"].append(
            run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}={service.get('replicas', 1)}", service["name"]], timeout=300)
        )
    elif args.action == "promote":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["summary"] = "Promotion requires a later backend API protected cutover state and service manifest change."
        if not args.dry_run:
            report["errors"].append("promote requires cutover_state=cutover-approved and production_writes_enabled=true")
    elif args.action == "restart":
        report["commands"].append(run_command(compose_base(args) + ["restart", service["name"]], timeout=180))
    elif args.action == "scale":
        replicas = args.replicas if args.replicas is not None else int(service.get("replicas", 1))
        max_replicas = int(manifest.get("max_replicas_per_service", 1))
        if replicas < 0 or replicas > max_replicas:
            report["status"] = "fail"
            report["errors"].append(f"replicas must be between 0 and {max_replicas}")
        else:
            report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}={replicas}", service["name"]], timeout=300))
    elif args.action == "rollback":
        if not service.get("rollback"):
            report["status"] = "fail"
            report["errors"].append("service rollback metadata is required before rollback")
        else:
            report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", service["name"]], timeout=300))
    elif args.action == "drain":
        report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}=0", service["name"]], timeout=300))
    elif args.action in {"queue-inspect", "dlq-inspect"}:
        kind = "dlq" if args.action == "dlq-inspect" else args.queue_kind
        queues = declared_queues(service, kind)
        report["queues"] = [rabbitmq_get_json(args, queue) for queue in queues]
        consumed_queues = set(declared_consumed_queues(service))
        zero_consumer_queues = [
            str(snapshot["queue"])
            for snapshot in report["queues"]
            if snapshot.get("queue") in consumed_queues
            and snapshot.get("status") == "healthy"
            and queue_consumers(snapshot) < 1
        ]
        report["zero_consumer_queues"] = zero_consumer_queues
        if zero_consumer_queues:
            report["status"] = "fail"
            report["errors"].append("required RabbitMQ queue has zero active consumers")
    elif args.action == "dlq-replay":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["queues"] = declared_queues(service, "dlq")
        report["summary"] = "DLQ replay is framework-gated; service-specific replay requires a later approved replayer image and idempotency proof."
        if not args.dry_run:
            report["errors"].append("dlq-replay currently fails closed unless --dry-run is set")
    elif args.action == "reconciliation":
        try:
            report["reconciliation"] = build_reconciliation_plan(args, service)
            report["reconciliation"]["service_invocation"] = invoke_service_reconciliation(args, service, report["reconciliation"])
        except RuntimeErrorWithReport as exc:
            report["status"] = "fail"
            report["summary"] = "Reconciliation could not complete safe stage planning or service-owned invocation."
            report["errors"].append(str(exc))
            return report
        ok, summary = service_reconciliation_outcome(report["reconciliation"]["service_invocation"])
        if ok:
            report["status"] = "dry_run" if args.dry_run else "pass"
            report["summary"] = summary
        else:
            report["status"] = "fail"
            report["summary"] = summary
            report["errors"].append(summary)
    elif args.action == "smoke":
        run_service_smoke(args, manifest, service, report)

    if report["commands"] and any(item["returncode"] != 0 for item in report["commands"]):
        report["status"] = "fail"
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage worker-uplift runtime services through fixed commands.")
    parser.add_argument("action", choices=ALL_ACTIONS)
    parser.add_argument("--manifest", type=Path, default=Path("/etc/nutsnews-worker-uplift/services.json"))
    parser.add_argument("--compose", type=Path, default=Path("/opt/nutsnews-worker-uplift/compose.yml"))
    parser.add_argument("--project", default="nutsnews-worker-uplift")
    parser.add_argument("--service-name")
    parser.add_argument("--replicas", type=int)
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--queue-kind", choices=("main", "retry", "dlq"), default="main")
    parser.add_argument("--rabbitmq-env", type=Path, default=Path("/etc/nutsnews-rabbitmq/rabbitmq.env"))
    parser.add_argument("--vhost", default="nutsnews-worker-uplift")
    parser.add_argument("--fixture-host", default="")
    parser.add_argument("--confirm-action", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = load_json(args.manifest)
        report = build_report(args, manifest)
    except RuntimeErrorWithReport as exc:
        report = {"status": "fail", "action": getattr(args, "action", "unknown"), "generated_at_utc": utc_now(), "errors": [str(exc)], **exc.report}
    except subprocess.TimeoutExpired as exc:
        report = {"status": "fail", "action": args.action, "generated_at_utc": utc_now(), "errors": [f"command timed out: {exc.cmd}"]}

    write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") in {"pass", "healthy", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
