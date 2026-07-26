#!/usr/bin/env python3
"""Validate private backend-to-existing-Supabase sync relay guardrails."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-sync-relay.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_sync_relay.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_sync_relay.py"
SMOKE_SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_sync_relay_smoke.py"
SMOKE_TEST_PATH = ROOT / "tests" / "test_backend_supabase_sync_relay_smoke.py"
SMOKE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-sync-relay-smoke.yml"
CHECKS_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
APPLY_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
DEFAULTS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "defaults" / "main.yml"
TASKS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "standby_sync_relay.yml"
MAIN_TASKS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "main.yml"
RELATION_RE = re.compile(r"^public\.[a-z_][a-z0-9_]*$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REQUIRED_TABLES = {
    "public.admin_audit_events",
    "public.ai_usage_runs",
    "public.article_ai_reviews",
    "public.article_engagement_daily",
    "public.article_summaries",
    "public.articles",
    "public.feed_health",
    "public.migration_schema_contract",
    "public.quota_usage_events",
    "public.release_readiness",
    "public.rss_feeds",
    "public.runtime_feature_flags",
    "public.staging_fixture_runs",
    "public.staging_fixture_users",
    "public.worker_runs",
}
REQUIRED_SEQUENCES = {
    "public.ai_usage_runs_id_seq",
    "public.article_summaries_id_seq",
    "public.feed_health_id_seq",
    "public.quota_usage_events_id_seq",
    "public.rss_feeds_id_seq",
    "public.worker_runs_id_seq",
}
FORBIDDEN_VALUE_MARKERS = (
    "postgres://",
    "postgresql://",
    "password=",
    "token=",
    "service_role=",
    "sb_secret_",
    "sb_publishable_",
)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


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


def require_path(relative_path: str, field: str, errors: list[str]) -> None:
    if not relative_path:
        errors.append(f"missing path field: {field}")
        return
    if not (ROOT / relative_path).exists():
        errors.append(f"{field} points to missing file: {relative_path}")


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    script = SCRIPT_PATH.read_text(encoding="utf-8") if SCRIPT_PATH.exists() else ""
    tests = TEST_PATH.read_text(encoding="utf-8") if TEST_PATH.exists() else ""
    smoke_script = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8") if SMOKE_SCRIPT_PATH.exists() else ""
    smoke_tests = SMOKE_TEST_PATH.read_text(encoding="utf-8") if SMOKE_TEST_PATH.exists() else ""
    smoke_workflow = SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8") if SMOKE_WORKFLOW_PATH.exists() else ""
    checks = CHECKS_PATH.read_text(encoding="utf-8") if CHECKS_PATH.exists() else ""
    apply_workflow = APPLY_WORKFLOW_PATH.read_text(encoding="utf-8") if APPLY_WORKFLOW_PATH.exists() else ""
    defaults = DEFAULTS_PATH.read_text(encoding="utf-8") if DEFAULTS_PATH.exists() else ""
    relay_tasks = TASKS_PATH.read_text(encoding="utf-8") if TASKS_PATH.exists() else ""
    main_tasks = MAIN_TASKS_PATH.read_text(encoding="utf-8") if MAIN_TASKS_PATH.exists() else ""
    errors: list[str] = []

    require(contract.get("contract_id") == "backend-supabase-sync-relay", "contract_id is incorrect", errors)
    require(contract.get("version") == 1, "version must be 1", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#499", "issue must point to #499", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#223", "epic must point to #223", errors)
    require(contract.get("source_manifest", {}).get("issue") == "ramideltoro/nutsnews#497", "source manifest must point to #497", errors)

    source = contract.get("source", {})
    target = contract.get("target", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(source.get("public_5432_allowed") is False, "backend PostgreSQL must stay private", errors)
    require(source.get("network_path") == "backend_host_loopback_postgresql", "source network path must be backend loopback", errors)
    require(SECRET_RE.match(source.get("db_url_env", "")) is not None, "source db_url_env must be uppercase", errors)
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase standby", errors)
    require(target.get("existing_production_supabase_project") is True, "target must be existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project creation must stay forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database creation must stay forbidden", errors)
    require(target.get("db_url_env") == "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "target DB URL env must be production Supabase DB URL", errors)

    relay = contract.get("relay", {})
    require(relay.get("direction") == "backend-postgres-to-existing-production-supabase", "relay direction is incorrect", errors)
    require(relay.get("runtime") == "backend-host-systemd-timer", "relay runtime must be backend-host systemd timer", errors)
    require(relay.get("interval_seconds") == 30, "relay interval must be 30 seconds for #499", errors)
    require(relay.get("app_worker_supabase_write_credentials_injected") is False, "relay must not inject app/worker Supabase write credentials", errors)
    for change_type in ("insert", "update", "delete", "sequence-readiness"):
        require(change_type in relay.get("supported_change_types", []), f"relay missing change type: {change_type}", errors)
    for gate in ("schema-fingerprint-mismatch", "manifest-table-identity-missing", "unsafe-replica-identity"):
        require(gate in relay.get("fail_closed_before_target_mutation", []), f"relay missing fail-closed gate: {gate}", errors)

    safety = contract.get("safety", {})
    require(safety.get("safe_metadata_only_report") is True, "relay reports must be safe metadata only", errors)
    require(safety.get("backend_postgresql_remains_primary") is True, "backend PostgreSQL must remain primary", errors)
    require(safety.get("target_is_existing_production_supabase") is True, "target must be existing production Supabase", errors)
    require(safety.get("app_worker_writes_to_supabase_before_failover") is False, "app/worker Supabase writes must stay disabled", errors)
    require(safety.get("no_inbound_supabase_connection_to_backend_postgres") is True, "Supabase must not connect inbound to backend Postgres", errors)
    require(safety.get("service_env_file_mode") == "0640", "relay env file mode must be 0640", errors)
    require(safety.get("safe_report_file_mode") == "0644", "relay safe report file mode must be 0644", errors)
    require(safety.get("safe_report_state_dir_mode") == "0755", "relay safe report state dir mode must be 0755", errors)
    for gate in ("lag-seconds-lte-30", "table-parity-match", "schema-fingerprint-match", "sequence-safety-verified", "primary-writers-paused", "split-brain-absence-verified"):
        require(gate in safety.get("failover_requires_later_gates", []), f"missing failover gate: {gate}", errors)

    table_names = [table.get("name") for table in contract.get("tables", [])]
    require(set(table_names) == REQUIRED_TABLES, "table list must mirror the standby manifest", errors)
    for table in contract.get("tables", []):
        name = table.get("name", "")
        primary_key = table.get("primary_key", [])
        identity = table.get("replica_identity", {})
        require(RELATION_RE.match(name) is not None, f"invalid table relation: {name}", errors)
        require(isinstance(primary_key, list) and len(primary_key) >= 1, f"table {name} must declare a primary key", errors)
        require(identity.get("type") == "primary_key", f"table {name} must use primary-key replica identity", errors)
        require(identity.get("columns") == primary_key, f"table {name} replica identity must match primary key", errors)

    sequence_names = [sequence.get("name") for sequence in contract.get("sequences", [])]
    require(set(sequence_names) == REQUIRED_SEQUENCES, "sequence list must mirror the standby manifest", errors)
    for sequence in contract.get("sequences", []):
        require(RELATION_RE.match(sequence.get("name", "")) is not None, f"invalid sequence relation: {sequence.get('name')}", errors)
        require(sequence.get("table") in REQUIRED_TABLES, f"sequence {sequence.get('name')} references unknown table", errors)
        require(sequence.get("column") == "id", f"sequence {sequence.get('name')} must protect id", errors)
    require(set(contract.get("apply_order", [])) == REQUIRED_TABLES, "apply_order must cover every replicated table", errors)

    ansible = contract.get("ansible", {})
    for relative_path, field in (
        (ansible.get("playbook", ""), "ansible.playbook"),
        (ansible.get("task_file", ""), "ansible.task_file"),
        (ansible.get("protected_workflow", ""), "ansible.protected_workflow"),
    ):
        require_path(relative_path, field, errors)
    require(ansible.get("environment") == "production-backend", "relay must install through production-backend", errors)
    require(ansible.get("enable_variable") == "NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED", "enable variable is incorrect", errors)
    require(ansible.get("target_db_url_secret") == "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "target secret is incorrect", errors)

    validation = contract.get("validation", {})
    require(validation.get("local_validator") == "python3 scripts/validate_backend_supabase_sync_relay.py", "local validator command is incorrect", errors)
    require(validation.get("offline_validator") == "python3 scripts/backend_supabase_sync_relay.py --offline --enforce", "offline validator command is incorrect", errors)
    require(validation.get("unit_tests") == "python3 -m unittest tests.test_backend_supabase_sync_relay", "unit tests command is incorrect", errors)
    require_path("scripts/backend_supabase_sync_relay.py", "validation.offline_validator", errors)
    require_path("tests/test_backend_supabase_sync_relay.py", "validation.unit_tests", errors)
    require_path("scripts/backend_supabase_sync_relay_smoke.py", "validation.smoke_script", errors)
    require_path("tests/test_backend_supabase_sync_relay_smoke.py", "validation.smoke_unit_tests", errors)
    require_path(".github/workflows/backend-supabase-sync-relay-smoke.yml", "validation.smoke_workflow", errors)

    for token in (
        "relay_preflight",
        "apply_sync_once",
        "preflight_failed",
        "manifest_replica_identity_not_primary_key",
        "app_worker_supabase_write_credentials_injected",
        "backend_postgresql_remains_primary",
    ):
        require(token in script, f"relay script missing token: {token}", errors)
    for token in (
        "preflight_failed",
        "manifest_replica_identity_not_primary_key",
        "apply_table_backfill.assert_called_once",
        "app_worker_supabase_write_credentials_injected",
    ):
        require(token in tests, f"relay tests missing token: {token}", errors)
    for token in (
        "backend_supabase_sync_relay_enabled: false",
        "backend_supabase_sync_relay_interval_seconds: 30",
        "backend_supabase_sync_relay_source_db_url: \"\"",
        "backend_supabase_sync_relay_target_db_url: \"\"",
    ):
        require(token in defaults, f"defaults missing token: {token}", errors)
    for token in (
        "standby_sync_relay.yml",
        "backend_supabase_sync_relay_enabled | bool",
    ):
        require(token in main_tasks, f"main tasks missing token: {token}", errors)
    for token in (
        "nutsnews-supabase-sync-relay.service",
        "nutsnews-supabase-sync-relay.timer",
        "EnvironmentFile={{ backend_supabase_sync_relay_env_path }}",
        "--mode sync-once",
        "--enforce",
        "mode: \"0755\"",
        "safe_report_path",
        "NoNewPrivileges=true",
        "app/worker services do not receive Supabase write credentials",
    ):
        require(token in relay_tasks, f"relay Ansible tasks missing token: {token}", errors)
    for token in (
        "NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED",
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "backend_supabase_sync_relay_source_db_url",
        "backend_supabase_sync_relay_target_db_url",
        "nutsnews_migration_validation",
    ):
        require(token in apply_workflow, f"protected apply workflow missing token: {token}", errors)
    for token in (
        "prove_relay",
        "staging_fixture_runs",
        "staging_fixture_users",
        "insert_catchup",
        "update_catchup",
        "delete_catchup",
        "backend_postgres_public_5432_allowed",
        "safe_metadata_only",
        "target_database_url_is_pooler",
        "sudo",
        "-u",
        "postgres",
    ):
        require(token in smoke_script, f"smoke script missing token: {token}", errors)
    for token in (
        "test_target_query_does_not_place_database_url_in_argv",
        "test_target_url_without_sslmode_still_forces_pgsslmode_require",
        "test_rejects_explicit_non_required_sslmode",
        "test_proves_insert_update_delete_catchup_with_safe_metadata",
        "test_main_output_omits_database_url_when_blocked",
    ):
        require(token in smoke_tests, f"smoke tests missing token: {token}", errors)
    for token in (
        "workflow_dispatch:",
        "prove-backend-supabase-sync-relay",
        "production-backend",
        "runs-on: ubuntu-latest",
        "permissions:\n  contents: read",
        "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
        "NUTSNEWS_BACKEND_KNOWN_HOSTS",
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "StrictHostKeyChecking=yes",
        "ClearAllForwardings=yes",
        "RequestTTY=no",
        "backend_supabase_sync_relay_smoke.py",
        "backend-supabase-sync-relay-smoke",
    ):
        require(token in smoke_workflow, f"smoke workflow missing token: {token}", errors)
    for forbidden in (
        "ssh-keyscan",
        "actions/checkout@v",
        "contents: write",
    ):
        require(forbidden not in smoke_workflow, f"smoke workflow contains forbidden token: {forbidden}", errors)
    for token in (
        "validate_backend_supabase_sync_relay.py",
        "backend_supabase_sync_relay.py --offline --enforce",
        "python3 -m unittest tests.test_backend_supabase_sync_relay",
        "python3 -m unittest tests.test_backend_supabase_sync_relay_smoke",
    ):
        require(token in checks, f"backend checks missing token: {token}", errors)

    for value in walk_values(contract):
        if isinstance(value, str) and any(marker in value.lower() for marker in FORBIDDEN_VALUE_MARKERS):
            errors.append("contract must not contain secrets, database URLs, tokens, or key material")
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend Supabase sync relay guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
