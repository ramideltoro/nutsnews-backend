#!/usr/bin/env python3
"""Validate Supabase standby failover workflow guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-failover-workflow.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_failover_plan.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_failover.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-failover.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_FAILOVER.md"
README_PATH = ROOT / "README.md"


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
    errors: list[str] = []

    require(contract.get("schema_version") == 1, "schema_version must be 1", errors)
    require(contract.get("workflow_id") == "backend-supabase-standby-failover", "workflow_id is incorrect", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#502", "contract must point to #502", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    for dependency in (
        "docs/backend-supabase-standby-promotion-decision.json",
        "docs/backend-supabase-standby-recovery-boundaries.json",
        "docs/backend-database-provider-switch.json",
    ):
        require(dependency in contract.get("depends_on", []), f"missing dependency {dependency}", errors)

    target = contract.get("target_after_failover", {})
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)

    decision = contract.get("required_decision", {})
    require(decision.get("issue") == "ramideltoro/nutsnews#528", "failover must consume #528", errors)
    require(decision.get("accepted_decision") == "GO", "failover must require GO", errors)
    require(decision.get("single_use") is True, "failover decision must be single-use", errors)
    require(decision.get("consumed_by_this_workflow") is True, "failover workflow must consume the decision", errors)

    recovery = contract.get("required_recovery_boundaries", {})
    require(recovery.get("issue") == "ramideltoro/nutsnews#504", "failover must depend on #504", errors)
    require(recovery.get("post_failover_authoritative_provider") == "existing_production_supabase_standby", "post-failover authority mismatch", errors)

    safety = contract.get("safety", {})
    require(safety.get("protected_environment") == "production-backend", "workflow must use production-backend", errors)
    require(safety.get("runs_from") == "main", "workflow must run from main", errors)
    require(safety.get("requires_typed_confirmation_for_apply") is True, "apply must require typed confirmation", errors)
    require(safety.get("dry_run_by_default") is True, "workflow must dry-run by default", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("does_not_reimplement_gate_checks") is True, "workflow must not reimplement gates", errors)
    require(safety.get("app_worker_writes_to_supabase_before_approved_failover") is False, "pre-failover app/worker Supabase writes must stay blocked", errors)

    actions = {item.get("id"): item for item in contract.get("provider_switch_actions", []) if isinstance(item, dict)}
    for action_id in ("consume_promotion_decision", "app_provider_switch", "worker_provider_switch", "post_failover_smoke"):
        require(action_id in actions, f"missing provider switch action {action_id}", errors)
    for action_id in ("app_provider_switch", "worker_provider_switch"):
        action = actions.get(action_id, {})
        require(action.get("target_database_provider_mode") == "supabase_primary", f"{action_id} must target supabase_primary", errors)
        require(action.get("production_writes_paused") == "true", f"{action_id} must keep writes paused", errors)
        require(action.get("required_confirmation") == "deploy-supabase-primary", f"{action_id} confirmation mismatch", errors)

    for failure in ("missing_go_decision", "no_go_decision", "expired_decision", "consumed_decision", "mismatched_attempt"):
        require(contract.get("failure_policy", {}).get(failure) == "blocked", f"failure policy for {failure} must be blocked", errors)

    for token in (
        "WORKFLOW_ID = \"backend-supabase-standby-failover\"",
        "ISSUE = \"ramideltoro/nutsnews#502\"",
        "APPLY_CONFIRMATION = \"execute-supabase-standby-failover\"",
        "validate_promotion_decision",
        "validate_recovery_boundaries_contract",
        "would_consume_promotion_decision",
        "provider_switch_performed_by_this_workflow",
        "does_not_reimplement_gate_checks",
        "--enforce",
    ):
        require_token(token, script, "failover script", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
    ):
        require(forbidden not in script, f"failover plan must not contain provisioning/secret marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "promotion_decision_run_id:",
        "execute-supabase-standby-failover",
        "environment: production-backend",
        "gh run download",
        "backend-supabase-standby-promotion-decision",
        "backend-supabase-standby-failover-plan",
        "python3 scripts/validate_backend_supabase_standby_failover.py",
        "python3 -m unittest tests.test_backend_supabase_standby_failover",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "failover workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '--operation "${{ inputs.operation }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_validator_passes",
        "test_missing_promotion_decision_blocks_dry_run",
        "test_no_go_decision_blocks_apply",
        "test_expired_go_decision_blocks_apply",
        "test_go_apply_would_consume_single_use_decision",
        "test_mismatched_attempt_blocks_apply",
        "test_enforce_returns_nonzero_when_blocked",
        "test_artifact_is_safe_metadata_only",
    ):
        require_token(token, tests, "failover tests", errors)

    require_token("backend-supabase-standby-failover", checks_workflow, "backend checks workflow", errors)
    require_token("tests.test_backend_supabase_standby_failover", checks_workflow, "backend checks workflow", errors)
    require_token("SUPABASE_STANDBY_FAILOVER.md", readme, "README", errors)
    for token in ("Issue #502", "fresh", "single-use", "GO", "supabase_primary", "does not reimplement"):
        require_token(token, runbook, "failover runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby failover workflow guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
