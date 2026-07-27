#!/usr/bin/env python3
"""Validate Supabase standby production acceptance guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-production-acceptance.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_production_acceptance.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_production_acceptance.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-production-acceptance.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_PRODUCTION_ACCEPTANCE.md"
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
    require(contract.get("acceptance_id") == "backend-supabase-standby-production-acceptance", "acceptance_id is incorrect", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#505", "contract must point to #505", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    for dependency in (
        "docs/backend-supabase-sync-relay.json",
        "docs/backend-supabase-standby-lag-gate.json",
        "docs/backend-supabase-standby-parity-gate.json",
        "docs/backend-supabase-standby-failover-workflow.json",
        "docs/backend-supabase-standby-staging-failover-drill.json",
    ):
        require(dependency in contract.get("depends_on", []), f"missing dependency {dependency}", errors)

    source = contract.get("source_before_acceptance", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require("normal production read/write primary" in source.get("role", ""), "source role must preserve backend primary", errors)
    target = contract.get("target_after_acceptance", {})
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)

    soak = contract.get("soak_requirements", {})
    require(soak.get("minimum_window_hours") == 24, "soak must require 24 hours", errors)
    require(soak.get("relay_health_status") == "healthy", "relay health must be healthy", errors)
    require(soak.get("max_observed_lag_seconds") == 30, "lag must be <= 30 seconds", errors)
    require(soak.get("critical_backend_health_count") == 0, "critical backend health count must be zero", errors)
    require(soak.get("parity_status") == "PASS", "parity must pass", errors)
    require(soak.get("safe_metadata_only") is True, "soak must be safe metadata only", errors)

    failover = contract.get("required_production_dry_run", {})
    require(failover.get("workflow_id") == "backend-supabase-standby-failover", "must consume #502 dry-run shape", errors)
    require(failover.get("issue") == "ramideltoro/nutsnews#502", "must point to #502", errors)
    require(failover.get("operation") == "dry-run", "required failover operation must be dry-run", errors)
    require(
        failover.get("accepted_blocker_without_go") == "missing_supabase_standby_promotion_decision",
        "must accept only the expected missing-GO dry-run blocker",
        errors,
    )
    require(failover.get("mutation_performed") is False, "production dry-run must be non-mutating", errors)
    require(failover.get("provider_switch_performed_by_this_workflow") is False, "dry-run must not switch provider", errors)

    staging = contract.get("required_staging_drill", {})
    require(staging.get("drill_id") == "backend-supabase-standby-staging-failover-drill", "must consume #503 drill shape", errors)
    require(staging.get("operation") == "staging-apply", "staging drill operation mismatch", errors)
    require(staging.get("status") == "PASS", "staging drill must require PASS", errors)
    require(staging.get("target_database_provider_mode") == "supabase_primary", "staging drill must prove supabase_primary", errors)
    require(staging.get("production_writes_paused") is True, "staging drill must preserve write pause", errors)
    require(staging.get("backend_postgres_write_delta_after_failover") == 0, "backend write delta must be zero", errors)
    require(staging.get("write_eligible_provider_count") == 1, "write eligible provider count must be one", errors)
    require(staging.get("eligible_provider") == "existing_production_supabase_standby", "eligible provider mismatch", errors)

    decision = contract.get("decision_policy", {})
    require(decision.get("accepted_decision") == "GO", "accepted decision must be GO", errors)
    require(decision.get("rejected_decision") == "NO-GO", "rejected decision must be NO-GO", errors)
    require(decision.get("requires_explicit_owner_decision") is True, "explicit owner decision policy missing", errors)
    require(decision.get("go_does_not_execute_failover") is True, "GO must not execute failover", errors)
    require(decision.get("failover_execution_still_requires_fresh_528_go") is True, "fresh #528 GO must remain required", errors)

    safety = contract.get("safety", {})
    require(safety.get("protected_environment") == "production-backend", "workflow must use production-backend gate", errors)
    require(safety.get("runs_from") == "main", "workflow must run from main", errors)
    require(safety.get("requires_typed_confirmation_for_acceptance") is True, "typed confirmation missing", errors)
    require(safety.get("dry_run_by_default") is True, "dry-run default missing", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("production_mutation_performed") is False, "production mutation must be false", errors)
    require(safety.get("provider_switch_performed") is False, "provider switch must be false", errors)
    require(safety.get("approved_for_production_provider_switch") is False, "provider switch approval must be false", errors)
    require(safety.get("backend_postgresql_remains_primary_until_approved_failover") is True, "backend primary policy missing", errors)
    require(safety.get("target_is_existing_production_supabase") is True, "existing target policy missing", errors)
    require(safety.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(safety.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)
    require(safety.get("app_worker_writes_to_supabase_before_approved_failover") is False, "pre-failover Supabase writes must stay blocked", errors)

    validation = contract.get("validation", {})
    require(
        validation.get("local_validator") == "python3 scripts/validate_backend_supabase_standby_production_acceptance.py",
        "local validator command is incorrect",
        errors,
    )
    require("record-production-standby-acceptance" in validation.get("acceptance_runner", ""), "acceptance runner must include typed confirmation", errors)
    require("--owner-decision GO" in validation.get("acceptance_runner", ""), "acceptance runner must include owner GO", errors)

    for token in (
        "ACCEPTANCE_ID = \"backend-supabase-standby-production-acceptance\"",
        "ISSUE = \"ramideltoro/nutsnews#505\"",
        "ACCEPTANCE_CONFIRMATION = \"record-production-standby-acceptance\"",
        "EXPECTED_MISSING_GO_BLOCKER = \"missing_supabase_standby_promotion_decision\"",
        "soak_report_summary",
        "failover_plan_summary",
        "staging_drill_summary",
        "fixture_soak_report",
        "official_backup_accepted",
        "requires_fresh_528_go_for_failover",
        "--fixture-pass",
        "--enforce",
    ):
        require_token(token, script, "production acceptance script", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "createdb",
    ):
        require(forbidden not in script, f"production acceptance script contains forbidden provisioning/secret marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "owner_decision",
        "record-production-standby-acceptance",
        "plan-production-standby-acceptance",
        "environment: production-backend",
        "backend_supabase_standby_failover_plan.py",
        "backend_supabase_standby_staging_failover_drill.py",
        "backend_supabase_standby_production_acceptance.py",
        "backend-supabase-standby-production-acceptance.json",
        "python3 scripts/validate_backend_supabase_standby_production_acceptance.py",
        "python3 -m unittest tests.test_backend_supabase_standby_production_acceptance",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "production acceptance workflow", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "supabase db",
    ):
        require(forbidden not in workflow, f"production acceptance workflow contains forbidden marker: {forbidden}", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '--operation "${{ inputs.operation }}"',
        '--owner-decision "${{ inputs.owner_decision }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_fixture_acceptance_go_passes",
        "test_missing_soak_report_blocks_without_fixture",
        "test_soak_window_under_24_hours_blocks",
        "test_lag_over_30_blocks",
        "test_critical_backend_health_blocks",
        "test_parity_not_pass_blocks",
        "test_missing_failover_plan_blocks",
        "test_mutating_failover_plan_blocks",
        "test_staging_drill_not_pass_blocks",
        "test_owner_no_go_blocks",
        "test_enforce_returns_nonzero_on_no_go",
        "test_artifact_omits_secrets_and_raw_data_markers",
    ):
        require_token(token, tests, "production acceptance tests", errors)

    require_token("backend-supabase-standby-production-acceptance", checks_workflow, "backend checks workflow", errors)
    require_token("tests.test_backend_supabase_standby_production_acceptance", checks_workflow, "backend checks workflow", errors)
    require_token("SUPABASE_STANDBY_PRODUCTION_ACCEPTANCE.md", readme, "README", errors)
    for token in (
        "Issue #505",
        "24 hours",
        "lag <= 30 seconds",
        "protected production failover dry-run",
        "GO",
        "NO-GO",
        "fresh #528 GO",
        "existing production Supabase",
        "does not switch providers",
    ):
        require_token(token, runbook, "production acceptance runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby production acceptance guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
