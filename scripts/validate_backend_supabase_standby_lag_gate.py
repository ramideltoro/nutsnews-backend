#!/usr/bin/env python3
"""Validate Supabase standby lag gate guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-lag-gate.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_lag_gate.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_lag_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-lag-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_LAG_GATE.md"


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

    require(contract.get("gate_id") == "supabase_standby_lag", "gate_id must be supabase_standby_lag", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#522", "contract must point to #522", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    thresholds = contract.get("thresholds", {})
    require(thresholds.get("max_allowed_lag_seconds") == 30, "lag threshold must be 30 seconds", errors)
    require(thresholds.get("max_telemetry_age_seconds") == 120, "telemetry freshness threshold must be 120 seconds", errors)
    require(thresholds.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    workflow_contract = contract.get("workflow", {})
    require(workflow_contract.get("environment") == "production-backend", "workflow must use production-backend environment", errors)
    require(workflow_contract.get("default_enforce") is False, "default enforce must be false for proof runs", errors)

    for blocker in (
        "lag_exceeds_threshold",
        "relay_unhealthy",
        "relay_health_telemetry_missing",
        "telemetry_stale",
        "telemetry_malformed",
        "target_fingerprint_mismatch",
        "relay_telemetry_unavailable",
    ):
        require(blocker in contract.get("fail_closed_blockers", []), f"contract missing fail-closed blocker: {blocker}", errors)

    for token in (
        "GATE_NAME = \"supabase_standby_lag\"",
        "ISSUE = \"ramideltoro/nutsnews#522\"",
        "MAX_ALLOWED_LAG_SECONDS = 30",
        "MAX_TELEMETRY_AGE_SECONDS = 120",
        "RESULT_TTL_SECONDS = 300",
        "ATTEMPT_ID_RE",
        "safe_fingerprint",
        "supabase_sync_relay_health",
        "supabase_sync_relay_status",
        "lag_exceeds_threshold",
        "target_fingerprint_mismatch",
        "safe_metadata_only",
        "--enforce",
    ):
        require_token(token, script, "lag gate script", errors)

    for forbidden in ("postgres://", "postgresql://", "PGPASSWORD", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "supabase.co"):
        require(forbidden not in script, f"lag gate script must not contain credential/host marker: {forbidden}", errors)

    for token in (
        "test_boundary_29_seconds_passes",
        "test_boundary_30_seconds_passes",
        "test_boundary_31_seconds_fails",
        "test_missing_health_check_fails_closed",
        "test_stale_telemetry_fails_closed",
        "test_malformed_health_report_fails_closed",
        "test_mismatched_target_fails_closed_without_printing_target_label",
        "test_stopped_relay_fails_closed",
        "test_unavailable_relay_status_fails_closed",
        "test_result_and_summary_are_safe_metadata_only",
    ):
        require_token(token, tests, "lag gate tests", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "confirmation:",
        "evaluate-standby-lag-gate",
        "enforce:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "ssh-keygen -F \"$NUTSNEWS_BACKEND_HOST\" -f \"$HOME/.ssh/known_hosts\" > /dev/null 2>&1",
        "python3 scripts/backend_health_report.py",
        "> \"$RUNNER_TEMP/backend-health-report.stdout\"",
        "python3 scripts/backend_supabase_standby_lag_gate.py",
        "backend-supabase-standby-lag-gate",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "lag gate workflow", errors)

    for forbidden in ("NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "PGPASSWORD", "postgresql://", "postgres://"):
        require(forbidden not in workflow, f"lag gate workflow must not contain credential marker: {forbidden}", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_lag_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_lag_gate",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "Issue #522",
        "PASS",
        "FAIL",
        "`<= 30` seconds",
        "Missing, stale, malformed, mismatched-target, stopped-relay, and",
        "safe metadata only",
    ):
        require_token(token, runbook, "lag gate runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby lag gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
