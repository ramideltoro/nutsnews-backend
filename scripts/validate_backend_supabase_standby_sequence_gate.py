#!/usr/bin/env python3
"""Validate Supabase standby sequence safety gate guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-sequence-gate.json"
SYNC_RELAY_CONTRACT_PATH = ROOT / "docs" / "backend-supabase-sync-relay.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_sequence_gate.py"
RELAY_SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_reconcile.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_sequence_gate.py"
RELAY_TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_reconcile.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-sequence-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_SEQUENCE_GATE.md"
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
    sync_relay_contract = load_json(SYNC_RELAY_CONTRACT_PATH)
    script = read(SCRIPT_PATH)
    relay_script = read(RELAY_SCRIPT_PATH)
    tests = read(TEST_PATH)
    relay_tests = read(RELAY_TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    errors: list[str] = []

    require(contract.get("gate_id") == "supabase_standby_sequence_safety", "gate_id is incorrect", errors)
    require(contract.get("version") == 1, "contract version must be 1", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#525", "contract must point to #525", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    require("ramideltoro/nutsnews#497" in contract.get("parent_prerequisites", []), "contract must depend on #497", errors)
    require("ramideltoro/nutsnews#499" in contract.get("parent_prerequisites", []), "contract must depend on #499", errors)
    require(len(sync_relay_contract.get("sequences", [])) == 6, "sync relay contract must define the six manifest sequences", errors)

    thresholds = contract.get("thresholds", {})
    require(thresholds.get("max_telemetry_age_seconds") == 300, "telemetry threshold must be 300 seconds", errors)
    require(thresholds.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    workflow_contract = contract.get("workflow", {})
    require(workflow_contract.get("environment") == "production-backend", "workflow must use production-backend", errors)
    require(workflow_contract.get("runs_on") == "ubuntu-latest", "workflow must use ubuntu-latest", errors)
    require(workflow_contract.get("default_enforce") is False, "default enforce must be false", errors)
    require(workflow_contract.get("artifact") == "backend-supabase-standby-sequence-gate", "artifact name is incorrect", errors)

    required_fields = set(contract.get("result_schema", {}).get("required_fields", []))
    for field in (
        "status",
        "gate",
        "failover_attempt_id",
        "repository_revision",
        "manifest_fingerprint",
        "relay_contract_fingerprint",
        "source_fingerprint",
        "target_fingerprint",
        "measured_at_utc",
        "expires_at_utc",
        "relay_checked_at_utc",
        "relay_sequence_age_seconds",
        "required_sequence_count",
        "passed_sequence_count",
        "failed_sequence_count",
        "sequences",
        "blockers",
        "safe_metadata_only",
    ):
        require(field in required_fields, f"result schema missing {field}", errors)

    per_sequence_fields = set(contract.get("result_schema", {}).get("per_sequence_fields", []))
    for field in (
        "name",
        "table",
        "column",
        "status",
        "source_next_value",
        "target_next_value",
        "source_max_id",
        "target_max_id",
        "binding_fingerprint",
        "blockers",
    ):
        require(field in per_sequence_fields, f"per-sequence schema missing {field}", errors)

    for blocker in (
        "telemetry_unavailable",
        "telemetry_malformed",
        "relay_telemetry_unavailable",
        "relay_sequence_stale",
        "source_fingerprint_mismatch",
        "target_fingerprint_mismatch",
        "post_sync_checks_missing",
        "sequence_safety_failed",
        "target_existing_production_supabase_not_confirmed",
        "app_worker_supabase_writes_not_blocked",
    ):
        require(blocker in contract.get("fail_closed_blockers", []), f"contract missing fail-closed blocker: {blocker}", errors)

    for blocker in (
        "sequence_check_missing",
        "sequence_status_not_pass",
        "source_sequence_unowned",
        "target_sequence_unowned",
        "source_sequence_misbound",
        "target_sequence_misbound",
        "source_sequence_cycle_enabled",
        "target_sequence_cycle_enabled",
        "source_sequence_exhausted",
        "target_sequence_exhausted",
        "source_sequence_increment_not_one",
        "target_sequence_increment_not_one",
        "source_next_value_not_above_source_max_id",
        "target_next_value_not_above_target_max_id",
        "target_next_value_not_above_source_max_id",
        "target_next_value_lt_source_next_value",
    ):
        require(blocker in contract.get("sequence_blockers", []), f"contract missing sequence blocker: {blocker}", errors)

    for token in (
        "GATE_NAME = \"supabase_standby_sequence_safety\"",
        "ISSUE = \"ramideltoro/nutsnews#525\"",
        "EPIC = \"ramideltoro/nutsnews#521\"",
        "MAX_TELEMETRY_AGE_SECONDS = 300",
        "RESULT_TTL_SECONDS = 300",
        "EXPECTED_SOURCE_LABEL = \"backend_postgres_primary\"",
        "EXPECTED_TARGET_LABEL = \"existing_production_supabase_standby\"",
        "sequence_side_blockers",
        "target_next_value_lt_source_next_value",
        "target_next_value_not_above_target_max_id",
        "sequence_cycle_enabled",
        "sequence_exhausted",
        "sequence_increment_mismatch",
        "sequence_safety_failed",
        "safe_metadata_only",
        "--enforce",
    ):
        require_token(token, script, "sequence gate script", errors)

    for forbidden in ("postgres://", "postgresql://", "PGPASSWORD", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "supabase.co"):
        require(forbidden not in script, f"sequence gate script must not contain credential/host marker: {forbidden}", errors)

    for token in (
        "sequence_state_sql",
        "pg_sequences.max_value",
        "pg_sequences.cycle",
        "pg_depend",
        "expected_binding_matches",
        "owned_by_count",
        "sequence_metadata_reasons",
        "target_next_value_lt_source_next_value",
        "sequence_exhausted",
        "sequence_misbound",
    ):
        require_token(token, relay_script, "relay reconcile script", errors)
    require("nextval" not in relay_script[relay_script.find("def sequence_state_sql"):relay_script.find("def sequence_next_value")].lower(), "sequence metadata SQL must not consume nextval", errors)

    for token in (
        "test_safe_sequence_fixtures_pass_without_nextval_or_mutation",
        "test_behind_max_id_fixture_fails",
        "test_behind_source_next_fixture_fails",
        "test_missing_sequence_fails",
        "test_misbound_sequence_fails",
        "test_unowned_sequence_fails",
        "test_cycled_sequence_fails",
        "test_exhausted_sequence_fails",
        "test_incomplete_report_fixture_fails",
        "test_empty_table_sequence_semantics_pass",
        "test_never_called_sequence_semantics_fail_when_next_collides",
        "test_unexpected_increment_configuration_fails",
        "test_stale_telemetry_fails_closed",
        "test_malformed_telemetry_fails_closed",
        "test_mismatched_target_fails_without_printing_target_label",
        "test_unavailable_relay_status_fails_closed",
        "test_enforce_returns_nonzero_on_failure",
        "test_artifact_and_summary_are_safe_metadata_only",
    ):
        require_token(token, tests, "sequence gate tests", errors)
    for token in (
        "test_sequence_state_sql_does_not_consume_nextval",
        "test_target_sequence_next_value_behind_source_next_fails",
        "test_sequence_misbound_cycled_or_exhausted_fails",
    ):
        require_token(token, relay_tests, "relay reconcile tests", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "confirmation:",
        "evaluate-standby-sequence-gate",
        "enforce:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "CONFIRMATION: ${{ inputs.confirmation }}",
        "FAILOVER_ATTEMPT_ID: ${{ inputs.failover_attempt_id }}",
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "python3 scripts/backend_health_report.py",
        "--ssh-key \"$HOME/.ssh/nutsnews_backend_sequence_gate\"",
        "> \"$RUNNER_TEMP/backend-health-report.stdout\"",
        "python3 scripts/backend_supabase_standby_sequence_gate.py",
        "backend-supabase-standby-sequence-gate",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "sequence gate workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)
    for forbidden in ("NUTSNEWS_BACKEND_HOST", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "PGPASSWORD", "postgresql://", "postgres://"):
        require(forbidden not in workflow, f"sequence gate workflow must not contain credential marker: {forbidden}", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_sequence_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_sequence_gate",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "Issue #525",
        "PASS",
        "FAIL",
        "sequence safety",
        "safe metadata only",
        "behind-max-ID",
        "behind-source",
        "missing sequence",
        "misbound",
        "unowned",
        "cycled",
        "exhausted",
        "empty-table",
        "never-called",
    ):
        require_token(token, runbook, "sequence gate runbook", errors)
    require("SUPABASE_STANDBY_SEQUENCE_GATE.md" in readme, "README must link sequence gate runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby sequence safety gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
