#!/usr/bin/env python3
"""Validate Supabase standby staging failover drill guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-staging-failover-drill.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_staging_failover_drill.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_staging_failover_drill.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-staging-failover-drill.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_STAGING_FAILOVER_DRILL.md"
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
    require(contract.get("drill_id") == "backend-supabase-standby-staging-failover-drill", "drill_id is incorrect", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#503", "contract must point to #503", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    for dependency in (
        "docs/backend-supabase-standby-failover-workflow.json",
        "docs/backend-supabase-standby-recovery-boundaries.json",
        "docs/backend-database-provider-switch.json",
    ):
        require(dependency in contract.get("depends_on", []), f"missing dependency {dependency}", errors)

    source = contract.get("source_before_drill", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    target = contract.get("target_after_drill", {})
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)

    failover = contract.get("protected_failover_dry_run", {})
    require(failover.get("workflow_id") == "backend-supabase-standby-failover", "drill must consume #502 dry-run shape", errors)
    require(failover.get("issue") == "ramideltoro/nutsnews#502", "drill must point to #502", errors)
    require(
        failover.get("accepted_blocker_without_go") == "missing_supabase_standby_promotion_decision",
        "drill must accept only the expected missing-GO dry-run blocker",
        errors,
    )
    require(failover.get("mutation_performed") is False, "protected failover dry-run must be non-mutating", errors)
    require(failover.get("provider_switch_performed_by_this_workflow") is False, "dry-run must not switch provider", errors)

    staging = contract.get("staging_apply", {})
    require(staging.get("operation") == "staging-apply", "staging operation mismatch", errors)
    require(staging.get("environment") == "staging", "staging apply must be staging", errors)
    require(staging.get("required_confirmation") == "execute-staging-supabase-failover-drill", "staging confirmation mismatch", errors)
    require(staging.get("target_database_provider_mode") == "supabase_primary", "staging apply must target supabase_primary", errors)
    require(staging.get("production_writes_paused") is True, "staging apply must preserve write pause", errors)
    require(staging.get("backend_postgres_unavailable_simulated") is True, "staging apply must simulate unavailable backend Postgres", errors)
    require(staging.get("production_mutation_performed") is False, "staging apply must not mutate production", errors)

    smoke_ids = {item.get("id"): item for item in contract.get("required_smoke_results", []) if isinstance(item, dict)}
    for smoke_id in (
        "public_reads_use_supabase",
        "controlled_writes_use_supabase",
        "backend_postgres_receives_no_writes_after_failover",
        "no_split_brain",
        "write_pause_preserved",
        "negative_backend_postgres_unavailable_path",
    ):
        require(smoke_id in smoke_ids, f"missing smoke result {smoke_id}", errors)
        require(smoke_ids.get(smoke_id, {}).get("required_status") == "PASS", f"{smoke_id} must require PASS", errors)
    require(
        smoke_ids.get("backend_postgres_receives_no_writes_after_failover", {}).get("required_backend_postgres_write_delta") == 0,
        "backend write delta must be zero",
        errors,
    )
    require(smoke_ids.get("no_split_brain", {}).get("required_write_eligible_provider_count") == 1, "split-brain count must be one", errors)

    safety = contract.get("safety", {})
    require(safety.get("protected_environment") == "production-backend", "workflow must use production-backend gate", errors)
    require(safety.get("runs_from") == "main", "workflow must run from main", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("staging_only") is True, "staging-only flag missing", errors)
    require(safety.get("production_mutation_performed") is False, "production mutation must be false", errors)
    require(safety.get("backend_postgresql_remains_primary_until_approved_production_failover") is True, "backend primary policy missing", errors)
    require(safety.get("target_is_existing_production_supabase") is True, "existing target policy missing", errors)
    require(safety.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(safety.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)
    require(safety.get("app_worker_writes_to_supabase_before_approved_failover") is False, "pre-failover Supabase writes must stay blocked", errors)

    for token in (
        "DRILL_ID = \"backend-supabase-standby-staging-failover-drill\"",
        "ISSUE = \"ramideltoro/nutsnews#503\"",
        "STAGING_APPLY_CONFIRMATION = \"execute-staging-supabase-failover-drill\"",
        "EXPECTED_MISSING_GO_BLOCKER = \"missing_supabase_standby_promotion_decision\"",
        "failover_plan_summary",
        "fixture_defaults",
        "backend_postgres_write_delta_after_failover",
        "provider_mode_not_supabase_primary",
        "backend_postgres_received_writes_after_failover",
        "--fixture-pass",
        "--enforce",
    ):
        require_token(token, script, "staging drill script", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "createdb",
    ):
        require(forbidden not in script, f"staging drill script contains forbidden provisioning/secret marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "staging-apply",
        "execute-staging-supabase-failover-drill",
        "environment: production-backend",
        "backend_supabase_standby_failover_plan.py",
        "backend_supabase_standby_staging_failover_drill.py",
        "backend-supabase-standby-failover-plan.json",
        "backend-supabase-standby-staging-failover-drill.json",
        "python3 scripts/validate_backend_supabase_standby_staging_failover_drill.py",
        "python3 -m unittest tests.test_backend_supabase_standby_staging_failover_drill",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "staging drill workflow", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "supabase db",
    ):
        require(forbidden not in workflow, f"staging drill workflow contains forbidden marker: {forbidden}", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '--operation "${{ inputs.operation }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_fixture_staging_apply_passes_with_safe_metadata",
        "test_dry_run_passes_without_staging_apply",
        "test_missing_failover_plan_blocks_staging_apply",
        "test_mismatched_failover_plan_blocks_staging_apply",
        "test_wrong_confirmation_blocks_staging_apply",
        "test_backend_postgres_write_delta_blocks",
        "test_provider_mode_mismatch_blocks",
        "test_enforce_returns_nonzero_when_blocked",
        "test_artifact_omits_secrets_and_raw_data_markers",
    ):
        require_token(token, tests, "staging drill tests", errors)

    require_token("backend-supabase-standby-staging-failover-drill", checks_workflow, "backend checks workflow", errors)
    require_token("tests.test_backend_supabase_standby_staging_failover_drill", checks_workflow, "backend checks workflow", errors)
    require_token("SUPABASE_STANDBY_STAGING_FAILOVER_DRILL.md", readme, "README", errors)
    for token in (
        "Issue #503",
        "staging-apply",
        "supabase_primary",
        "write pause",
        "no split brain",
        "backend PostgreSQL receives no writes",
        "existing production Supabase",
    ):
        require_token(token, runbook, "staging drill runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby staging failover drill guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
