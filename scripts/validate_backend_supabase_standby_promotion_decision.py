#!/usr/bin/env python3
"""Validate Supabase standby promotion decision guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-promotion-decision.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_promotion_decision.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_promotion_decision.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-promotion-decision.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_PROMOTION_DECISION.md"
README_PATH = ROOT / "README.md"
PROVIDER_SWITCH_SCRIPT_PATH = ROOT / "scripts" / "backend_database_provider_switch_plan.py"
PROVIDER_SWITCH_TEST_PATH = ROOT / "tests" / "test_backend_database_provider_switch.py"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read(path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object in {path}")
    return data


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def require_token(token: str, text: str, label: str, errors: list[str]) -> None:
    require(token in text, f"{label} missing token: {token}", errors)


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    script = read(SCRIPT_PATH)
    tests = read(TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    provider_switch_script = read(PROVIDER_SWITCH_SCRIPT_PATH)
    provider_switch_tests = read(PROVIDER_SWITCH_TEST_PATH)
    errors: list[str] = []

    require(contract.get("schema_version") == 1, "schema_version must be 1", errors)
    require(
        contract.get("gate_id") == "backend-supabase-standby-promotion-decision",
        "contract gate_id is incorrect",
        errors,
    )
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#528", "contract must point to #528", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)

    source = contract.get("source", {})
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    decision = contract.get("decision", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)
    require(
        safety.get("backend_postgresql_remains_primary_until_approved_failover") is True,
        "backend primary safety flag missing",
        errors,
    )
    require(safety.get("app_worker_writes_to_supabase_before_failover") is False, "app/worker Supabase writes must stay blocked", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(decision.get("go_value") == "GO", "GO value must be GO", errors)
    require(decision.get("no_go_value") == "NO-GO", "NO-GO value must be NO-GO", errors)
    require(decision.get("single_use") is True, "decision must be single-use", errors)
    require(decision.get("ttl_seconds") == 300, "decision TTL must be 300 seconds", errors)
    require(decision.get("provider_switch_performed_by_this_workflow") is False, "decision workflow must not switch providers", errors)

    required_gate_ids = {item.get("id") for item in contract.get("required_gates", []) if isinstance(item, dict)}
    require(
        required_gate_ids == {"lag", "parity", "schema", "sequence", "writer_pause", "split_brain_fence"},
        "contract must list exactly six required gates",
        errors,
    )
    failure_policy = contract.get("failure_policy", {})
    for policy in (
        "missing_evidence",
        "stale_evidence",
        "malformed_evidence",
        "duplicate_evidence",
        "replayed_decision",
        "mismatched_attempt",
        "mismatched_target",
        "mismatched_revision",
        "mismatched_epoch",
        "unavailable_evidence",
        "failing_gate",
    ):
        require(failure_policy.get(policy) == "NO-GO", f"failure policy must be NO-GO for {policy}", errors)

    for token in (
        "GATE_NAME = \"supabase_standby_promotion_decision\"",
        "ISSUE = \"ramideltoro/nutsnews#528\"",
        "EXPECTED_SOURCE_LABEL = \"backend_postgres_primary\"",
        "EXPECTED_TARGET_LABEL = \"existing_production_supabase_standby\"",
        "REQUIRED_GATES",
        "standby_binding_fingerprint",
        "not_all_gates_passed",
        "decision_already_consumed",
        "duplicate_gate_evidence",
        "source_binding_fingerprint",
        "target_binding_fingerprint",
        "provider_switch_performed",
        "--enforce",
    ):
        require_token(token, script, "promotion decision script", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "nutsnews-standby",
    ):
        require(forbidden not in script, f"decision evaluator must not contain provisioning/secret marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "candidate_application_revision:",
        "fence_epoch:",
        "lag_gate_run_id:",
        "split_brain_fence_gate_run_id:",
        "confirmation:",
        "evaluate-standby-promotion-decision",
        "environment: production-backend",
        "gh run download",
        "backend-supabase-standby-promotion-decision",
        "python3 scripts/validate_backend_supabase_standby_promotion_decision.py",
        "python3 -m unittest tests.test_backend_supabase_standby_promotion_decision",
        "python3 -m unittest tests.test_backend_database_provider_switch",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "promotion decision workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_happy_path_returns_go",
        "test_each_failing_gate_produces_no_go",
        "test_missing_gate_evidence_fails_closed",
        "test_malformed_gate_evidence_fails_closed",
        "test_stale_gate_evidence_fails_closed",
        "test_mismatched_attempt_fails_closed",
        "test_mismatched_target_binding_fails_closed",
        "test_mismatched_revision_fails_closed",
        "test_mismatched_epoch_fails_closed",
        "test_duplicate_gate_evidence_fails_closed",
        "test_go_cannot_be_reused_after_consumption",
        "test_go_cannot_be_reused_after_expiry",
        "test_enforce_returns_nonzero_on_no_go",
        "test_artifact_is_safe_metadata_only",
    ):
        require_token(token, tests, "promotion decision tests", errors)

    for token in (
        "missing_supabase_standby_promotion_decision",
        "supabase_standby_promotion_decision_not_go",
        "supabase_standby_promotion_decision_expired",
        "supabase_standby_promotion_decision_already_consumed",
    ):
        require_token(token, provider_switch_script, "provider switch plan", errors)
    for token in (
        "test_production_supabase_switch_without_go_decision_is_blocked",
        "test_production_switch_with_expired_go_decision_is_blocked",
    ):
        require_token(token, provider_switch_tests, "provider switch tests", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_promotion_decision.py",
        "python3 -m unittest tests.test_backend_supabase_standby_promotion_decision",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "Issue #528",
        "GO",
        "NO-GO",
        "single-use",
        "safe metadata only",
        "#502",
    ):
        require_token(token, runbook, "promotion decision runbook", errors)
    require_token("SUPABASE_STANDBY_PROMOTION_DECISION.md", readme, "README", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby promotion decision guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
