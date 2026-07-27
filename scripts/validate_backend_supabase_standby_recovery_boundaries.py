#!/usr/bin/env python3
"""Validate Supabase standby recovery boundary guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-recovery-boundaries.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_recovery_boundaries.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_recovery_boundaries.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-recovery-boundaries.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_RECOVERY_BOUNDARIES.md"
README_PATH = ROOT / "README.md"
ROLLBACK_GUARDRAILS_PATH = ROOT / "docs" / "backend-db-rollback-guardrails.json"
ROLLBACK_VALIDATOR_PATH = ROOT / "scripts" / "validate_backend_db_rollback_guardrails.py"
PROVIDER_SWITCH_PATH = ROOT / "docs" / "backend-database-provider-switch.json"
PROVIDER_SWITCH_SCRIPT_PATH = ROOT / "scripts" / "backend_database_provider_switch_plan.py"
PROVIDER_SWITCH_VALIDATOR_PATH = ROOT / "scripts" / "validate_backend_database_provider_switch.py"
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
    rollback_guardrails = load_json(ROLLBACK_GUARDRAILS_PATH)
    rollback_validator = read(ROLLBACK_VALIDATOR_PATH)
    provider_switch = load_json(PROVIDER_SWITCH_PATH)
    provider_switch_script = read(PROVIDER_SWITCH_SCRIPT_PATH)
    provider_switch_validator = read(PROVIDER_SWITCH_VALIDATOR_PATH)
    provider_switch_tests = read(PROVIDER_SWITCH_TEST_PATH)
    errors: list[str] = []

    require(contract.get("schema_version") == 1, "schema_version must be 1", errors)
    require(contract.get("contract_id") == "backend-supabase-standby-recovery-boundaries", "contract_id is incorrect", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#504", "contract must point to #504", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)

    target = contract.get("target_after_failover", {})
    safety = contract.get("safety", {})
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("mutates_production") is False, "contract must be non-mutating", errors)
    require(safety.get("backend_postgresql_remains_primary_until_approved_failover") is True, "backend primary-until-failover flag missing", errors)
    require(safety.get("app_worker_writes_to_supabase_before_approved_failover") is False, "pre-failover Supabase writes must stay blocked", errors)
    require(safety.get("post_failover_supabase_is_authoritative") is True, "post-failover Supabase authority missing", errors)
    require(
        safety.get("backend_postgres_reuse_blocked_until_rebuilt_or_reconciled_from_supabase") is True,
        "backend reuse block missing",
        errors,
    )

    boundary_ids = {item.get("id") for item in contract.get("boundaries", []) if isinstance(item, dict)}
    require(
        boundary_ids == {"pre_switch_abort", "post_supabase_failover_forward_recovery", "switch_back_to_backend_postgres"},
        "contract must define exactly three recovery boundaries",
        errors,
    )
    boundaries = {item.get("id"): item for item in contract.get("boundaries", []) if isinstance(item, dict)}
    require(boundaries.get("post_supabase_failover_forward_recovery", {}).get("authoritative_provider") == "existing_production_supabase_standby", "Supabase must be authoritative after failover", errors)
    require(boundaries.get("post_supabase_failover_forward_recovery", {}).get("backend_postgres_reuse_allowed") is False, "backend reuse must be false after failover", errors)
    require(
        boundaries.get("switch_back_to_backend_postgres", {}).get("backend_postgres_reuse_allowed")
        == "only_after_rebuild_or_reconciliation_from_supabase",
        "switch-back must require Supabase-origin rebuild or reconciliation",
        errors,
    )
    gate_ids = {item.get("id") for item in contract.get("switch_back_gates", []) if isinstance(item, dict)}
    required_gate_ids = {
        "backend_rebuild_or_reconciliation_from_supabase",
        "supabase_to_backend_parity",
        "backend_sequence_safety",
        "no_split_brain_fence",
        "writer_pause",
        "owner_approval",
        "staging_drill_evidence",
    }
    require(gate_ids == required_gate_ids, "switch-back gate set is incorrect", errors)

    for failure in ("missing_evidence", "malformed_evidence", "failing_evidence", "unsafe_metadata", "split_brain_risk"):
        require(contract.get("failure_policy", {}).get(failure) == "blocked", f"failure policy for {failure} must be blocked", errors)

    for token in (
        "CONTRACT_ID = \"backend-supabase-standby-recovery-boundaries\"",
        "ISSUE = \"ramideltoro/nutsnews#504\"",
        "SUPABASE_STANDBY = \"existing_production_supabase_standby\"",
        "SWITCH_BACK_EVIDENCE",
        "backend_postgres_reuse_policy",
        "blocked_until_rebuilt_or_reconciled_from_authoritative_supabase",
        "not_all_switch_back_gates_passed",
        "--enforce",
    ):
        require_token(token, script, "recovery boundary script", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
    ):
        require(forbidden not in script, f"recovery evaluator must not contain provisioning/secret marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "boundary:",
        "provider_switch_performed:",
        "evaluate-supabase-standby-recovery-boundaries",
        "environment: production-backend",
        "python3 scripts/validate_backend_supabase_standby_recovery_boundaries.py",
        "python3 -m unittest tests.test_backend_supabase_standby_recovery_boundaries",
        "backend-supabase-standby-recovery-boundaries",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "recovery boundary workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '--boundary "${{ inputs.boundary }}"',
        '--provider-switch-performed "${{ inputs.provider_switch_performed }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_validator_passes",
        "test_post_failover_forward_recovery_blocks_backend_reuse",
        "test_switch_back_without_evidence_fails_closed",
        "test_switch_back_with_safe_evidence_is_ready",
        "test_pre_switch_abort_blocks_after_provider_switch",
        "test_enforce_returns_nonzero_for_blocked_switch_back",
        "test_artifact_is_safe_metadata_only",
    ):
        require_token(token, tests, "recovery boundary tests", errors)

    require_token("backend-supabase-standby-recovery-boundaries", checks_workflow, "backend checks workflow", errors)
    require_token("tests.test_backend_supabase_standby_recovery_boundaries", checks_workflow, "backend checks workflow", errors)
    require_token("SUPABASE_STANDBY_RECOVERY_BOUNDARIES.md", readme, "README", errors)
    for token in ("Issue #504", "Supabase is authoritative", "backend PostgreSQL cannot resume primary", "switch-back"):
        require_token(token, runbook, "recovery boundary runbook", errors)

    recovery = rollback_guardrails.get("supabase_standby_failover_recovery", {})
    require(recovery.get("tracking_issue") == "ramideltoro/nutsnews#504", "rollback guardrails must reference #504", errors)
    require(recovery.get("post_failover_authoritative_provider") == "existing_production_supabase_standby", "rollback guardrails must make Supabase authoritative", errors)
    require(recovery.get("backend_postgres_reuse_policy") == "blocked_until_rebuilt_or_reconciled_from_supabase", "rollback guardrails must block backend reuse", errors)
    require_token("supabase_standby_failover_recovery", rollback_validator, "rollback validator", errors)
    require_token("backend_postgres_reuse_policy", rollback_validator, "rollback validator", errors)

    require("docs/backend-supabase-standby-recovery-boundaries.json" in provider_switch.get("depends_on", []), "provider switch must depend on #504 contract", errors)
    standby_recovery = provider_switch.get("supabase_standby_recovery_boundaries", {})
    require(standby_recovery.get("issue") == "ramideltoro/nutsnews#504", "provider switch must point to #504 recovery boundaries", errors)
    require(standby_recovery.get("required_before_production_supabase_switch") is True, "provider switch must require #504 before Supabase switch", errors)
    require_token("validate_recovery_boundaries_contract", provider_switch_script, "provider switch plan", errors)
    require_token("recovery_boundaries_required", provider_switch_script, "provider switch plan", errors)
    require_token("backend-supabase-standby-recovery-boundaries", provider_switch_validator, "provider switch validator", errors)
    require_token("test_production_supabase_switch_reports_recovery_boundaries_contract", provider_switch_tests, "provider switch tests", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby recovery boundary guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
