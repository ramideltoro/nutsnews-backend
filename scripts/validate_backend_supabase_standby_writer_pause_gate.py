#!/usr/bin/env python3
"""Validate Supabase standby writer pause gate guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-writer-pause-gate.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_writer_pause_gate.py"
POSITION_SCRIPT_PATH = ROOT / "scripts" / "backend_postgres_write_position.py"
MANAGER_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "nutsnews_writer_pause.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_writer_pause_gate.py"
POSITION_TEST_PATH = ROOT / "tests" / "test_backend_postgres_write_position.py"
MANAGER_TEST_PATH = ROOT / "tests" / "test_backend_writer_pause_manager.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-writer-pause-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_WRITER_PAUSE_GATE.md"
README_PATH = ROOT / "README.md"
ANSIBLE_DEFAULTS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "defaults" / "main.yml"
ANSIBLE_MAIN_TASKS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "main.yml"
ANSIBLE_WRITER_TASKS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "writer_pause.yml"


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
    position_script = read(POSITION_SCRIPT_PATH)
    manager = read(MANAGER_PATH)
    tests = read(TEST_PATH)
    position_tests = read(POSITION_TEST_PATH)
    manager_tests = read(MANAGER_TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    ansible_defaults = read(ANSIBLE_DEFAULTS_PATH)
    ansible_main = read(ANSIBLE_MAIN_TASKS_PATH)
    ansible_writer_tasks = read(ANSIBLE_WRITER_TASKS_PATH)
    errors: list[str] = []

    require(contract.get("schema_version") == 1, "contract schema_version must be 1", errors)
    require(contract.get("gate_id") == "backend-supabase-standby-writer-pause", "contract gate_id is incorrect", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#526", "contract must point to #526", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)

    source = contract.get("source", {})
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(source.get("role") == "normal production read/write primary", "source role must keep backend PostgreSQL primary", errors)
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase standby", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase project", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project creation must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby DB creation must be forbidden", errors)
    require(safety.get("backend_postgresql_remains_primary") is True, "backend primary safety flag missing", errors)
    require(safety.get("target_is_existing_production_supabase") is True, "existing production Supabase safety flag missing", errors)
    require(safety.get("app_worker_writes_to_supabase_before_failover") is False, "app/worker Supabase writes must remain disabled", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("sql_text_exposed") is False, "SQL text exposure must be false", errors)
    require(safety.get("row_data_exposed") is False, "row data exposure must be false", errors)
    require(safety.get("credentials_exposed") is False, "credential exposure must be false", errors)

    writer_ids = {
        item.get("id")
        for item in contract.get("writer_classes", [])
        if isinstance(item, dict)
    }
    for writer_id in (
        "backend_worker_database_api",
        "worker_uplift_runtime_services",
        "backend_mutation_workflows",
        "manual_database_access",
        "standby_sync_relay",
    ):
        require(writer_id in writer_ids, f"writer inventory missing {writer_id}", errors)
    require(len(writer_ids) == 5, "writer inventory must contain five explicit writer classes", errors)
    require(
        set(contract.get("known_runtime_services", []))
        == {"scheduler", "fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"},
        "known runtime services must be bounded",
        errors,
    )

    failure_policy = contract.get("failure_policy", {})
    for policy in (
        "unknown_writer",
        "incomplete_inventory",
        "unsuccessful_pause",
        "drain_timeout",
        "resumed_writer",
        "observed_write_position_advance",
        "stale_evidence",
        "mismatched_attempt",
        "malformed_evidence",
        "target_mismatch",
    ):
        require(failure_policy.get(policy) == "FAIL", f"failure policy must fail closed for {policy}", errors)

    result_contract = contract.get("result_contract", {})
    require(result_contract.get("safe_metadata_only") is True, "result contract must be safe metadata only", errors)
    require(result_contract.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    for field in (
        "failover_attempt_id",
        "writer_inventory_fingerprint",
        "pause_started_at_utc",
        "quiet_window_seconds",
        "write_position_fingerprint",
        "expires_at_utc",
        "blockers",
    ):
        require(field in result_contract.get("required_fields", []), f"result contract missing {field}", errors)

    for token in (
        "DEFAULT_INVENTORY = ROOT / \"docs\" / \"backend-supabase-writer-pause-gate.json\"",
        "GATE_NAME = \"supabase_standby_writer_pause_quiescence\"",
        "ISSUE = \"ramideltoro/nutsnews#526\"",
        "EPIC = \"ramideltoro/nutsnews#521\"",
        "EXPECTED_SOURCE_LABEL = \"backend_postgres_primary\"",
        "EXPECTED_TARGET_LABEL = \"existing_production_supabase_standby\"",
        "EXPECTED_WRITER_CLASS_IDS",
        "writer_inventory_incomplete",
        "observed_write_position_advance",
        "writer_resumed_during_attempt",
        "active_writer_workflow",
        "app_worker_supabase_writes_not_blocked",
        "safe_metadata_only",
        "--enforce",
    ):
        require_token(token, script, "writer pause evaluator", errors)

    for token in (
        "DEFAULT_WORKER_API_DROPIN",
        "WORKER_API_WRITE_FLAGS",
        "NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=false",
        "NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=false",
        "pause_report",
        "resume_report",
        "drain_until_paused",
        "unknown_runtime_writers",
        "unknown_writers",
        "safe_metadata_only",
        "resume_requires_confirm_action",
    ):
        require_token(token, manager, "writer pause manager", errors)

    for token in (
        "DEFAULT_DB_URL_ENV = \"NUTSNEWS_BACKEND_PRIMARY_DB_URL\"",
        "write_position_fingerprint",
        "aggregate_and_hash_only",
        "safe_metadata_only",
        "row_checksum_sha256",
        "source_db_url_missing",
    ):
        require_token(token, position_script, "write position script", errors)
    require("print(db_url" not in position_script, "write position script must not print DB URLs", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
    ):
        require(forbidden not in script, f"evaluator must not contain credential/provisioning marker: {forbidden}", errors)
        require(forbidden not in manager, f"manager must not contain credential/provisioning marker: {forbidden}", errors)
        require(forbidden not in position_script, f"write position script must not contain credential/provisioning marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "action:",
        "pause-and-prove",
        "resume-aborted-attempt",
        "status",
        "failover_attempt_id:",
        "quiet_window_seconds:",
        "drain_timeout_seconds:",
        "confirmation:",
        "manual_freeze_confirmation:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "python3 scripts/validate_backend_supabase_standby_writer_pause_gate.py",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "echo \"::add-mask::$source_url\"",
        "nutsnews-writer-pause pause",
        "backend_postgres_write_position.py",
        "backend_supabase_standby_writer_pause_gate.py",
        "nutsnews-writer-pause resume",
        "backend-supabase-standby-writer-pause-gate",
        "if-no-files-found: ignore",
    ):
        require_token(token, workflow, "writer pause workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "backend_writer_pause_inventory_source_path",
        "backend_writer_pause_manager_path",
        "backend_writer_pause_worker_api_dropin_dir",
    ):
        require_token(token, ansible_defaults, "Ansible defaults", errors)
    require_token("Install backend writer pause boundary", ansible_main, "Ansible main tasks", errors)
    for token in (
        "Install writer pause inventory",
        "Install writer pause manager",
        "Validate writer pause manager inventory",
    ):
        require_token(token, ansible_writer_tasks, "Ansible writer pause tasks", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_writer_pause_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_writer_pause_gate",
        "python3 -m unittest tests.test_backend_writer_pause_manager",
        "python3 -m unittest tests.test_backend_postgres_write_position",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "test_complete_pause_and_stable_write_position_passes",
        "test_active_writer_fixture_fails",
        "test_unknown_writer_fixture_fails",
        "test_failed_pause_fixture_fails",
        "test_drain_timeout_fixture_fails",
        "test_resumed_writer_fixture_fails",
        "test_observed_write_fixture_fails",
        "test_incomplete_inventory_fails",
        "test_artifact_is_safe_metadata_only",
    ):
        require_token(token, tests, "writer pause evaluator tests", errors)
    for token in (
        "test_pause_installs_write_guard_dropin_and_emits_safe_status",
        "test_resume_restores_recorded_runtime_replicas_and_removes_dropin",
    ):
        require_token(token, manager_tests, "writer pause manager tests", errors)
    for token in (
        "test_snapshot_uses_safe_hashes_without_printing_sql_or_rows",
        "test_missing_database_url_fails_closed",
    ):
        require_token(token, position_tests, "write position tests", errors)

    for token in (
        "Issue #526",
        "PASS",
        "FAIL",
        "writer inventory",
        "quiet window",
        "safe metadata only",
        "existing production Supabase",
        "no new Supabase project",
        "no `nutsnews-standby` database",
        "backend PostgreSQL remains the primary read/write database",
        "pause-and-prove",
        "resume-aborted-attempt",
    ):
        require_token(token, runbook, "writer pause runbook", errors)

    require_token("runbooks/SUPABASE_STANDBY_WRITER_PAUSE_GATE.md", readme, "README", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby writer pause gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
