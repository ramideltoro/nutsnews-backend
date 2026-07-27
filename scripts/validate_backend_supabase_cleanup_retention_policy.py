#!/usr/bin/env python3
"""Validate Supabase cleanup retention policy guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs" / "backend-supabase-cleanup-retention-policy.json"
LOGICAL_REPLICATION_PLAN = ROOT / "docs" / "backend-postgres-logical-replication-plan.json"
PROVIDER_SWITCH = ROOT / "docs" / "backend-database-provider-switch.json"
FUTURE_TOPOLOGY = ROOT / "docs" / "backend-postgres-future-primary-topology.json"
PARITY_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-parity.json"
WORKFLOW = ROOT / ".github" / "workflows" / "backend-postgres-logical-replication.yml"
SOURCE_SCRIPT = ROOT / "scripts" / "backend_postgres_logical_replication_source.py"
TARGET_SCRIPT = ROOT / "scripts" / "backend_postgres_logical_replication_target_remote.sh"
RUNBOOK = ROOT / "runbooks" / "DB_MIGRATION_LOGICAL_REPLICATION.md"
PLATFORM_RUNBOOK = ROOT / "runbooks" / "SUPABASE_PLATFORM_PARITY.md"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
README = ROOT / "README.md"
TESTS = ROOT / "tests" / "test_backend_supabase_cleanup_retention_policy.py"


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
    policy = load_json(POLICY_PATH)
    logical = load_json(LOGICAL_REPLICATION_PLAN)
    provider = load_json(PROVIDER_SWITCH)
    topology = load_json(FUTURE_TOPOLOGY)
    parity = load_json(PARITY_MANIFEST)
    workflow = read(WORKFLOW)
    source_script = read(SOURCE_SCRIPT)
    target_script = read(TARGET_SCRIPT)
    runbook = read(RUNBOOK)
    platform_runbook = read(PLATFORM_RUNBOOK)
    checks = read(CHECKS_WORKFLOW)
    readme = read(README)
    tests = read(TESTS)
    errors: list[str] = []

    require(policy.get("schema_version") == 1, "policy schema_version must be 1", errors)
    require(policy.get("policy_id") == "backend-supabase-cleanup-retention-policy", "policy_id mismatch", errors)
    require(policy.get("tracking_issue") == "ramideltoro/nutsnews#506", "policy must point to #506", errors)
    require(policy.get("parent_issue") == "ramideltoro/nutsnews#223", "policy must point to #223", errors)
    require(policy.get("standby_acceptance_issue") == "ramideltoro/nutsnews#505", "policy must point to #505", errors)
    evidence = policy.get("standby_acceptance_evidence", {})
    require(evidence.get("decision") == "GO", "policy must link accepted #505 GO evidence", errors)
    require("30236434040" in evidence.get("protected_acceptance_run", ""), "policy must link protected #505 run", errors)

    production = policy.get("production_database_policy", {})
    require(production.get("normal_primary") == "backend_postgres_primary", "backend PostgreSQL must remain normal primary", errors)
    require(production.get("retained_hot_standby") == "existing_production_supabase_standby", "retained standby target mismatch", errors)
    require(production.get("supabase_is_retained_as_hot_standby") is True, "Supabase must be retained as hot standby", errors)
    require(production.get("supabase_is_not_blindly_retired") is True, "policy must forbid blind Supabase retirement", errors)
    require(production.get("create_new_supabase_project") is False, "new Supabase project must be forbidden", errors)
    require(production.get("create_nutsnews_standby_database") is False, "nutsnews-standby database must be forbidden", errors)

    allowed_cleanup = "\n".join(json.dumps(item, sort_keys=True) for item in policy.get("allowed_cleanup_after_standby_acceptance", []))
    for token in (
        "obsolete_supabase_to_backend_migration_publication_slot_subscription",
        "nutsnews_backend_migration_",
        "standby acceptance #505 GO evidence",
        "explicit #114 or later owner-approved cleanup issue",
        "migration_only_replication_credentials",
    ):
        require_token(token, allowed_cleanup, "allowed cleanup policy", errors)

    retained = "\n".join(policy.get("retained_standby_resources", []))
    for token in (
        "existing production Supabase project and database",
        "supabase-standby GitHub Environment",
        "NUTSNEWS_STANDBY_SUPABASE_DB_URL",
        "backend-to-Supabase sync relay service",
        "standby lag, parity, schema, sequence, writer-pause, split-brain",
    ):
        require_token(token, retained, "retained standby resources", errors)

    forbidden = "\n".join(policy.get("forbidden_without_new_owner_approval", []))
    for token in (
        "delete the supabase-standby GitHub Environment",
        "delete or blank NUTSNEWS_STANDBY_SUPABASE_* secrets",
        "disable or uninstall nutsnews-supabase-sync-relay.service",
        "drop existing production Supabase schemas",
        "route app or worker writes to Supabase outside the approved failover workflow",
    ):
        require_token(token, forbidden, "forbidden cleanup policy", errors)

    workflow_guardrails = policy.get("cleanup_workflow_guardrails", {})
    require(workflow_guardrails.get("allowed_teardown_scope") == "obsolete_supabase_to_backend_migration_logical_replication_only", "workflow cleanup scope mismatch", errors)
    require(workflow_guardrails.get("allowed_resource_prefix") == "nutsnews_backend_migration_", "workflow cleanup prefix mismatch", errors)
    require(workflow_guardrails.get("must_preserve_hot_standby_resources") is True, "workflow must preserve hot standby resources", errors)
    require(workflow_guardrails.get("protected_environment") == "production-backend", "cleanup workflow must stay protected", errors)
    require(workflow_guardrails.get("safe_metadata_only") is True, "cleanup workflow must stay safe metadata only", errors)

    require(logical.get("cleanup_retention_policy") == "docs/backend-supabase-cleanup-retention-policy.json", "logical replication plan must link cleanup policy", errors)
    teardown = logical.get("post_cutover_teardown", {})
    require(teardown.get("cleanup_policy_issue") == "ramideltoro/nutsnews#506", "logical teardown must point to #506", errors)
    require(teardown.get("allowed_teardown_scope") == "obsolete_supabase_to_backend_migration_logical_replication_only", "logical teardown scope mismatch", errors)
    require(teardown.get("allowed_resource_prefix") == "nutsnews_backend_migration_", "logical teardown prefix mismatch", errors)
    must_preserve = "\n".join(teardown.get("must_preserve", []))
    for token in ("existing production Supabase hot standby", "NUTSNEWS_STANDBY_SUPABASE", "backend-to-Supabase standby sync relay"):
        require_token(token, must_preserve, "logical teardown preserve policy", errors)
    logical_text = json.dumps(logical)
    require("Supabase archive or retirement decision" not in logical_text, "logical plan must not require blind Supabase archive/retirement", errors)

    post_cutover = provider.get("post_cutover_status", {})
    require(provider.get("status") == "production_primary_cutover_complete_supabase_hot_standby_retained", "provider switch status must retain hot standby", errors)
    require(post_cutover.get("standby_retention_issue") == "ramideltoro/nutsnews#506", "provider switch must link #506", errors)
    require("retirement_pending" not in post_cutover, "provider switch must not keep retirement_pending cleanup list", errors)
    require("retained_standby" in post_cutover, "provider switch must list retained standby resources", errors)

    cutover = topology.get("cutover_and_retirement_gates", {})
    require(cutover.get("standby_retention_issue") == "ramideltoro/nutsnews#506", "topology must link #506", errors)
    require(cutover.get("supabase_hot_standby_retained_after_cutover") is True, "topology must retain hot standby", errors)
    require(cutover.get("cleanup_scope") == "obsolete_migration_resources_only", "topology cleanup scope mismatch", errors)

    parity_status = parity.get("post_cutover_status", {})
    require(parity_status.get("standby_retention_issue") == "ramideltoro/nutsnews#506", "parity manifest must link #506", errors)
    require(parity_status.get("supabase_role") == "existing_production_supabase_hot_standby_retained_after_issue_505_506", "parity Supabase role must be hot standby", errors)
    require(parity_status.get("cleanup_scope") == "obsolete_supabase_to_backend_migration_resources_only", "parity cleanup scope mismatch", errors)

    for token in (
        "CLEANUP_SCOPE = \"obsolete_supabase_to_backend_migration_logical_replication_only\"",
        "PRESERVED_HOT_STANDBY_RESOURCES",
        "\"allowed_cleanup_resource_prefix\": \"nutsnews_backend_migration_\"",
        "\"preserved_hot_standby_resources\": PRESERVED_HOT_STANDBY_RESOURCES",
    ):
        require_token(token, source_script, "source teardown script", errors)
    for token in (
        "\"cleanup_scope\": \"obsolete_supabase_to_backend_migration_logical_replication_only\"",
        "\"allowed_cleanup_resource_prefix\": \"nutsnews_backend_migration_\"",
        "\"preserved_hot_standby_resources\"",
        "backend_to_supabase_sync_relay_service_timer_env_contract_reports",
    ):
        require_token(token, target_script, "target teardown script", errors)

    for token in (
        "Teardown scope is obsolete Supabase-to-backend migration logical replication only.",
        "Existing production Supabase hot standby",
        "backend-to-Supabase sync relay resources are preserved",
    ):
        require_token(token, workflow, "logical replication workflow", errors)

    for token in (
        "Issue #506",
        "existing production Supabase is retained as the hot standby",
        "nutsnews_backend_migration_",
        "Do not remove:",
        "backend-to-Supabase standby sync relay",
        "#505 acceptance evidence",
    ):
        require_token(token, runbook, "logical replication runbook", errors)
    for token in (
        "standby credentials",
        "backend-to-Supabase standby relay credentials",
        "#505/#506",
    ):
        require_token(token, platform_runbook, "Supabase platform parity runbook", errors)

    require_token("validate_backend_supabase_cleanup_retention_policy.py", checks, "backend checks workflow", errors)
    require_token("tests.test_backend_supabase_cleanup_retention_policy", checks, "backend checks workflow", errors)
    require_token("SUPABASE_CLEANUP_RETENTION_POLICY.md", readme, "README", errors)
    for token in (
        "test_policy_validator_passes",
        "test_source_teardown_report_preserves_standby_resources",
        "test_policy_forbids_blind_supabase_retirement",
    ):
        require_token(token, tests, "cleanup retention tests", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase cleanup retention policy guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
