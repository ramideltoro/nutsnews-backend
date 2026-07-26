#!/usr/bin/env python3
"""Validate Supabase standby required-table parity gate guardrails."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-parity-gate.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_parity_gate.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_parity_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-parity-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_PARITY_GATE.md"


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
    errors: list[str] = []

    require(contract.get("gate_id") == "supabase_standby_required_table_parity", "gate_id is incorrect", errors)
    require(contract.get("version") == 1, "contract version must be 1", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#523", "contract must point to #523", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    require("ramideltoro/nutsnews#497" in contract.get("parent_prerequisites", []), "contract must depend on #497", errors)
    require("ramideltoro/nutsnews#499" in contract.get("parent_prerequisites", []), "contract must depend on #499", errors)

    thresholds = contract.get("thresholds", {})
    require(thresholds.get("max_telemetry_age_seconds") == 300, "telemetry threshold must be 300 seconds", errors)
    require(thresholds.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    workflow_contract = contract.get("workflow", {})
    require(workflow_contract.get("environment") == "production-backend", "workflow must use production-backend", errors)
    require(workflow_contract.get("default_enforce") is False, "default enforce must be false", errors)
    require(workflow_contract.get("artifact") == "backend-supabase-standby-parity-gate", "artifact name is incorrect", errors)

    required_fields = set(contract.get("result_schema", {}).get("required_fields", []))
    for field in (
        "status",
        "gate",
        "failover_attempt_id",
        "manifest_fingerprint",
        "relay_contract_fingerprint",
        "source_fingerprint",
        "target_fingerprint",
        "measured_at_utc",
        "expires_at_utc",
        "relay_checked_at_utc",
        "relay_comparison_age_seconds",
        "tables",
        "blockers",
        "safe_metadata_only",
    ):
        require(field in required_fields, f"result schema missing {field}", errors)

    per_table_fields = set(contract.get("result_schema", {}).get("per_table_fields", []))
    for field in ("name", "status", "source_count", "target_count", "source_row_checksum", "target_row_checksum", "blockers"):
        require(field in per_table_fields, f"per-table schema missing {field}", errors)

    for blocker in (
        "telemetry_unavailable",
        "telemetry_malformed",
        "telemetry_stale",
        "relay_telemetry_unavailable",
        "relay_comparison_stale",
        "source_fingerprint_mismatch",
        "target_fingerprint_mismatch",
        "comparison_checks_missing",
        "table_parity_failed",
        "target_existing_production_supabase_not_confirmed",
        "app_worker_supabase_writes_not_blocked",
    ):
        require(blocker in contract.get("fail_closed_blockers", []), f"contract missing fail-closed blocker: {blocker}", errors)

    for blocker in (
        "table_comparison_missing",
        "table_status_not_pass",
        "row_count_mismatch",
        "row_checksum_mismatch",
        "target_lag_rows_nonzero",
        "checksum_query_error",
    ):
        require(blocker in contract.get("per_table_blockers", []), f"contract missing per-table blocker: {blocker}", errors)

    for token in (
        "GATE_NAME = \"supabase_standby_required_table_parity\"",
        "ISSUE = \"ramideltoro/nutsnews#523\"",
        "EPIC = \"ramideltoro/nutsnews#521\"",
        "MAX_TELEMETRY_AGE_SECONDS = 300",
        "RESULT_TTL_SECONDS = 300",
        "EXPECTED_SOURCE_LABEL = \"backend_postgres_primary\"",
        "EXPECTED_TARGET_LABEL = \"existing_production_supabase_standby\"",
        "backend_postgresql_remains_primary",
        "target_is_existing_production_supabase",
        "create_new_supabase_project",
        "create_nutsnews_standby_database",
        "app_worker_writes_to_supabase_before_failover",
        "manifest_fingerprint",
        "relay_contract_fingerprint",
        "relay_checked_at_utc",
        "relay_comparison_stale",
        "comparison_checks_missing",
        "table_parity_failed",
        "row_count_mismatch",
        "row_checksum_mismatch",
        "--enforce",
    ):
        require_token(token, script, "parity gate script", errors)

    for forbidden in ("postgres://", "postgresql://", "PGPASSWORD", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "supabase.co"):
        require(forbidden not in script, f"parity gate script must not contain credential/host marker: {forbidden}", errors)

    for token in (
        "test_exact_required_table_parity_passes",
        "test_sequence_only_post_sync_failure_does_not_block_table_parity",
        "test_added_row_fixture_fails_count_mismatch",
        "test_deleted_row_fixture_fails_count_mismatch",
        "test_changed_row_fixture_fails_checksum_mismatch",
        "test_missing_required_table_fails",
        "test_incomplete_comparison_fails",
        "test_stale_telemetry_fails_closed",
        "test_malformed_telemetry_fails_closed",
        "test_mismatched_target_fails_without_printing_target_label",
        "test_unavailable_relay_status_fails_closed",
        "test_enforce_returns_nonzero_on_failure",
        "test_artifact_and_summary_are_safe_metadata_only",
    ):
        require_token(token, tests, "parity gate tests", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "confirmation:",
        "evaluate-standby-parity-gate",
        "enforce:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "CONFIRMATION: ${{ inputs.confirmation }}",
        "FAILOVER_ATTEMPT_ID: ${{ inputs.failover_attempt_id }}",
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "python3 scripts/backend_health_report.py",
        "--ssh-key \"$HOME/.ssh/nutsnews_backend_parity_gate\"",
        "> \"$RUNNER_TEMP/backend-health-report.stdout\"",
        "python3 scripts/backend_supabase_standby_parity_gate.py",
        "backend-supabase-standby-parity-gate",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "parity gate workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)
    for forbidden in ("NUTSNEWS_BACKEND_HOST", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "PGPASSWORD", "postgresql://", "postgres://"):
        require(forbidden not in workflow, f"parity gate workflow must not contain credential/host marker: {forbidden}", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_parity_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_parity_gate",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "Issue #523",
        "PASS",
        "FAIL",
        "required application tables",
        "safe metadata only",
        "missing required table",
        "incomplete comparison",
        "added-row fixture",
        "changed-row fixture",
        "deleted-row fixture",
    ):
        require_token(token, runbook, "parity gate runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby required-table parity gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
