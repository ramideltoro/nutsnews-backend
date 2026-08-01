#!/usr/bin/env python3
"""Fail-closed source validator for #126 cutover controls and standing authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs/worker-uplift-cutover-controls.json"
DECISION_PATH = ROOT / "docs/worker-uplift-final-cutover-decision.json"
WORKFLOW_PATH = ROOT / ".github/workflows/backend-worker-uplift-cutover-controls.yml"
MANAGER_PATH = ROOT / "scripts/worker_uplift_cutover_control.py"
SQL_PATH = ROOT / "ansible/roles/backend_baseline/templates/worker-uplift-shadow-data-model.sql.j2"
ANSIBLE_PATH = ROOT / "ansible/roles/backend_worker_runtime/tasks/main.yml"
APPLY_PATH = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
CHECKS_PATH = ROOT / ".github/workflows/backend-checks.yml"
INVENTORY_PATH = ROOT / "docs/backend-credential-inventory.json"
PINNED_SCOPE_SHA256 = "17dffe06f80ec9266761a84a2c738517c57da31e57ad8936dce16d003c021804"
OWNER_COMMENT_BODY_SHA256 = "654dd45f2a425ca59a5115cb1ce0fd9d1e87488682b103d52673c9c2cff19544"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def scope_payload(document: dict[str, Any]) -> dict[str, Any]:
    authority = document["standing_authorization"]
    return {
        "authorized_operations": authority["authorized_operations"],
        "authorized_environment": authority["authorized_environment"],
        "authorized_typed_confirmations": authority["authorized_typed_confirmations"],
        "excluded_authorities": authority["excluded_authorities"],
        "current_required_state": document["current_required_state"],
        "database_target": document["database_control"]["target"],
        "database_role": document["database_control"]["role"],
        "database_allowed_statements": document["database_control"]["allowed_statements"],
        "workflow_path": document["workflow"]["path"],
        "workflow_concurrency_group": document["workflow"]["concurrency_group"],
        "workflow_mutation_modes": document["workflow"]["mutation_modes"],
        "workflow_routine_modes": document["workflow"]["routine_modes"],
    }


def validate_contract(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority = document.get("standing_authorization", {})
    evidence = authority.get("owner_evidence", {})
    expected_operations = ["preflight", "dry-run", "rehearse", "verify", "deploy-safe-controls"]
    required_exclusions = {
        "issue-166-go", "issue-127-execution", "cutover", "production-writes",
        "ingestion-ownership-change", "legacy-ingestion-disable", "dns-change",
        "failover-change", "cloudflare-change", "arbitrary-sql", "risk-acceptance",
    }
    if authority.get("authorized_owner_login") != "ramideltoro":
        errors.append("standing authorization owner must be ramideltoro")
    if evidence.get("url") != "https://github.com/ramideltoro/nutsnews-worker/issues/126#issuecomment-5150510712":
        errors.append("standing authorization must bind the immutable owner comment")
    if evidence.get("body_sha256") != OWNER_COMMENT_BODY_SHA256:
        errors.append("standing authorization comment digest changed")
    if authority.get("authorized_operations") != expected_operations:
        errors.append("standing authorization operation set changed")
    if authority.get("authorized_environment") != "production-backend":
        errors.append("standing authorization environment changed")
    if not required_exclusions.issubset(set(authority.get("excluded_authorities", []))):
        errors.append("standing authorization lost a required exclusion")
    if any(authority.get(field) is not False for field in (
        "per_release_owner_approval_required", "first_run_owner_approval_required",
        "routine_environment_wait_owner_approval_required",
    )):
        errors.append("routine bounded operations must use standing authorization")
    try:
        actual = canonical_sha256(scope_payload(document))
    except (KeyError, TypeError):
        return errors + ["standing authorization scope is incomplete"]
    if actual != PINNED_SCOPE_SHA256 or authority.get("scope_sha256") != PINNED_SCOPE_SHA256:
        errors.append("standing authorization scope fingerprint changed; fail closed")
    if document.get("current_required_state") != {
        "active_ingestion_owner": "legacy_shards", "legacy_dispatch_enabled": True,
        "uplift_mode": "shadow", "uplift_production_writes_enabled": False,
        "uplift_scheduler_enabled": True, "dns_failover_unchanged": True,
    }:
        errors.append("committed required state must remain safe shadow")
    database = document.get("database_control", {})
    if database.get("target") != "worker_uplift_final.cutover_control" or database.get("row_id") != "production":
        errors.append("database mutation target must remain the single production control row")
    if database.get("role") != "nutsnews_worker_uplift_cutover":
        errors.append("database control role changed")
    if document.get("workflow", {}).get("mutation_modes") != ["apply", "rollback"]:
        errors.append("workflow mutation mode set changed")
    return errors


def require_text(errors: list[str], text: str, needles: list[str], source: str) -> None:
    for needle in needles:
        if needle not in text:
            errors.append(f"{source} missing required enforcement: {needle}")


def validate_repository() -> list[str]:
    try:
        contract = json.loads(CONTRACT_PATH.read_text())
        decision = json.loads(DECISION_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"could not load cutover control JSON: {exc.__class__.__name__}"]
    errors = validate_contract(contract)
    workflow = WORKFLOW_PATH.read_text()
    manager = MANAGER_PATH.read_text()
    sql = SQL_PATH.read_text()
    ansible = ANSIBLE_PATH.read_text()
    apply = APPLY_PATH.read_text()
    checks = CHECKS_PATH.read_text()
    inventory = json.loads(INVENTORY_PATH.read_text())
    require_text(errors, workflow, [
        "group: backend-worker-uplift-cutover-controls", "environment: production-backend",
        "execute-worker-uplift-cutover:$CANDIDATE", "rollback-worker-uplift-cutover:$WATERMARK",
        "docs/worker-uplift-final-cutover-decision.json", "retention-days: 90",
        "controller-ingestion-scheduling-operations.yml", "NUTSNEWS_MAINTENANCE_GITHUB_TOKEN",
    ], "protected workflow")
    require_text(errors, manager, [
        "where control_id = 'production'", "and generation = %s", "and state = %s",
        "validate_final_decision", "exact #166 GO is absent", "dns_failover_unchanged",
        "production_targets_reachable",
    ], "fixed manager")
    if "shell=True" in manager or "os.system(" in manager:
        errors.append("fixed manager must not expose arbitrary shell execution")
    require_text(errors, sql, [
        "CREATE TABLE IF NOT EXISTS %I.cutover_control", "CHECK (NOT uplift_production_writes_enabled OR NOT legacy_dispatch_enabled)",
        "REVOKE ALL PRIVILEGES ON ALL TABLES", "GRANT UPDATE (generation, state",
        "cutover_control_audit", "SECURITY DEFINER",
        "REVOKE ALL ON FUNCTION %I.audit_cutover_control_transition() FROM PUBLIC",
        "cutover control transition is not in the fixed state graph",
        "NEW.generation <> OLD.generation + 1",
    ], "database model")
    require_text(errors, ansible, [
        "Install fixed worker-uplift cutover control manager", "mode: \"0600\"",
        "backend_worker_runtime_default_mode == 'shadow'", "backend_worker_runtime_cutover_db_password",
    ], "Ansible runtime")
    require_text(errors, apply, [
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_PASSWORD", "backend_worker_uplift_cutover_password",
        "backend_worker_runtime_cutover_db_password",
    ], "protected baseline workflow")
    require_text(errors, checks, [
        "scripts/validate_worker_uplift_cutover_controls.py",
        "tests.test_validate_worker_uplift_cutover_controls",
        "tests.test_worker_uplift_cutover_controls",
    ], "Backend Checks")
    names = {
        item.get("name")
        for group in inventory.get("secret_groups", [])
        for item in group.get("secrets", [])
        if isinstance(item, dict)
    }
    if "NUTSNEWS_WORKER_UPLIFT_CUTOVER_PASSWORD" not in names:
        errors.append("credential inventory is missing the dedicated cutover role secret")
    if decision.get("decision") != "NO-GO" or decision.get("authorized_for_execution") is not False:
        errors.append("#126 must commit a fail-closed NO-GO final decision")
    if decision.get("safety") != {
        "active_ingestion_owner": "legacy_shards", "legacy_dispatch_enabled": True,
        "uplift_mode": "shadow", "production_writes_enabled": False,
        "dns_failover_unchanged": True,
    }:
        errors.append("committed final decision safety state changed")
    return errors


def main() -> int:
    errors = validate_repository()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"worker-uplift cutover controls valid; scope_sha256={PINNED_SCOPE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
