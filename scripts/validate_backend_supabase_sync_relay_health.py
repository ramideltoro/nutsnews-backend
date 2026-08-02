#!/usr/bin/env python3
"""Validate backend-to-Supabase sync relay health and lag guardrails."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH_REPORT_PATH = ROOT / "scripts" / "backend_health_report.py"
RELAY_PATH = ROOT / "scripts" / "backend_supabase_sync_relay.py"
HEALTH_TEST_PATH = ROOT / "tests" / "test_backend_health_report.py"
RELAY_TEST_PATH = ROOT / "tests" / "test_backend_supabase_sync_relay.py"
RUNBOOK_PATH = ROOT / "runbooks" / "BACKEND_HEALTH_REPORT.md"
RELAY_RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_SYNC_RELAY.md"
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-sync-relay.json"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def require(token: str, text: str, label: str, errors: list[str]) -> None:
    if token not in text:
        errors.append(f"{label} missing token: {token}")


def main() -> int:
    health_report = read(HEALTH_REPORT_PATH)
    relay = read(RELAY_PATH)
    health_tests = read(HEALTH_TEST_PATH)
    relay_tests = read(RELAY_TEST_PATH)
    runbook = read(RUNBOOK_PATH)
    relay_runbook = read(RELAY_RUNBOOK_PATH)
    contract = read(CONTRACT_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    errors: list[str] = []

    for token in (
        "SUPABASE_SYNC_RELAY_REPORT_PATH = \"/var/lib/nutsnews/supabase-sync-relay/last-run.json\"",
        "SUPABASE_SYNC_RELAY_LAG_CRITICAL_SECONDS = 180",
        "\"supabase_sync_relay_unit_states\"",
        "\"supabase_sync_relay_status\"",
        "nutsnews-supabase-sync-relay.timer",
        "nutsnews-supabase-sync-relay.service",
        "classify_supabase_sync_relay",
        "relay_lag_exceeds_threshold",
        "relay_timer_stopped",
        "relay_report_missing",
        "relay_failed_tables_present",
        "standby_failover_blocked=",
        "\"name\": \"supabase_sync_relay_health\"",
        "supabase_sync_relay_lag",
        "supabase_sync_relay_stopped",
    ):
        require(token, health_report, "backend health report", errors)

    for forbidden in ("postgres://", "postgresql://", "PGPASSWORD", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL"):
        if forbidden in health_report:
            errors.append(f"backend health report must not contain relay credential marker: {forbidden}")

    for token in (
        "test_supabase_sync_relay_health_is_healthy_when_recent_and_timer_active",
        "test_supabase_sync_relay_lag_over_180_seconds_is_critical_alert",
        "test_supabase_sync_relay_missing_or_stopped_is_critical_alert",
        "test_supabase_sync_relay_failed_table_count_is_critical",
        "supabase_sync_relay_health",
        "supabase_sync_relay_lag",
        "supabase_sync_relay_stopped",
    ):
        require(token, health_tests, "health report tests", errors)

    for token in ("completed_at_utc", "last_applied_at_utc"):
        require(token, relay, "sync relay", errors)
        require(token, relay_tests, "sync relay tests", errors)

    for token in (
        "supabase_sync_relay_health",
        "lag_seconds",
        "failed_table_count",
        "last_applied_at_utc",
        "180 seconds",
        "critical",
    ):
        require(token, runbook, "backend health report runbook", errors)

    for token in (
        "Issue #500",
        "last_applied_at_utc",
        "lag_seconds",
        "Lag over `180` seconds",
        "Missing or stopped relay timer",
    ):
        require(token, relay_runbook, "sync relay runbook", errors)

    for token in (
        "\"health\"",
        "\"issue\": \"ramideltoro/nutsnews#500\"",
        "\"check_name\": \"supabase_sync_relay_health\"",
        "\"lag_critical_seconds\": 180",
        "\"relay_lag_exceeds_threshold\"",
    ):
        require(token, contract, "sync relay contract", errors)

    require(
        "python3 scripts/validate_backend_supabase_sync_relay_health.py",
        checks_workflow,
        "backend checks workflow",
        errors,
    )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Backend Supabase sync relay health guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
