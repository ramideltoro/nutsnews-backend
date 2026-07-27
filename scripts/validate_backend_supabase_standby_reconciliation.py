#!/usr/bin/env python3
"""Validate backend-to-existing-Supabase standby reconciliation guardrails."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "backend-supabase-standby-reconciliation.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-reconciliation.yml"
CHECKS_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_reconcile.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_reconcile.py"
REPORT_SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_reconciliation_report.py"
REPORT_TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_reconciliation_report.py"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_RECONCILIATION.md"
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
RELATION_RE = re.compile(r"^public\.[a-z_][a-z0-9_]*$")
FORBIDDEN_VALUE_MARKERS = (
    "postgres://",
    "postgresql://",
    "password=",
    "token=",
    "service_role=",
    "sb_secret_",
    "sb_publishable_",
)
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
    manifest = load_json(MANIFEST_PATH)
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8") if WORKFLOW_PATH.exists() else ""
    checks = CHECKS_PATH.read_text(encoding="utf-8") if CHECKS_PATH.exists() else ""
    script = SCRIPT_PATH.read_text(encoding="utf-8") if SCRIPT_PATH.exists() else ""
    tests = TEST_PATH.read_text(encoding="utf-8") if TEST_PATH.exists() else ""
    report_script = REPORT_SCRIPT_PATH.read_text(encoding="utf-8") if REPORT_SCRIPT_PATH.exists() else ""
    report_tests = REPORT_TEST_PATH.read_text(encoding="utf-8") if REPORT_TEST_PATH.exists() else ""
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8") if RUNBOOK_PATH.exists() else ""
    errors: list[str] = []

    require(manifest.get("contract_id") == "backend-supabase-standby-reconciliation", "contract_id is incorrect", errors)
    require(manifest.get("version") == 2, "version must be 2", errors)
    require(manifest.get("issue") == "ramideltoro/nutsnews#501", "issue must point to ramideltoro/nutsnews#501", errors)
    require(manifest.get("bootstrap_issue") == "ramideltoro/nutsnews#498", "bootstrap issue must point to #498", errors)
    require(manifest.get("epic") == "ramideltoro/nutsnews#223", "epic must point to ramideltoro/nutsnews#223", errors)
    require(manifest.get("gate_epic") == "ramideltoro/nutsnews#521", "gate epic must point to #521", errors)

    source = manifest.get("source", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(source.get("network_path") == "protected_backend_ssh_to_loopback_postgresql", "source network path must be protected SSH to loopback PostgreSQL", errors)
    require(source.get("public_5432_allowed") is False, "backend primary must not expose public 5432", errors)
    require(SECRET_RE.match(source.get("db_url_env", "")) is not None, "source db_url_env must be an uppercase env name", errors)

    target = manifest.get("target", {})
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase standby", errors)
    require(target.get("existing_production_supabase_project") is True, "target must be the existing production Supabase project", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project creation must stay forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby database creation must stay forbidden", errors)
    require(target.get("db_url_secret") == "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "target must reuse the production Supabase DB URL secret", errors)

    safety = manifest.get("safety", {})
    require(safety.get("safe_metadata_only_report") is True, "report must be safe metadata only", errors)
    require(safety.get("schema_mismatch_diagnostics") == "bounded object names and metadata hashes only", "schema mismatch diagnostics must stay safe metadata only", errors)
    require(safety.get("schema_fingerprint_normalizes_pg_not_null_constraints") is True, "schema fingerprint must normalize PostgreSQL not-null catalog constraints", errors)
    require(safety.get("app_worker_writes_to_supabase_before_failover") is False, "app/worker Supabase writes must remain blocked", errors)
    require(safety.get("supabase_as_production_provider_before_failover") is False, "Supabase provider exposure must remain failover-only", errors)
    require(set(safety.get("scheduled_report_consumes_existing_gates", [])) == {
        "ramideltoro/nutsnews#523",
        "ramideltoro/nutsnews#524",
        "ramideltoro/nutsnews#525",
    }, "scheduled report must consume #523/#524/#525 gates", errors)
    require(safety.get("scheduled_report_mutates_supabase") is False, "scheduled report must not mutate Supabase", errors)
    for gate in ("lag-seconds-lte-30", "table-parity-match", "schema-fingerprint-match", "sequence-safety-verified", "primary-writers-paused", "split-brain-absence-verified"):
        require(gate in safety.get("failover_requires_later_gates", []), f"missing failover gate: {gate}", errors)
    for check in ("schema-fingerprint", "required-object-list", "table-row-count-parity", "table-row-checksum-parity", "sequence-last-value-gte-source", "sequence-next-value-gt-source-and-target-max-id"):
        require(check in manifest.get("required_checks", []), f"required checks missing {check}", errors)

    table_names = [table.get("name") for table in manifest.get("tables", [])]
    require(set(table_names) == REQUIRED_TABLES, "table list must mirror the app standby manifest", errors)
    for table in manifest.get("tables", []):
        require(RELATION_RE.match(table.get("name", "")) is not None, f"invalid table relation: {table.get('name')}", errors)
        require(len(table.get("primary_key", [])) >= 1, f"table {table.get('name')} must declare a primary key", errors)

    sequence_names = [sequence.get("name") for sequence in manifest.get("sequences", [])]
    require(set(sequence_names) == REQUIRED_SEQUENCES, "sequence list must mirror the app standby manifest", errors)
    for sequence in manifest.get("sequences", []):
        require(RELATION_RE.match(sequence.get("name", "")) is not None, f"invalid sequence relation: {sequence.get('name')}", errors)
        require(sequence.get("table") in REQUIRED_TABLES, f"sequence {sequence.get('name')} references an unknown table", errors)
        require(sequence.get("column") == "id", f"sequence {sequence.get('name')} must protect id", errors)

    backfill = manifest.get("backfill", {})
    require(backfill.get("mode") == "protected_workflow_only", "backfill must be protected-workflow only", errors)
    require(backfill.get("confirmation") == "backfill-existing-production-supabase-from-backend-primary", "backfill confirmation is incorrect", errors)
    require(backfill.get("deletes_target_extra_rows") is True, "backfill must delete target rows absent from the backend source snapshot", errors)
    require(backfill.get("mirrors_primary_key_values") is True, "backfill must mirror primary key values for checksum parity", errors)
    require(backfill.get("repairs_check_constraints_from_source") is True, "backfill must repair check constraints from the backend source", errors)
    require(set(backfill.get("table_order", [])) == REQUIRED_TABLES, "backfill table order must cover each table exactly once", errors)
    require(backfill.get("table_order", []).index("public.articles") < backfill.get("table_order", []).index("public.article_summaries"), "articles must backfill before article_summaries", errors)
    require(backfill.get("table_order", []).index("public.staging_fixture_runs") < backfill.get("table_order", []).index("public.staging_fixture_users"), "fixture runs must backfill before fixture users", errors)

    workflow_contract = manifest.get("workflow", {})
    require_path(workflow_contract.get("path", ""), "workflow.path", errors)
    require(workflow_contract.get("environment") == "production-backend", "workflow must use production-backend", errors)
    require(workflow_contract.get("runs_on") == "ubuntu-latest", "workflow must stay GitHub-hosted", errors)
    require(set(workflow_contract.get("triggers", [])) == {"workflow_dispatch", "schedule"}, "workflow must run manually and on a schedule", errors)
    require(workflow_contract.get("schedule_cron") == "37 */6 * * *", "workflow schedule cron is incorrect", errors)
    require(set(workflow_contract.get("modes", [])) == {"report", "apply-backfill"}, "workflow modes are incorrect", errors)
    require(workflow_contract.get("scheduled_report_confirmation") == "scheduled-existing-production-supabase-standby-reconciliation", "scheduled report confirmation is incorrect", errors)
    require(workflow_contract.get("app_worker_failover_changes") is False, "workflow must not expose app/worker failover changes", errors)
    require(workflow_contract.get("artifact") == "backend-supabase-standby-reconciliation", "artifact name is incorrect", errors)
    require(workflow_contract.get("gate_artifact") == "backend-supabase-standby-reconciliation-gates", "gate artifact name is incorrect", errors)

    report = manifest.get("report", {})
    require(report.get("mode") == "safe_metadata_gate_aggregate", "report mode must aggregate safe gate metadata", errors)
    require(report.get("script") == "scripts/backend_supabase_standby_reconciliation_report.py", "report script path is incorrect", errors)
    require(report.get("mutates") is False, "report mode must not mutate", errors)
    require(report.get("safe_evidence_only") is True, "report mode must be safe-evidence only", errors)
    require(set(report.get("required_gate_issues", [])) == {
        "ramideltoro/nutsnews#523",
        "ramideltoro/nutsnews#524",
        "ramideltoro/nutsnews#525",
    }, "report must require #523/#524/#525 gate artifacts", errors)

    validation = manifest.get("validation", {})
    require(validation.get("local_validator") == "python3 scripts/validate_backend_supabase_standby_reconciliation.py", "local validator command is incorrect", errors)
    require(validation.get("report_aggregator") == "python3 scripts/backend_supabase_standby_reconciliation_report.py", "report aggregator command is incorrect", errors)
    require(validation.get("report_unit_tests") == "python3 -m unittest tests.test_backend_supabase_standby_reconciliation_report", "report unit test command is incorrect", errors)
    require(validation.get("offline_validator") == "python3 scripts/backend_supabase_standby_reconcile.py --offline --enforce", "offline validator command is incorrect", errors)
    require(validation.get("unit_tests") == "python3 -m unittest tests.test_backend_supabase_standby_reconcile", "unit test command is incorrect", errors)

    for relative_path, field in (
        ("scripts/validate_backend_supabase_standby_reconciliation.py", "validation.local_validator"),
        ("scripts/backend_supabase_standby_reconciliation_report.py", "validation.report_aggregator"),
        ("tests/test_backend_supabase_standby_reconciliation_report.py", "validation.report_unit_tests"),
        ("scripts/backend_supabase_standby_reconcile.py", "validation.offline_validator"),
        ("tests/test_backend_supabase_standby_reconcile.py", "validation.unit_tests"),
        ("runbooks/SUPABASE_STANDBY_RECONCILIATION.md", "runbook"),
    ):
        require_path(relative_path, field, errors)

    workflow_required = [
        "workflow_dispatch:",
        "schedule:",
        "cron: \"37 */6 * * *\"",
        "mode:",
        "- report",
        "- apply-backfill",
        "confirmation:",
        "reconciliation_attempt_id:",
        "enforce:",
        "production-backend",
        "scheduled-existing-production-supabase-standby-reconciliation",
        "RECONCILIATION_ATTEMPT_ID",
        "backend_health_report.py",
        "backend_supabase_standby_parity_gate.py",
        "backend_supabase_standby_schema_gate.py",
        "backend_supabase_standby_sequence_gate.py",
        "backend_supabase_standby_reconciliation_report.py",
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
        "NUTSNEWS_BACKEND_KNOWN_HOSTS",
        "NUTSNEWS_STANDBY_RECONCILE_CONFIRMATION",
        "backend_supabase_standby_reconcile.py",
        "backend-supabase-standby-reconciliation",
        "backend-supabase-standby-reconciliation-gates",
        "actions/upload-artifact@",
        "echo \"::add-mask::$NUTSNEWS_PRODUCTION_SUPABASE_DB_URL\"",
    ]
    for token in workflow_required:
        require(token in workflow, f"workflow missing required token: {token}", errors)
    require("refs/heads/main" in workflow, "workflow must require main branch dispatch", errors)
    require("runs-on: ubuntu-latest" in workflow, "workflow must run on GitHub-hosted ubuntu-latest", errors)
    require("self-hosted" not in workflow, "workflow must not require self-hosted runners", errors)
    require("NUTSNEWS_STANDBY_SUPABASE_DB_URL" not in workflow, "backend workflow must use production Supabase DB URL, not app standby alias", errors)
    require("nutsnews-standby" not in workflow.lower(), "workflow must not create or target a nutsnews-standby database", errors)
    require("create project" not in workflow.lower(), "workflow must not create Supabase projects", errors)

    for token in (
        "backend_supabase_standby_reconcile.py --offline --enforce",
        "validate_backend_supabase_standby_reconciliation.py",
        "python3 -m unittest tests.test_backend_supabase_standby_reconcile",
        "python3 -m unittest tests.test_backend_supabase_standby_reconciliation_report",
    ):
        require(token in checks, f"backend checks missing {token}", errors)
    for token in (
        "NUTSNEWS_STANDBY_RECONCILE_CONFIRMATION",
        "primary_key_values_mirrored",
        "check_constraint_repair_failed",
        "con.contype <> 'n'",
        "schema_diff",
        "safe_metadata_only",
        "app_worker_writes_to_supabase_before_failover",
    ):
        require(token in script, f"script missing safety token: {token}", errors)
    for token in (
        "REPORT_ID = \"backend-supabase-standby-reconciliation-report\"",
        "ISSUE = \"ramideltoro/nutsnews#501\"",
        "EPIC = \"ramideltoro/nutsnews#223\"",
        "GATE_EPIC = \"ramideltoro/nutsnews#521\"",
        "ramideltoro/nutsnews#523",
        "ramideltoro/nutsnews#524",
        "ramideltoro/nutsnews#525",
        "required-table-row-count-parity",
        "required-table-row-checksum-parity",
        "schema-fingerprint-compatible",
        "required-object-list-compatible",
        "sequence-safety",
        "target_is_existing_production_supabase",
        "create_new_supabase_project",
        "create_nutsnews_standby_database",
        "app_worker_writes_to_supabase_before_failover",
        "--enforce",
    ):
        require(token in report_script, f"report script missing safety token: {token}", errors)
    for token in ("row_checksum_mismatch", "target_next_value_not_above_source_max_id", "apply_confirmation_missing"):
        require(token in tests, f"tests missing coverage token: {token}", errors)
    for token in (
        "test_passing_gates_emit_pass_report",
        "test_failed_required_table_blocks_reconciliation",
        "test_mismatched_attempt_fails_closed",
        "test_expired_gate_fails_closed",
        "test_enforce_returns_nonzero_on_failure",
    ):
        require(token in report_tests, f"report tests missing coverage token: {token}", errors)
    for token in (
        "Issue #501",
        "schedule",
        "workflow_dispatch",
        "safe metadata",
        "#523",
        "#524",
        "#525",
        "existing production Supabase",
        "does not create a new Supabase project",
        "does not create a `nutsnews-standby` database",
        "apply-backfill",
    ):
        require(token in runbook, f"runbook missing token: {token}", errors)

    for value in walk_values(manifest):
        if isinstance(value, str) and any(marker in value.lower() for marker in FORBIDDEN_VALUE_MARKERS):
            errors.append("manifest must not contain secrets, database URLs, tokens, or key material")
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend Supabase standby reconciliation guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
