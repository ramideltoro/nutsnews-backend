#!/usr/bin/env python3
"""Validate the fail-closed #166 final cutover-readiness authorization and decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUTHORIZATION_PATH = ROOT / "docs/worker-uplift-final-cutover-authorization.json"
DECISION_PATH = ROOT / "docs/worker-uplift-final-cutover-decision.json"
CANDIDATE_PATH = ROOT / "docs/worker-uplift-final-cutover-candidate.json"
EXECUTION_RECEIPT_PATH = ROOT / "docs/worker-uplift-final-cutover-execution-receipt.json"
WORKFLOW_PATH = ROOT / ".github/workflows/backend-worker-uplift-cutover-controls.yml"
MANAGER_PATH = ROOT / "scripts/worker_uplift_cutover_control.py"
BACKEND_CHECKS_PATH = ROOT / ".github/workflows/backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks/WORKER_UPLIFT_FINAL_CUTOVER_READINESS.md"
CONTROL_PLAN_PATH = ROOT / "docs/worker-uplift-cutover-control-plan.json"

COMMENT_URL = "https://github.com/ramideltoro/nutsnews-worker/issues/166#issuecomment-5151195619"
COMMENT_SHA256 = "738fb59be36e11889c75fe06f18797cf02a3466d7a4477d1cbc638856c24190c"
SCOPE_SHA256 = "0a6a390ddec8fd10ebad039dccb6a768726d494839b0bcfdfabff63b9e3b78eb"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^ghcr\.io/ramideltoro/[a-z0-9-]+@sha256:[0-9a-f]{64}$")
STAGES = [
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
]
STAGE_REPOSITORIES = {
    "scheduler": "ramideltoro/nutsnews-worker-feed-scheduler",
    "fetcher": "ramideltoro/nutsnews-worker-feed-fetcher",
    "canonicalizer": "ramideltoro/nutsnews-worker-article-canonicalizer",
    "enrichment": "ramideltoro/nutsnews-worker-article-enrichment",
    "approval": "ramideltoro/nutsnews-worker-article-approval",
    "translation": "ramideltoro/nutsnews-worker-article-translation",
    "persistence": "ramideltoro/nutsnews-worker-article-persistence",
    "publication": "ramideltoro/nutsnews-worker-article-publication",
}
REQUIRED_EVIDENCE = {
    "controls",
    "rollback_rehearsal",
    "runtime_status",
    "watermarks",
    "parity",
    "soak",
    "security",
    "dependency_outage",
    "empty_broker_recovery",
    "backup_restore",
    "authenticated_admin",
    "observability",
    "failover_analytics",
}
FORBIDDEN_VALUE_KEYS = {
    "secret",
    "secret_value",
    "password",
    "private_key",
    "token",
    "token_value",
    "connection_string",
    "authorization_header",
    "credential_value",
    "sql",
}
EXPECTED_SAFETY = {
    "active_ingestion_owner": "legacy_shards",
    "legacy_dispatch_enabled": True,
    "uplift_mode": "shadow",
    "production_writes_enabled": False,
    "dns_failover_unchanged": True,
}
EXPECTED_SCOPE = {
    "environment": "production-backend",
    "workflow": ".github/workflows/backend-worker-uplift-cutover-controls.yml",
    "decision_artifact": "docs/worker-uplift-final-cutover-decision.json",
    "candidate_artifact": "docs/worker-uplift-final-cutover-candidate.json",
    "allowed_execution_issue": "ramideltoro/nutsnews-worker#127",
    "allowed_operations": ["apply", "rollback"],
    "apply_confirmation_prefix": "execute-worker-uplift-cutover:",
    "rollback_confirmation_prefix": "rollback-worker-uplift-cutover:",
    "candidate_stage_count": 8,
    "required_current_owner": "legacy_shards",
    "required_current_legacy_dispatch_enabled": True,
    "required_current_uplift_mode": "shadow",
    "required_current_production_writes_enabled": False,
    "dns_failover_unchanged": True,
}
REQUIRED_FORBIDDEN_AUTHORITIES = {
    "arbitrary_sql",
    "article_or_domain_writes_outside_fixed_cutover_transition",
    "queue_publish_consume_purge_or_topology_change",
    "schema_change",
    "infrastructure_mutation",
    "dns_change",
    "failover_change",
    "cloudflare_change",
    "legacy_worker_code_change",
    "unvalidated_ingestion_owner_change",
    "unvalidated_production_write_enablement",
    "safety_gate_removal",
    "environment_protection_change",
    "secret_value_retrieval_or_recording",
    "risk_acceptance",
    "execution_outside_issue_127",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} root must be an object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def parse_utc(value: object, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an absolute ISO-8601 UTC timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        errors.append(f"{label} must be UTC")
        return None
    return parsed


def validate_value_free(value: object, errors: list[str], path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_VALUE_KEYS:
                errors.append(f"forbidden value-bearing key: {child_path}")
            validate_value_free(child, errors, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_value_free(child, errors, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if any(scheme in lowered for scheme in ("postgres://", "postgresql://", "amqp://", "amqps://")):
            errors.append(f"connection string found at {path}")
        if "authorization: bearer " in lowered:
            errors.append(f"authorization value found at {path}")


def validate_authorization(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require(contract.get("schema_version") == 1, "authorization schema_version must be 1", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews-worker#166", "authorization must track #166", errors)
    require(contract.get("execution_issue") == "ramideltoro/nutsnews-worker#127", "authorization must be limited to #127", errors)
    require(contract.get("implementation_repository") == "ramideltoro/nutsnews-backend", "authorization implementation repository mismatch", errors)
    authorization = contract.get("authorization", {})
    expected = {
        "kind": "standing_bounded_authorization",
        "owner_login": "ramideltoro",
        "owner_comment_url": COMMENT_URL,
        "owner_comment_body_sha256": COMMENT_SHA256,
        "applies_to_current_and_future_exact_candidates": True,
        "per_release_owner_approval_required": False,
        "first_run_owner_approval_required": False,
        "routine_environment_wait_owner_response_required": False,
        "exact_environment_wait_api_approval_allowed": True,
        "machine_validated_go_allowed": True,
        "risk_waiver": False,
        "survives_candidate_revisions": True,
        "fails_closed_on_scope_or_invariant_change": True,
        "scope_sha256": SCOPE_SHA256,
    }
    require(authorization == expected, "standing authorization is not exact", errors)
    require(contract.get("scope_fingerprint") == EXPECTED_SCOPE, "standing authorization scope changed", errors)
    require(sha256_json(contract.get("scope_fingerprint")) == SCOPE_SHA256, "standing authorization scope digest changed", errors)
    require(set(contract.get("forbidden_authorities", [])) == REQUIRED_FORBIDDEN_AUTHORITIES, "standing authorization exclusions changed", errors)
    drift = contract.get("drift_policy", {})
    require(drift.get("decision") == "NO-GO", "authorization drift must result in NO-GO", errors)
    require(drift.get("requires_reviewed_source_change") is True, "authorization drift must require reviewed source change", errors)
    require(drift.get("inherits_authorization_silently") is False, "scope drift must not inherit authorization", errors)
    require(drift.get("requires_new_owner_comment_when_scope_is_unchanged") is False, "unchanged scope must not require a recurring owner prompt", errors)
    require(drift.get("requires_new_owner_comment_when_scope_changes") is True, "scope changes must require new owner authorization", errors)
    validate_value_free(contract, errors)
    return errors


def validate_candidate(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require(candidate.get("schema_version") == 1, "candidate schema_version must be 1", errors)
    manifest = candidate.get("manifest")
    require(isinstance(manifest, dict), "candidate manifest must be an object", errors)
    if not isinstance(manifest, dict):
        return errors
    require(candidate.get("manifest_sha256") == sha256_json(manifest), "candidate manifest SHA-256 mismatch", errors)
    stages = manifest.get("stages", [])
    require([item.get("stage") for item in stages] == STAGES, "candidate must list the exact eight stages in order", errors)
    for item in stages:
        stage = str(item.get("stage", ""))
        require(item.get("source_repository") == STAGE_REPOSITORIES.get(stage), f"{stage} source repository mismatch", errors)
        require(bool(SHA_RE.fullmatch(str(item.get("source_commit", "")))), f"{stage} source commit must be immutable", errors)
        require(bool(IMAGE_RE.fullmatch(str(item.get("image", "")))), f"{stage} image must be digest-pinned", errors)
        require(item.get("mode") == "shadow", f"{stage} must be shadow before #127", errors)
        require(item.get("production_writes_enabled") is False, f"{stage} production writes must be false before #127", errors)
        require(bool(re.fullmatch(r"\d+\.\d+\.\d+", str(item.get("contract_version", "")))), f"{stage} contract version missing", errors)
        require(bool(re.fullmatch(r"\d+\.\d+\.\d+", str(item.get("runtime_package_version", "")))), f"{stage} runtime version missing", errors)
    packages = manifest.get("immutable_packages", [])
    require(len(packages) == 4, "candidate must pin four immutable shared-package releases", errors)
    for item in packages:
        require(item.get("repository") in {"ramideltoro/nutsnews-worker-contracts", "ramideltoro/nutsnews-worker-runtime"}, "unexpected package repository", errors)
        require(bool(SHA_RE.fullmatch(str(item.get("commit", "")))), "package commit must be immutable", errors)
        require(bool(SHA256_RE.fullmatch(str(item.get("tarball_sha256", "")))), "package tarball SHA-256 missing", errors)
        require(isinstance(item.get("publish_run"), int) and item["publish_run"] > 0, "package publish run missing", errors)
    source_hashes = manifest.get("source_control_hashes", [])
    require(bool(source_hashes), "candidate source-control hashes are required", errors)
    for item in source_hashes:
        path_value = str(item.get("path", ""))
        path = ROOT / path_value
        require(path.is_file() and ROOT in path.resolve().parents, f"candidate source path missing: {path_value}", errors)
        if path.is_file():
            require(item.get("sha256") == sha256_file(path), f"candidate source hash drifted: {path_value}", errors)
    write_policy = manifest.get("write_policy", {})
    require(write_policy == {
        "active_ingestion_owner": "legacy_shards",
        "legacy_dispatch_enabled": True,
        "uplift_mode": "shadow",
        "production_writes_enabled": False,
        "production_visibility_enabled": False,
        "single_writer_enforced": True,
        "dns_failover_unchanged": True,
    }, "candidate write policy is not the required pre-cutover state", errors)
    require(bool(SHA_RE.fullmatch(str(manifest.get("control_commit", "")))), "candidate control commit must be immutable", errors)
    require(bool(SHA_RE.fullmatch(str(manifest.get("legacy_scheduling_commit", "")))), "candidate legacy scheduling commit must be immutable", errors)
    deployment = manifest.get("deployed_state", {})
    require(deployment.get("mode") == "shadow", "deployed candidate mode must be shadow", errors)
    require(deployment.get("production_writes_enabled") is False, "deployed candidate writes must be false", errors)
    require(deployment.get("active_ingestion_owner") == "legacy_shards", "deployed candidate owner must be legacy", errors)
    require(isinstance(deployment.get("runtime_status_run"), int), "deployed runtime status run missing", errors)
    require(bool(SHA256_RE.fullmatch(str(deployment.get("artifact_digest_sha256", "")).removeprefix("sha256:"))), "deployed runtime artifact digest missing", errors)
    validate_value_free(candidate, errors)
    return errors


def validate_decision(
    decision: dict[str, Any],
    authorization: dict[str, Any],
    *,
    candidate: dict[str, Any] | None = None,
    require_go: bool = False,
) -> list[str]:
    errors: list[str] = []
    require(decision.get("schema_version") == 2, "decision schema_version must be 2", errors)
    require(decision.get("decision_id") == "worker-uplift-final-cutover-decision", "decision_id mismatch", errors)
    require(decision.get("tracking_issue") == "ramideltoro/nutsnews-worker#166", "decision must track #166", errors)
    require(decision.get("execution_issue") == "ramideltoro/nutsnews-worker#127", "decision must authorize only #127", errors)
    decision_authorization = decision.get("authorization", {})
    expected_authorization = {
        "kind": "standing_bounded_authorization",
        "contract": "docs/worker-uplift-final-cutover-authorization.json",
        "owner_login": "ramideltoro",
        "owner_comment_url": COMMENT_URL,
        "owner_comment_body_sha256": COMMENT_SHA256,
        "scope_sha256": SCOPE_SHA256,
        "recurring_owner_approval_required": False,
    }
    require(decision_authorization == expected_authorization, "decision is not bound to the exact standing authorization", errors)
    require(decision.get("safety") == EXPECTED_SAFETY, "decision pre-cutover safety state changed", errors)
    evaluated_at = parse_utc(decision.get("evaluated_at_utc"), "evaluated_at_utc", errors)
    validate_value_free(decision, errors)
    if decision.get("decision") == "NO-GO":
        require(decision.get("authorized_for_execution") is False, "NO-GO cannot authorize execution", errors)
        require(bool(decision.get("blockers")), "NO-GO must identify at least one blocker", errors)
        for field in (
            "candidate_sha256",
            "watermark_sha256",
            "rollback_deadline_utc",
            "observation_start_utc",
            "observation_end_utc",
            "control_commit",
            "execution_window",
        ):
            require(decision.get(field) is None, f"NO-GO must not freeze {field}", errors)
        if require_go:
            errors.append("exact #166 GO is absent")
        return errors
    require(decision.get("decision") == "GO", "decision must be GO or NO-GO", errors)
    require(decision.get("authorized_for_execution") is True, "GO must authorize the exact #127 execution", errors)
    require(decision.get("blockers") == [], "GO decision must have no blockers", errors)
    require(candidate is not None, "GO decision requires the exact candidate artifact", errors)
    if candidate is not None:
        errors.extend(validate_candidate(candidate))
        require(decision.get("candidate_sha256") == candidate.get("manifest_sha256"), "decision candidate SHA-256 mismatch", errors)
        require(decision.get("control_commit") == candidate.get("manifest", {}).get("control_commit"), "decision control commit mismatch", errors)
        require(decision.get("legacy_scheduling_commit") == candidate.get("manifest", {}).get("legacy_scheduling_commit"), "legacy scheduling commit mismatch", errors)
    require(bool(SHA256_RE.fullmatch(str(decision.get("candidate_sha256", "")))), "GO candidate SHA-256 missing", errors)
    require(bool(SHA256_RE.fullmatch(str(decision.get("watermark_sha256", "")))), "GO watermark SHA-256 missing", errors)
    require(bool(SHA_RE.fullmatch(str(decision.get("control_commit", "")))), "GO control commit missing", errors)
    execution_value = decision.get("execution_window")
    execution = execution_value if isinstance(execution_value, dict) else {}
    execution_start = parse_utc(execution.get("start_utc"), "execution_window.start_utc", errors)
    execution_end = parse_utc(execution.get("end_utc"), "execution_window.end_utc", errors)
    observation_start = parse_utc(decision.get("observation_start_utc"), "observation_start_utc", errors)
    observation_end = parse_utc(decision.get("observation_end_utc"), "observation_end_utc", errors)
    rollback_deadline = parse_utc(decision.get("rollback_deadline_utc"), "rollback_deadline_utc", errors)
    if execution_start and execution_end:
        require(execution_start < execution_end, "execution window must be positive", errors)
    if evaluated_at and execution_start:
        require(evaluated_at <= execution_start, "GO cannot be evaluated after its execution window starts", errors)
    if execution_end and observation_start:
        require(execution_end <= observation_start, "observation window cannot start before execution window ends", errors)
    if observation_start and observation_end:
        require(observation_end - observation_start == timedelta(hours=48), "observation window must be exactly 48 hours", errors)
    if observation_end and rollback_deadline:
        require(observation_end <= rollback_deadline, "rollback deadline cannot precede observation end", errors)
    thresholds = decision.get("thresholds", {})
    require(isinstance(thresholds.get("count"), int) and thresholds.get("count") == 17, "decision must freeze all 17 thresholds", errors)
    require(bool(SHA256_RE.fullmatch(str(thresholds.get("sha256", "")))), "threshold digest missing", errors)
    require(thresholds.get("source") == "docs/worker-uplift-cutover-control-plan.json", "threshold source mismatch", errors)
    try:
        threshold_values = load_json(CONTROL_PLAN_PATH).get("thresholds", [])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"could not load cutover threshold plan: {exc.__class__.__name__}")
    else:
        require(isinstance(threshold_values, list) and len(threshold_values) == 17, "cutover plan must contain exactly 17 thresholds", errors)
        require(thresholds.get("sha256") == sha256_json(threshold_values), "threshold digest does not match the source-controlled plan", errors)
    owners = decision.get("ownership", {})
    require(owners.get("primary_owner_login") == "ramideltoro", "primary operational owner mismatch", errors)
    require(owners.get("independent_human_backup_available") is False, "independent human backup must not be fabricated", errors)
    require(owners.get("backup_model") == "fail_closed_safe_state_controls", "owner backup model must fail closed", errors)
    evidence = decision.get("evidence", {})
    require(set(evidence) == REQUIRED_EVIDENCE, "final evidence category set is incomplete or expanded", errors)
    for evidence_id, item in evidence.items():
        require(item.get("status") == "pass", f"{evidence_id} evidence must pass", errors)
        require(item.get("value_free") is True, f"{evidence_id} evidence must be value-free", errors)
        require(bool(item.get("immutable_refs")), f"{evidence_id} immutable references missing", errors)
    scope = decision.get("decision_scope", {})
    require(scope == {
        "authorizes_issue": "ramideltoro/nutsnews-worker#127",
        "performs_cutover": False,
        "enables_production_writes_now": False,
        "changes_ingestion_owner_now": False,
        "changes_dns_or_failover": False,
    }, "GO scope expanded beyond protected #127", errors)
    return errors


def validate_execution_receipt(
    receipt: dict[str, Any],
    decision: dict[str, Any],
) -> list[str]:
    """Validate the immutable apply and completed rollback evidence."""

    errors: list[str] = []
    require(set(receipt) == {
        "schema_version",
        "receipt_id",
        "tracking_issue",
        "execution_issue",
        "status",
        "apply_authority_consumed",
        "apply_authorized",
        "rollback_authorized",
        "executed_at_utc",
        "candidate_sha256",
        "watermark_sha256",
        "rollback_deadline_utc",
        "decision_file_sha256",
        "apply_evidence",
        "applied_state",
        "rollback_progress",
        "legacy_scheduling_evidence",
        "boundary_evidence",
        "authority",
    }, "execution receipt field set drifted", errors)
    require(receipt.get("schema_version") == 1, "execution receipt schema_version must be 1", errors)
    require(receipt.get("receipt_id") == "worker-uplift-final-cutover-2026-08-01", "execution receipt id mismatch", errors)
    require(receipt.get("tracking_issue") == "ramideltoro/nutsnews-worker#166", "execution receipt must track #166", errors)
    require(receipt.get("execution_issue") == "ramideltoro/nutsnews-worker#127", "execution receipt must bind #127", errors)
    require(receipt.get("status") == "rolled_back", "execution receipt must record completed rollback", errors)
    require(receipt.get("apply_authority_consumed") is True, "apply authority must be consumed", errors)
    require(receipt.get("apply_authorized") is False, "repeat apply must be unauthorized", errors)
    require(receipt.get("rollback_authorized") is False, "completed rollback must not retain rollback authority", errors)
    require(
        receipt.get("executed_at_utc") == "2026-08-01T19:04:15Z",
        "execution receipt executed_at_utc drifted",
        errors,
    )
    require(
        receipt.get("candidate_sha256") == "71b0303705093ad398458083547a86e9e61f50458e8799ace38de4f2404859df",
        "historical execution receipt candidate drifted",
        errors,
    )
    require(
        receipt.get("watermark_sha256") == "e9b0ff2b129b76ec54589f32ade782b90aadaff54124344c2541e429d4d5d022",
        "historical execution receipt watermark drifted",
        errors,
    )
    require(
        receipt.get("rollback_deadline_utc") == "2026-08-03T21:00:00Z",
        "historical execution receipt deadline drifted",
        errors,
    )
    require(
        receipt.get("decision_file_sha256") == "2902e6e50d4614ddae4e86032e6072e86491741a3ca3394c7826b2edb811a0b2",
        "historical execution receipt decision digest drifted",
        errors,
    )
    require(receipt.get("apply_evidence") == {
        "workflow_run": 30713923790,
        "workflow_url": "https://github.com/ramideltoro/nutsnews-backend/actions/runs/30713923790",
        "head_commit": "af044a137fec686d302babfb3c9b0bf252f60cba",
        "artifact_id": 8822743880,
        "artifact_digest": "sha256:c69dc89738dff24996556b47033d35b5b6af842f18d84962be97ffa3e748c6c6",
        "conclusion": "success",
    }, "execution receipt apply evidence drifted", errors)
    require(receipt.get("applied_state") == {
        "state": "cutover_active",
        "active_ingestion_owner": "worker_uplift",
        "legacy_dispatch_enabled": False,
        "uplift_scheduler_enabled": True,
        "uplift_production_writes_enabled": True,
        "publication_write_mode": "production",
    }, "execution receipt applied state drifted", errors)

    rollback = receipt.get("rollback_progress", {})
    require(rollback.get("status") == "completed", "rollback receipt must record completion", errors)
    require(rollback.get("finalize_required") is False, "completed rollback must not require finalize", errors)
    require(
        sha256_json(rollback)
        == "bbafb62afa27b60208b5b3c68047ef7374006a9d6c3c6f277aa304b7acdd07eb",
        "completed rollback evidence drifted",
        errors,
    )
    require(
        sha256_json(receipt.get("legacy_scheduling_evidence"))
        == "94910ccb01329b752a1e46dbbafdb955632fb7c970b977f5417cf7d0e7ab3b75",
        "legacy scheduling evidence drifted",
        errors,
    )
    require(receipt.get("boundary_evidence") == {
        "legacy_in_flight_drain_proven": False,
        "legacy_to_uplift_barrier_proven": False,
        "post_transition_boundary_verification_proven": False,
        "single_writer_report_scope": "database_control_row_only",
        "disposition": "unresolved_do_not_claim_boundary_integrity_or_repeat_apply",
    }, "boundary evidence must remain explicitly unresolved", errors)
    require(receipt.get("authority") == {
        "repeat_apply": False,
        "repeat_rollback": False,
        "rollback_resume": False,
        "different_candidate": False,
        "different_watermark": False,
        "different_deadline": False,
        "runtime_1_deployment_or_qualification": False,
    }, "execution receipt authority drifted", errors)
    validate_value_free(receipt, errors)
    return errors


def require_text(errors: list[str], text: str, fragments: list[str], label: str) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} missing required enforcement: {fragment}")


def validate_repository(
    *,
    require_go: bool = False,
) -> list[str]:
    try:
        authorization = load_json(AUTHORIZATION_PATH)
        decision = load_json(DECISION_PATH)
        receipt = load_json(EXECUTION_RECEIPT_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"could not load final readiness artifacts: {exc.__class__.__name__}"]
    errors = validate_authorization(authorization)
    candidate = None
    if CANDIDATE_PATH.exists():
        try:
            candidate = load_json(CANDIDATE_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"could not load candidate artifact: {exc.__class__.__name__}")
    errors.extend(validate_decision(decision, authorization, candidate=candidate, require_go=require_go))
    errors.extend(validate_execution_receipt(receipt, decision))
    if require_go and receipt.get("apply_authority_consumed") is True:
        errors.append("exact #166 GO apply authority has already been consumed")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    manager = MANAGER_PATH.read_text(encoding="utf-8")
    checks = BACKEND_CHECKS_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8") if RUNBOOK_PATH.exists() else ""
    require_text(errors, workflow, [
        "scripts/validate_worker_uplift_final_cutover_readiness.py",
        "--require-go",
        "environment: production-backend",
        "execute-worker-uplift-cutover:$CANDIDATE",
        "rollback-worker-uplift-cutover:$WATERMARK",
    ], "cutover workflow")
    require_text(errors, manager, [
        "standing_bounded_authorization",
        COMMENT_URL,
        SCOPE_SHA256,
        "recurring_owner_approval_required",
        "execution_window",
    ], "cutover manager")
    require_text(errors, checks, [
        "scripts/validate_worker_uplift_final_cutover_readiness.py",
        "tests.test_worker_uplift_final_cutover_readiness",
    ], "Backend Checks")
    require_text(errors, runbook, [
        COMMENT_URL,
        "No recurring owner prompt",
        "fail closed",
        "#127",
    ], "final readiness runbook")
    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-go", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    errors = validate_repository(require_go=args.require_go)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    decision = load_json(DECISION_PATH)
    receipt = load_json(EXECUTION_RECEIPT_PATH)
    print(
        "worker-uplift final cutover readiness valid; "
        f"current_decision={decision['decision']} "
        "historical_execution_decision=GO "
        f"execution_status={receipt['status']} "
        f"apply_authorized={str(receipt['apply_authorized']).lower()} "
        f"rollback_authorized={str(receipt['rollback_authorized']).lower()} "
        f"scope_sha256={SCOPE_SHA256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
