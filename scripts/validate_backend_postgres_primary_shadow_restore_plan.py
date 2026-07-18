#!/usr/bin/env python3
"""Validate the backend PostgreSQL primary shadow restore plan."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-primary-shadow-restore-plan.json"
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=")


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


def require_path(errors: list[str], relative_path: str, field: str) -> None:
    if not relative_path:
        errors.append(f"missing path field: {field}")
        return
    if not (ROOT / relative_path).exists():
        errors.append(f"{field} points to missing file: {relative_path}")


def main() -> int:
    plan = load_json(PLAN_PATH)
    errors: list[str] = []

    if plan.get("plan_id") != "backend-postgres-primary-shadow-restore":
        errors.append("plan_id must be backend-postgres-primary-shadow-restore")
    if plan.get("issue") != 212:
        errors.append("issue must be 212")
    if plan.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if plan.get("version") != 1:
        errors.append("version must be 1")
    if set(plan.get("depends_on_issues", [])) != {210, 211}:
        errors.append("restore plan must depend on issues 210 and 211")

    source = plan.get("source", {})
    if source.get("environment") != "production":
        errors.append("source environment must be production")
    if source.get("dump_method") != "supabase_cli_logical_dump":
        errors.append("source dump method must use the Supabase CLI logical dump path")
    for field in ("access_token_secret", "project_ref_secret"):
        value = source.get(field, "")
        if not SECRET_NAME_RE.match(value):
            errors.append(f"source {field} must be an uppercase secret name")
    if source.get("production_writer_after_restore") != "production_supabase":
        errors.append("production Supabase must remain writer after restore")
    for scope in ("public_schema", "public_data", "supabase_migrations_schema", "supabase_migrations_data"):
        if scope not in source.get("dump_scopes", []):
            errors.append(f"missing dump scope: {scope}")

    target = plan.get("target", {})
    if target.get("database") != "nutsnews_primary_shadow":
        errors.append("target database must be nutsnews_primary_shadow")
    if target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("target network path must use SSH tunnel to loopback PostgreSQL")
    if target.get("public_5432_allowed") is not False:
        errors.append("target must forbid public 5432")
    for database_name in ("nutsnews_restore_rehearsal", "nutsnews_backup_restore_proof"):
        if database_name not in target.get("must_be_distinct_from", []):
            errors.append(f"target must be distinct from {database_name}")

    workflow = plan.get("workflow", {})
    require_path(errors, workflow.get("path", ""), "workflow.path")
    require_path(errors, workflow.get("remote_script", ""), "workflow.remote_script")
    if workflow.get("environment") != "production-backend":
        errors.append("workflow must use production-backend environment")
    if workflow.get("confirmation") != "restore-production-to-primary-shadow":
        errors.append("workflow confirmation string is incorrect")
    if workflow.get("restore_mode") != "restore-production":
        errors.append("workflow restore mode must be restore-production")
    if workflow.get("status_mode") != "status":
        errors.append("workflow status mode must be status")

    preconditions = plan.get("preconditions", {})
    if preconditions.get("requires_issue_211_live_provisioning") is not True:
        errors.append("restore must require live #211 provisioning")
    if preconditions.get("requires_primary_shadow_owner") != "nutsnews_migration_restore":
        errors.append("primary shadow owner must be nutsnews_migration_restore")
    if preconditions.get("requires_shadow_database") != "nutsnews_primary_shadow":
        errors.append("precondition shadow database must be nutsnews_primary_shadow")
    if preconditions.get("requires_loopback_only_postgresql") is not True:
        errors.append("preconditions must require loopback-only PostgreSQL")
    if preconditions.get("requires_protected_approval") is not True:
        errors.append("preconditions must require protected approval")

    artifact_policy = plan.get("artifact_policy", {})
    if artifact_policy.get("safe_metadata_only") is not True:
        errors.append("artifact policy must be safe metadata only")
    for field in ("snapshot_id", "target_database", "duration_seconds", "validation_status", "rpo_seconds", "rto_seconds", "operator", "workflow_url"):
        if field not in artifact_policy.get("required_fields", []):
            errors.append(f"artifact policy missing required field: {field}")
    for forbidden in ("database_urls", "passwords", "tokens", "database_dumps", "row_data"):
        if forbidden not in artifact_policy.get("forbidden_evidence", []):
            errors.append(f"artifact policy must forbid {forbidden}")

    validation = plan.get("validation", {})
    for field in ("plan_validator", "report_validator", "restore_validation_sql", "example_report"):
        command_or_path = validation.get(field, "")
        if field.endswith("validator"):
            relative_path = command_or_path.removeprefix("python3 ")
        else:
            relative_path = command_or_path
        require_path(errors, relative_path, f"validation.{field}")

    cutover = plan.get("cutover_policy", {})
    if cutover.get("production_cutover_allowed") is not False:
        errors.append("restore workflow must not allow production cutover")
    if cutover.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")
    if cutover.get("app_worker_writes_to_backend_allowed") is not False:
        errors.append("app/worker writes to backend must remain disabled")

    serialized = json.dumps(plan).lower()
    if any(marker in serialized for marker in FORBIDDEN_MARKERS):
        errors.append("restore plan must not include secret values or database URLs")

    for value in walk_values(plan):
        if isinstance(value, str) and "\n" in value:
            errors.append("restore plan values must not contain embedded multi-line content")
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend PostgreSQL primary shadow restore plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
