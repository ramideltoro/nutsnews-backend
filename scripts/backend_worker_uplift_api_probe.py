#!/usr/bin/env python3
"""Probe scoped worker-uplift Backend API final-shadow writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://backend.nutsnews.com/api/worker/db"
DEFAULT_TIMEOUT_SECONDS = 15
OPERATION = "uplift-record-shadow-aggregate"
TARGET_LANGUAGES = ["fr", "ja", "de-CH", "de", "el"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def deterministic_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(parts)))


def safe_text(value: Any, limit: int = 512) -> str:
    text = str(value or "")
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def parse_json_bytes(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {"error": "non-json backend api response"}


def request_json(base_url: str, token: str, operation: str, body: dict[str, Any]) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}/{operation}"
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-nutsnews-db-client": "backend-worker-uplift-api-probe",
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return response.status, parse_json_bytes(response.read())
    except HTTPError as error:
        return error.code, parse_json_bytes(error.read())
    except URLError as error:
        raise RuntimeError(f"request failed for {operation}: {safe_text(error.reason)}") from error


def build_shadow_aggregate(probe_id: str) -> dict[str, Any]:
    article_id = f"worker-api-probe-{probe_id}"
    base_aggregate: dict[str, Any] = {
        "articleIdentityHash": article_id,
        "canonicalUrlHash": f"canonical-{article_id}",
        "originalUrlHash": f"original-{article_id}",
        "aggregateVersion": 1,
        "sourceFeedUrl": "https://feeds.example.test/worker-uplift/api-probe.xml",
        "titleRef": f"backend://worker-uplift/probe/{article_id}/title",
        "imageUrlRef": f"backend://worker-uplift/probe/{article_id}/image",
        "category": "worker-api-probe",
        "positivityScore": 73,
        "approvalVersion": 1,
        "translationLanguages": TARGET_LANGUAGES,
        "publicationStatus": "ready",
        "payloadRef": f"backend://worker-uplift/final-shadow/{article_id}/v1",
        "diagnosticMetadata": {
            "safeMetadataOnly": True,
            "probe": "worker-api-final-shadow",
            "requiredLanguageCount": len(TARGET_LANGUAGES),
            "acceptedLanguageCount": len(TARGET_LANGUAGES),
            "missingLanguageCount": 0,
        },
    }
    return {
        **base_aggregate,
        "payloadDigest": sha256_json(base_aggregate),
    }


def build_command(probe_id: str, *, github_run_id: str = "", occurred_at: str | None = None) -> dict[str, Any]:
    article_id = f"worker-api-probe-{probe_id}"
    run_id = github_run_id or "local"
    observed_at = occurred_at or utc_now()
    aggregate = build_shadow_aggregate(probe_id)
    aggregate["diagnosticMetadata"] = {
        **aggregate["diagnosticMetadata"],
        "githubRunId": run_id,
        "observedAt": observed_at,
        "articleIdentityHash": article_id,
    }
    aggregate["payloadDigest"] = sha256_json({
        key: value
        for key, value in aggregate.items()
        if key != "payloadDigest"
    })
    return {
        "operation": OPERATION,
        "providerMode": "backend_postgres_shadow",
        "idempotencyKey": f"probe:worker-api-final-shadow:{probe_id}",
        "messageId": deterministic_uuid("worker-api-probe", probe_id, "message", run_id),
        "correlationId": deterministic_uuid("worker-api-probe", probe_id, "correlation"),
        "pipelineRunId": deterministic_uuid("worker-api-probe", probe_id, "pipeline"),
        "stageExecutionId": deterministic_uuid("worker-api-probe", probe_id, "stage"),
        "sourceMessageId": deterministic_uuid("worker-api-probe", probe_id, "source"),
        "actorService": "worker-uplift-persistence",
        "schemaVersion": 1,
        "operationVersion": 1,
        "expectedArticleVersion": 1,
        "shadowAggregate": aggregate,
    }


def response_snapshot(status_code: int, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"http_status": status_code, "payload_type": type(payload).__name__}
    snapshot: dict[str, Any] = {"http_status": status_code}
    for key in (
        "ok",
        "operation",
        "mode",
        "recorded",
        "duplicate",
        "productionSideEffect",
        "idempotencyKey",
        "errorClass",
        "pgcode",
        "sqlstate",
        "safeMetadataOnly",
    ):
        if key in payload:
            snapshot[key] = payload[key]
    if "error" in payload:
        snapshot["error"] = safe_text(payload["error"])
    return snapshot


def evaluate_first_response(status_code: int, payload: Any) -> tuple[str, list[str]]:
    errors: list[str] = []
    if status_code != 200:
        errors.append(f"first_call_http_{status_code}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        errors.append("first_call_not_ok")
    if isinstance(payload, dict) and payload.get("productionSideEffect") is not False:
        errors.append("first_call_production_side_effect_not_false")
    return ("pass" if not errors else "fail", errors)


def evaluate_duplicate_response(status_code: int, payload: Any) -> tuple[str, list[str]]:
    errors: list[str] = []
    if status_code != 200:
        errors.append(f"duplicate_call_http_{status_code}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        errors.append("duplicate_call_not_ok")
    if not isinstance(payload, dict) or payload.get("duplicate") is not True:
        errors.append("duplicate_receipt_not_observed")
    if isinstance(payload, dict) and payload.get("productionSideEffect") is not False:
        errors.append("duplicate_call_production_side_effect_not_false")
    return ("pass" if not errors else "fail", errors)


def write_report(report: dict[str, Any], output: str) -> None:
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(f"{encoded}\n", encoding="utf-8")
    print(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--base-url-env", default="NUTSNEWS_BACKEND_WORKER_API_BASE_URL")
    parser.add_argument("--token-env", default="NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN")
    parser.add_argument("--output", default="")
    parser.add_argument("--probe-id", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    base_url = env_value(args.base_url_env) or DEFAULT_BASE_URL
    token = env_value(args.token_env)
    probe_id = args.probe_id or uuid.uuid4().hex[:12]
    report: dict[str, Any] = {
        "status": "pass",
        "checked_at_utc": utc_now(),
        "safe_metadata_only": True,
        "operation": OPERATION,
        "base_url": base_url,
        "probe_id": probe_id,
        "article_identity_hash": f"worker-api-probe-{probe_id}",
        "checks": [],
        "errors": [],
    }

    if args.offline:
        report["status"] = "skipped"
        report["reason"] = "offline mode"
        report["checks"].append({
            "name": "worker_api_final_shadow_probe",
            "status": "skipped_with_reason",
            "reason": report["reason"],
        })
        write_report(report, args.output)
        return 0

    if not token:
        report["status"] = "fail"
        report["reason"] = f"missing token env {args.token_env}"
        report["errors"].append("missing_worker_uplift_persistence_token")
        report["checks"].append({
            "name": "worker_api_final_shadow_probe",
            "status": "fail",
            "reason": report["reason"],
        })
        write_report(report, args.output)
        return 1 if args.enforce else 0

    command = build_command(probe_id, github_run_id=env_value("GITHUB_RUN_ID"))
    try:
        first_status, first_payload = request_json(base_url, token, OPERATION, command)
        first_check_status, first_errors = evaluate_first_response(first_status, first_payload)
        report["checks"].append({
            "name": "record_shadow_aggregate",
            "status": first_check_status,
            **response_snapshot(first_status, first_payload),
        })
        report["errors"].extend(first_errors)

        if first_check_status == "pass":
            duplicate_status, duplicate_payload = request_json(base_url, token, OPERATION, command)
            duplicate_check_status, duplicate_errors = evaluate_duplicate_response(duplicate_status, duplicate_payload)
            report["checks"].append({
                "name": "idempotent_receipt",
                "status": duplicate_check_status,
                **response_snapshot(duplicate_status, duplicate_payload),
            })
            report["errors"].extend(duplicate_errors)
        else:
            report["checks"].append({
                "name": "idempotent_receipt",
                "status": "skipped_with_reason",
                "reason": "initial final-shadow API call failed",
            })
    except RuntimeError as error:
        report["checks"].append({
            "name": "record_shadow_aggregate",
            "status": "fail",
            "error": safe_text(error),
        })
        report["errors"].append("worker_api_request_failed")

    if report["errors"]:
        report["status"] = "fail"

    write_report(report, args.output)
    return 1 if args.enforce and report["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
