#!/usr/bin/env python3
"""Validate the non-mutating #165 worker-uplift cutover-control plan."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN_PATH = ROOT / "docs" / "worker-uplift-cutover-control-plan.json"
BACKEND_CHECKS_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
PLACEHOLDER_RE = re.compile(
    r"\b(?:tbd|todo|placeholder|to be decided|relative[- ]only)\b",
    re.IGNORECASE,
)
FORBIDDEN_VALUE_KEYS = {
    "secret_value",
    "credential_value",
    "token_value",
    "password",
    "private_key",
    "connection_string",
    "authorization_header",
}
SOURCE_REPOSITORIES = {
    "ramideltoro/nutsnews-backend",
    "ramideltoro/nutsnews-worker",
    "ramideltoro/nutsnews-docs",
    "ramideltoro/nutsnews-infra",
}
WATERMARK_SOURCES = {
    "legacy_dispatch_fence",
    "rabbitmq_drain",
    "stage_reconciliation",
    "stage_outbox",
    "final_effect_boundary",
    "exact_candidate",
}
THRESHOLD_IDS = {
    "single_writer",
    "service_health",
    "consumer_count",
    "queue_depth",
    "queue_age",
    "dlq_growth",
    "reconciliation",
    "boundary_integrity",
    "api_commands",
    "availability",
    "latency",
    "feed_freshness",
    "publisher_confirms",
    "host_headroom",
    "qwen_and_cost",
    "observability",
    "dns_failover",
}
OWNERSHIP_REPOSITORIES = {
    "ingestion": "ramideltoro/nutsnews-worker",
    "scheduling": "ramideltoro/nutsnews-worker",
    "write_enablement": "ramideltoro/nutsnews-backend",
    "rabbitmq": "ramideltoro/nutsnews-backend",
    "database_api": "ramideltoro/nutsnews-backend",
    "qwen": "ramideltoro/nutsnews-backend",
    "observability": "ramideltoro/nutsnews-infra",
    "cloudflare_failover": "ramideltoro/nutsnews-infra",
    "incident_command": "ramideltoro/nutsnews-worker",
    "rollback": "ramideltoro/nutsnews-backend",
    "final_readiness": "ramideltoro/nutsnews-worker",
}
FINAL_GATE_FIELDS = {
    "planned_execution_window.start_utc",
    "planned_execution_window.end_utc",
    "cutover_watermark complete value-source snapshot and SHA-256",
    "rollback.planned_absolute_deadline_utc",
    "observation_window absolute start and end",
    "threshold evaluation artifact IDs and digests",
    "ownership roster availability",
    "eight image digests and source commits",
    "package and contract versions",
    "runtime configuration, topology, identity, and write-policy hashes",
    "deployed owner and write-policy state",
    "rollback rehearsal run and artifact",
    "DNS failover controller and analytics disposition evidence",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing worker-uplift cutover-control plan: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def parse_utc(value: object, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an absolute ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        errors.append(f"{label} must be an absolute UTC timestamp")
        return None
    return parsed


def duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated


def validate_value_free(value: object, errors: list[str], path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_VALUE_KEYS:
                errors.append(f"plan contains forbidden value-bearing key: {child_path}")
            validate_value_free(child, errors, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_value_free(child, errors, f"{path}[{index}]")
    elif isinstance(value, str):
        if PLACEHOLDER_RE.search(value):
            errors.append(f"plan contains placeholder text at {path}")
        lowered = value.lower()
        if any(scheme in lowered for scheme in ("postgres://", "postgresql://", "amqp://", "amqps://")):
            errors.append(f"plan contains a connection string at {path}")
        if "authorization: bearer " in lowered:
            errors.append(f"plan contains an authorization value at {path}")


def validate_plan(
    plan: dict[str, Any],
    *,
    backend_checks_text: str | None = None,
) -> list[str]:
    errors: list[str] = []

    if plan.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if plan.get("plan_id") != "worker-uplift-cutover-control-plan":
        errors.append("plan_id must identify the worker-uplift cutover-control plan")
    if plan.get("tracking_issue") != "ramideltoro/nutsnews-worker#165":
        errors.append("tracking_issue must identify nutsnews-worker#165")
    if plan.get("implementation_repository") != "ramideltoro/nutsnews-backend":
        errors.append("implementation_repository must be nutsnews-backend")
    captured_at = parse_utc(plan.get("captured_at_utc"), "captured_at_utc", errors)
    if captured_at is not None and captured_at > datetime.now(timezone.utc):
        errors.append("captured_at_utc cannot be future-dated")
    if plan.get("status") != "complete_non_mutating_plan_no_cutover_authority":
        errors.append("status must record a complete non-mutating plan with no cutover authority")

    source_heads = plan.get("source_heads", [])
    repositories = [str(item.get("repository", "")) for item in source_heads]
    if set(repositories) != SOURCE_REPOSITORIES or duplicates(repositories):
        errors.append("source_heads must contain each required repository exactly once")
    for item in source_heads:
        if not SHA_RE.fullmatch(str(item.get("commit", ""))):
            errors.append(f"{item.get('repository')} source head must be a full Git SHA")
    contracts = plan.get("source_contracts", [])
    for required in (
        "docs/worker-uplift-architecture-adr.json",
        "docs/worker-uplift-production-readiness-decision.json",
        "docs/worker-uplift-shadow-soak-capacity-report.json",
        "docs/worker-uplift-legacy-to-shadow-parity-report.json",
        "docs/worker-uplift-rabbitmq-capacity-security-decision.json",
    ):
        if required not in contracts:
            errors.append(f"source contract missing: {required}")

    authority = plan.get("authority_boundary", {})
    for field in (
        "issue_125",
        "issue_150",
        "issue_126",
        "issue_166",
        "issue_127",
        "issue_128",
    ):
        if not authority.get(field):
            errors.append(f"authority_boundary.{field} must be explicit")
    for field in (
        "this_plan_authorizes_cutover",
        "this_plan_authorizes_production_writes",
        "this_plan_authorizes_ingestion_owner_change",
        "this_plan_authorizes_dns_or_failover_change",
        "this_plan_implements_controls",
    ):
        if authority.get(field) is not False:
            errors.append(f"authority_boundary.{field} must be false")
    if "#150 only" not in str(authority.get("issue_125", "")):
        errors.append("#125 authority must be limited to beginning #150 only")
    if "#127" not in str(authority.get("issue_166", "")):
        errors.append("#166 must be the final authority gate for #127")

    safety = plan.get("current_safety_state", {})
    if safety.get("active_production_ingestion_owner") != "legacy_shards":
        errors.append("legacy_shards must remain the current production ingestion owner")
    if safety.get("active_production_ingestion_repository") != "ramideltoro/nutsnews-worker":
        errors.append("current production ingestion repository must remain nutsnews-worker")
    if not SHA_RE.fullmatch(str(safety.get("active_production_ingestion_commit", ""))):
        errors.append("current production ingestion commit must be a full Git SHA")
    if safety.get("legacy_ingestion_dispatch_enabled") is not True:
        errors.append("legacy ingestion dispatch must remain enabled")
    if safety.get("uplift_mode") != "shadow":
        errors.append("uplift mode must remain shadow")
    if safety.get("cloudflare_dns_failover_controller_unchanged") is not True:
        errors.append("Cloudflare DNS failover must remain unchanged")
    for field in (
        "uplift_production_writes_enabled",
        "uplift_production_visibility_enabled",
        "legacy_worker_modified_by_plan",
        "production_infrastructure_modified_by_plan",
        "production_data_modified_by_plan",
        "secret_values_recorded",
    ):
        if safety.get(field) is not False:
            errors.append(f"current_safety_state.{field} must be false")

    window = plan.get("planned_execution_window", {})
    window_start = parse_utc(window.get("start_utc"), "planned_execution_window.start_utc", errors)
    window_end = parse_utc(window.get("end_utc"), "planned_execution_window.end_utc", errors)
    if window_start is not None and window_end is not None and window_start >= window_end:
        errors.append("planned execution window end must follow its start")
    if window.get("status") != "planning_reference_not_execution_authority":
        errors.append("planned execution window must not imply execution authority")
    if window.get("must_be_refreshed_by_issue_166_if_execution_is_outside_window") is not True:
        errors.append("#166 must refresh a stale planned execution window")

    watermark = plan.get("cutover_watermark", {})
    if watermark.get("semantics_version") != "worker-uplift-handoff-v1":
        errors.append("cutover watermark semantics version is invalid")
    if watermark.get("status") != "semantics_defined_value_not_captured":
        errors.append("#165 must define watermark semantics without claiming capture")
    if watermark.get("capture_issue") != "ramideltoro/nutsnews-worker#166":
        errors.append("#166 must capture the final watermark")
    if watermark.get("execution_issue") != "ramideltoro/nutsnews-worker#127":
        errors.append("#127 must remain the watermark execution issue")
    if watermark.get("capture_owner_login") != "ramideltoro":
        errors.append("watermark must name the authorized owner")
    if "sha256" not in str(watermark.get("watermark_id_rule", "")).lower():
        errors.append("watermark ID must be a SHA-256 of canonical evidence")
    value_sources = watermark.get("value_sources", [])
    source_ids = [str(item.get("id", "")) for item in value_sources]
    if set(source_ids) != WATERMARK_SOURCES or duplicates(source_ids):
        errors.append("cutover watermark must contain every value source exactly once")
    for item in value_sources:
        if not item.get("source") or not item.get("required_boundary"):
            errors.append(f"watermark source {item.get('id')} must define source and boundary")
        fields = item.get("required_fields", [])
        if not isinstance(fields, list) or not fields or any(not str(field) for field in fields):
            errors.append(f"watermark source {item.get('id')} must define required fields")
    if len(watermark.get("invalidation_triggers", [])) < 5:
        errors.append("watermark must define complete invalidation triggers")

    rollback = plan.get("rollback", {})
    deadline = parse_utc(
        rollback.get("planned_absolute_deadline_utc"),
        "rollback.planned_absolute_deadline_utc",
        errors,
    )
    calculation = rollback.get("deadline_calculation", {})
    reference = parse_utc(
        calculation.get("reference_timestamp_utc"),
        "rollback.deadline_calculation.reference_timestamp_utc",
        errors,
    )
    if calculation.get("rollback_eligibility_hours") != 48:
        errors.append("rollback eligibility must be exactly 48 hours")
    if reference is not None and deadline != reference + timedelta(hours=48):
        errors.append("absolute rollback deadline must equal the reference plus 48 hours")
    if window_end is not None and reference != window_end:
        errors.append("rollback deadline reference must equal the planned window end")
    if not calculation.get("calculation"):
        errors.append("rollback deadline must record its calculation")
    if rollback.get("target_recovery_time_seconds") != 900:
        errors.append("rollback recovery target must be 900 seconds")
    if rollback.get("final_gate_must_refresh_absolute_deadline") is not True:
        errors.append("#166 must refresh the absolute rollback deadline")
    if len(rollback.get("eligibility_requirements", [])) < 6:
        errors.append("rollback must define complete eligibility requirements")
    if len(rollback.get("early_invalidation_triggers", [])) < 5:
        errors.append("rollback must define early invalidation triggers")
    if len(rollback.get("action_order", [])) < 7:
        errors.append("rollback must define a complete ordered recovery sequence")
    if "forward recovery" not in str(rollback.get("after_invalidation_policy", "")):
        errors.append("ineligible rollback must default to forward recovery")

    observation = plan.get("observation_window", {})
    if observation.get("duration_hours") != 48:
        errors.append("observation window must be exactly 48 hours")
    if observation.get("checkpoint_interval_seconds") != 300:
        errors.append("observation checkpoints must occur every 300 seconds")
    if observation.get("final_gate_must_freeze_absolute_start_and_end") is not True:
        errors.append("#166 must freeze absolute observation start and end timestamps")
    if observation.get("legacy_dispatch_state") != "disabled_but_reversible":
        errors.append("legacy dispatch must remain reversible during observation")
    if observation.get("legacy_assets_state") != "deployed_or_immediately_redeployable_standby":
        errors.append("legacy assets must remain available during observation")
    if observation.get("decommission_before_window_success") is not False:
        errors.append("legacy ingestion cannot be decommissioned before observation succeeds")
    if observation.get("decommission_issue") != "ramideltoro/nutsnews-worker#128":
        errors.append("#128 must own post-observation decommissioning")

    thresholds = plan.get("thresholds", [])
    threshold_ids = [str(item.get("id", "")) for item in thresholds]
    if set(threshold_ids) != THRESHOLD_IDS or duplicates(threshold_ids):
        errors.append("threshold set is incomplete or duplicated")
    for item in thresholds:
        if not item.get("measure") or not item.get("success") or not item.get("abort"):
            errors.append(f"threshold {item.get('id')} must define measure, success, and abort")
        if item.get("sustain_seconds") not in {0, 600}:
            errors.append(f"threshold {item.get('id')} has an invalid sustain interval")
    threshold_map = {item.get("id"): item for item in thresholds}
    if "exactly one owner" not in str(threshold_map.get("single_writer", {}).get("success", "")):
        errors.append("single-writer threshold must require exactly one owner")
    if "growth = 0" not in str(threshold_map.get("dlq_growth", {}).get("success", "")):
        errors.append("DLQ threshold must require zero growth")
    if "lost = 0" not in str(threshold_map.get("boundary_integrity", {}).get("success", "")):
        errors.append("boundary integrity must require zero lost effects")

    ownership = plan.get("ownership", {})
    if ownership.get("authorized_owner_logins") != ["ramideltoro"]:
        errors.append("ownership must name the only current authorized owner")
    if ownership.get("single_human_operator") is not True:
        errors.append("ownership must accurately record the single-human operator model")
    if ownership.get("independent_human_backup_available") is not False:
        errors.append("ownership must not fabricate an independent human backup")
    unavailable_policy = str(ownership.get("owner_unavailable_policy", ""))
    if "standing authorization" not in unavailable_policy or "fails closed" not in unavailable_policy:
        errors.append("owner unavailability policy must use standing authorization and fail closed")
    domains = ownership.get("domains", [])
    domain_ids = [str(item.get("id", "")) for item in domains]
    if set(domain_ids) != set(OWNERSHIP_REPOSITORIES) or duplicates(domain_ids):
        errors.append("ownership domain set is incomplete or duplicated")
    for item in domains:
        domain_id = str(item.get("id", ""))
        owner = str(item.get("primary_owner_login", ""))
        if not LOGIN_RE.fullmatch(owner) or owner != "ramideltoro":
            errors.append(f"{domain_id} must name the authorized GitHub owner")
        if "backup_owner_login" not in item or item.get("backup_owner_login") is not None:
            errors.append(
                f"{domain_id} must explicitly record that no human backup owner exists"
            )
        if item.get("implementation_repository") != OWNERSHIP_REPOSITORIES.get(domain_id):
            errors.append(f"{domain_id} implementation repository is incorrect")
        backup = str(item.get("backup_control_id", ""))
        if not backup or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", backup):
            errors.append(f"{domain_id} must name a concrete fail-closed backup control")

    handoff = plan.get("single_writer_handoff", [])
    if [item.get("sequence") for item in handoff] != [1, 2, 3, 4, 5]:
        errors.append("single-writer handoff must use the exact five-step sequence")
    expected_phases = [
        "preflight",
        "fence_and_drain",
        "freeze_watermark",
        "atomic_owner_and_write_switch",
        "start_uplift_scheduler_and_verify",
    ]
    if [item.get("phase") for item in handoff] != expected_phases:
        errors.append("single-writer handoff phases are incomplete or out of order")
    for item in handoff[:3]:
        if item.get("active_owner") != "legacy_shards" or item.get(
            "uplift_production_writes"
        ) is not False:
            errors.append("pre-switch handoff phases must retain legacy owner and writes false")
    for item in handoff[3:]:
        if item.get("active_owner") != "worker_uplift" or item.get(
            "uplift_production_writes"
        ) is not True:
            errors.append("post-switch handoff phases must record the future uplift owner state")
    if any(not item.get("required_evidence") for item in handoff):
        errors.append("every handoff phase must name required evidence")

    custody = plan.get("evidence_custody", {})
    if custody.get("source_control_owner") != "ramideltoro/nutsnews-backend":
        errors.append("backend must own the source-controlled evidence plan")
    if custody.get("tracking_owner") != "ramideltoro/nutsnews-worker":
        errors.append("nutsnews-worker must own tracking evidence")
    if custody.get("minimum_retention_days_after_issue_128_closure") != 90:
        errors.append("evidence retention must be at least 90 days after #128")
    if custody.get("download_before_platform_expiry") is not True:
        errors.append("evidence must be downloaded before platform expiry")
    if custody.get("issue_comments_record_digests_not_secret_values") is not True:
        errors.append("issue comments must record digests without secret values")
    if len(custody.get("required_identifiers", [])) < 10:
        errors.append("evidence custody identifiers are incomplete")

    final_gate = plan.get("final_gate_refresh", {})
    if final_gate.get("issue") != "ramideltoro/nutsnews-worker#166":
        errors.append("#166 must own the final refresh")
    if set(final_gate.get("required_exact_fields", [])) != FINAL_GATE_FIELDS:
        errors.append("#166 final refresh field set is incomplete")
    if final_gate.get("stale_or_missing_field_decision") != "NO-GO":
        errors.append("stale or missing final evidence must be NO-GO")
    if final_gate.get("standing_authorization_contract") != "docs/worker-uplift-final-cutover-authorization.json":
        errors.append("#166 must bind the standing authorization contract")
    if final_gate.get("standing_authorization_comment") != "https://github.com/ramideltoro/nutsnews-worker/issues/166#issuecomment-5151195619":
        errors.append("#166 standing authorization comment mismatch")
    if final_gate.get("recurring_named_approver_required") is not False:
        errors.append("exact #166 operations must not require a recurring owner prompt")
    if final_gate.get("advisory_metadata_is_authorization") is not False:
        errors.append("advisory metadata cannot replace the standing authorization contract")
    if final_gate.get("machine_validator_may_issue_go_under_standing_authorization") is not True:
        errors.append("the exact machine validator must be able to issue GO under standing authorization")
    if final_gate.get("scope_or_invariant_drift_decision") != "NO-GO":
        errors.append("scope or invariant drift must be NO-GO")
    if final_gate.get("go_authority") != "authorize protected #127 execution only":
        errors.append("#166 GO authority must be limited to protected #127 execution")

    validation = plan.get("validation", {})
    command = "python3 scripts/validate_worker_uplift_cutover_control_plan.py"
    focused = "python3 -m unittest tests.test_worker_uplift_cutover_control_plan"
    if validation.get("validator") != command:
        errors.append("validation must name the cutover-control plan validator")
    if validation.get("focused_tests") != focused:
        errors.append("validation must name the focused cutover-control plan tests")
    if validation.get("backend_checks_enforced") is not True:
        errors.append("Backend Checks enforcement must be enabled")
    if backend_checks_text is None:
        try:
            backend_checks_text = BACKEND_CHECKS_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            backend_checks_text = ""
    if f"run: {command}" not in backend_checks_text:
        errors.append("Backend Checks must run the cutover-control plan validator")
    if f"run: {focused}" not in backend_checks_text:
        errors.append("Backend Checks must run the cutover-control plan tests")

    validate_value_free(plan, errors)
    return errors


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PLAN_PATH
    errors = validate_plan(load_json(path))
    if errors:
        print("Worker-uplift cutover-control plan validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Worker-uplift cutover-control plan is valid and non-mutating.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
