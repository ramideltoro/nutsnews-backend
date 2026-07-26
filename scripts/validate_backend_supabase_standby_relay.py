#!/usr/bin/env python3
"""Validate backend-to-Supabase standby relay guardrails."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-relay.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_relay.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_relay.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-relay.yml"
CHECKS_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
PLAYBOOK_PATH = ROOT / "ansible" / "playbooks" / "backend_supabase_standby_relay.yml"
ROLE_PATH = ROOT / "ansible" / "roles" / "backend_supabase_standby_relay"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_RELAY.md"
README_PATH = ROOT / "README.md"
ANSIBLE_README_PATH = ROOT / "ansible" / "README.md"
FORBIDDEN_VALUE_MARKERS = (
    "postgres://",
    "postgresql://",
    "password=",
    "service_role=",
    "sb_secret_",
    "sb_publishable_",
)
RELATION_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)
    else:
        yield value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def require_path(path: Path, label: str, errors: list[str]) -> None:
    require(path.exists(), f"missing {label}: {path.relative_to(ROOT)}", errors)


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    script = read(SCRIPT_PATH)
    tests = read(TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks = read(CHECKS_PATH)
    playbook = read(PLAYBOOK_PATH)
    tasks = read(ROLE_PATH / "tasks" / "main.yml")
    defaults = read(ROLE_PATH / "defaults" / "main.yml")
    service = read(ROLE_PATH / "templates" / "nutsnews-supabase-standby-relay.service.j2")
    timer = read(ROLE_PATH / "templates" / "nutsnews-supabase-standby-relay.timer.j2")
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    ansible_readme = read(ANSIBLE_README_PATH)
    errors: list[str] = []

    for path, label in (
        (SCRIPT_PATH, "relay script"),
        (TEST_PATH, "relay tests"),
        (WORKFLOW_PATH, "relay workflow"),
        (PLAYBOOK_PATH, "relay playbook"),
        (ROLE_PATH / "tasks" / "main.yml", "relay role tasks"),
        (ROLE_PATH / "defaults" / "main.yml", "relay role defaults"),
        (RUNBOOK_PATH, "relay runbook"),
    ):
        require_path(path, label, errors)

    require(contract.get("contract_id") == "backend-supabase-standby-relay", "contract_id is incorrect", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#499", "issue must be #499", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#223", "epic must be #223", errors)
    require(contract.get("reconciliation_prerequisite", {}).get("issue") == "ramideltoro/nutsnews#498", "#498 prerequisite must be recorded", errors)

    architecture = contract.get("architecture", {})
    require(architecture.get("mode") == "backend-local-trigger-ledger-relay", "relay architecture mode is incorrect", errors)
    require(architecture.get("source") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(architecture.get("target") == "existing_production_supabase_standby", "target must be existing production Supabase standby", errors)
    require(architecture.get("backend_postgres_public_5432_allowed") is False, "public backend 5432 must stay forbidden", errors)
    require(architecture.get("inbound_supabase_to_backend_connection_required") is False, "inbound Supabase-to-backend must not be required", errors)
    require(architecture.get("pooler_allowed") is False, "Supabase pooler must not be used for direct standby relay", errors)

    capture = contract.get("source_capture", {})
    require(capture.get("trigger_operations") == ["insert", "update", "delete"], "relay must capture inserts, updates, and deletes", errors)
    require(capture.get("prints_raw_rows") is False, "relay must not print raw rows", errors)
    require(capture.get("prints_database_errors") is False, "relay must not print PostgreSQL errors", errors)
    require(capture.get("prints_connection_strings") is False, "relay must not print connection strings", errors)

    runtime = contract.get("runtime", {})
    for key in ("os_user", "service_unit", "timer_unit", "script_path", "config_dir", "state_dir", "lock_dir"):
        require(runtime.get(key), f"runtime.{key} is required", errors)
    for relation in (capture.get("schema", ""), capture.get("event_table", ""), runtime.get("os_user", "").replace("-", "_")):
        require(RELATION_RE.match(relation) is not None, f"unsafe relation/name marker: {relation}", errors)
    for hardening in ("NoNewPrivileges", "ProtectSystem=strict", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX"):
        require(hardening in runtime.get("systemd_hardening", []), f"missing systemd hardening marker: {hardening}", errors)

    secret_contract = contract.get("secret_contract", {})
    require(secret_contract.get("environment") == "production-backend", "relay workflow must use production-backend", errors)
    require(secret_contract.get("source_password_secret") == "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD", "source password secret is incorrect", errors)
    require(secret_contract.get("target_db_url_secret") == "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "target DB URL secret is incorrect", errors)
    require(secret_contract.get("database_urls_printed") is False, "database URLs must not be printed", errors)
    require(secret_contract.get("secrets_persisted_on_backend", {}).get("mode") == "0640", "backend relay env file must be 0640", errors)

    fail_closed = contract.get("fail_closed", {})
    for key in (
        "schema_mismatch_blocks_apply",
        "unsafe_source_table_identity_blocks_apply",
        "unsafe_target_table_identity_blocks_apply",
        "target_apply_failure_does_not_ack_source_events",
        "locked_single_execution",
        "safe_metadata_only_report",
    ):
        require(fail_closed.get(key) is True, f"fail_closed.{key} must be true", errors)

    workflow_contract = contract.get("workflow", {})
    require(workflow_contract.get("path") == ".github/workflows/backend-supabase-standby-relay.yml", "workflow path is incorrect", errors)
    require(workflow_contract.get("environment") == "production-backend", "workflow environment is incorrect", errors)
    require(workflow_contract.get("runs_on") == "ubuntu-latest", "workflow must run on ubuntu-latest", errors)
    require(set(workflow_contract.get("modes", [])) == {"check", "apply"}, "workflow modes are incorrect", errors)
    require(set(workflow_contract.get("states", [])) == {"present", "absent"}, "workflow states are incorrect", errors)

    for command in (
        "python3 scripts/validate_backend_supabase_standby_relay.py",
        "python3 scripts/backend_supabase_standby_relay.py --mode offline --enforce",
        "python3 -m unittest tests.test_backend_supabase_standby_relay",
        "ansible-playbook playbooks/backend_supabase_standby_relay.yml --syntax-check",
    ):
        require(command in checks, f"backend checks missing command: {command}", errors)

    for token in (
        "record_change",
        "fetch_batch",
        "ack_events",
        "sequence_snapshot",
        "after insert or update or delete",
        "target_apply_failure_does_not_ack_source_events",
        "source_unsafe_table_identity",
        "target_unsafe_table_identity",
        "database_url_blockers",
        "target_database_url_is_pooler",
        "source_database_not_loopback",
        "safe_metadata_only",
        "fcntl.flock",
        "jsonb_populate_record",
        "on conflict",
        "delete from",
        "setval",
    ):
        require(token in script, f"relay script missing token: {token}", errors)

    for token in (
        "test_offline_report_is_safe_metadata_only",
        "test_source_install_sql_captures_insert_update_delete",
        "test_target_apply_failure_does_not_ack_source_events",
        "test_schema_identity_mismatch_blocks_run",
        "test_sequence_advance_uses_safe_counts",
    ):
        require(token in tests, f"relay tests missing coverage: {token}", errors)

    for token in (
        "workflow_dispatch:",
        "run_mode:",
        "relay_state:",
        "confirm_apply:",
        "refs/heads/main",
        "production-backend",
        "runs-on: ubuntu-latest",
        "permissions:\n  contents: read",
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD",
        "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
        "NUTSNEWS_BACKEND_KNOWN_HOSTS",
        "playbooks/backend_supabase_standby_relay.yml",
        "--extra-vars",
        "backend.nutsnews.com",
        "backend-supabase-standby-relay",
    ):
        require(token in workflow, f"workflow missing guardrail token: {token}", errors)
    for forbidden in (
        "self-hosted",
        "pooler.supabase.com",
        "NUTSNEWS_STANDBY_SUPABASE_DB_URL",
        "ssh-keyscan",
        "actions/checkout@v",
        "contents: write",
    ):
        require(forbidden not in workflow, f"workflow contains forbidden token: {forbidden}", errors)

    require("backend_supabase_standby_relay" in playbook, "playbook must include relay role", errors)
    for token in (
        "nutsnews-standby-relay",
        "backend_supabase_standby_relay_target_db_url",
        "no_log: true",
        "become_user: postgres",
        "--mode install-source",
        "--mode remove-source",
        "systemd",
        "nutsnews-supabase-standby-relay.timer",
    ):
        require(token in tasks + defaults, f"Ansible role missing token: {token}", errors)

    for token in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "EnvironmentFile=",
        "--mode run-once",
        "User={{ backend_supabase_standby_relay_user }}",
    ):
        require(token in service, f"service template missing token: {token}", errors)
    require("OnCalendar={{ backend_supabase_standby_relay_calendar }}" in timer, "timer template must use managed calendar", errors)

    for token in (
        "ramideltoro/nutsnews#499",
        "backend-local trigger ledger",
        "check mode before apply",
        "insert, update, delete",
        "sequence readiness",
        "rollback",
        "safe metadata only",
    ):
        require(token in runbook, f"runbook missing token: {token}", errors)
    require("SUPABASE_STANDBY_RELAY.md" in readme, "README must link relay runbook", errors)
    require("backend_supabase_standby_relay.yml" in ansible_readme, "Ansible README must document relay playbook", errors)

    for value in walk_values(contract):
        if isinstance(value, str) and any(marker in value.lower() for marker in FORBIDDEN_VALUE_MARKERS):
            errors.append("contract must not contain secrets, database URLs, tokens, or key material")
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend Supabase standby relay guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
