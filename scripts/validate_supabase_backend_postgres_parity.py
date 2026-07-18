#!/usr/bin/env python3
"""Validate the Supabase-to-backend PostgreSQL parity manifest."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "supabase-backend-postgres-parity.json"
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
OBJECT_ID_RE = re.compile(r"^[a-z0-9_.-]+$")
ALLOWED_CLASSIFICATIONS = {
    "migrate",
    "replace",
    "keep_on_supabase_temporarily",
    "exclude_with_reason",
}
ALLOWED_REPORT_STATES = {"pass", "fail", "skipped_with_reason", "warning"}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def has_secret_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in ("postgres://", "postgresql://", "service_role=", "password="))


def walk_values(value: object):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)
    else:
        yield value


def require_path(errors: list[str], value: str, field: str) -> None:
    if not value:
        errors.append(f"missing path field: {field}")
        return
    if not (ROOT / value).exists():
        errors.append(f"{field} points to missing file: {value}")


def validate_required_object(errors: list[str], item: dict, seen_ids: set[str]) -> None:
    object_id = item.get("id", "")
    if not OBJECT_ID_RE.match(object_id):
        errors.append(f"invalid required object id: {object_id}")
    if object_id in seen_ids:
        errors.append(f"duplicate required object id: {object_id}")
    seen_ids.add(object_id)

    for field in ("object_type", "name", "owner", "classification", "migration_status", "validation"):
        if field not in item:
            errors.append(f"required object {object_id} missing {field}")
    if item.get("classification") not in ALLOWED_CLASSIFICATIONS:
        errors.append(f"required object {object_id} has invalid classification")
    if item.get("migration_status") != "required":
        errors.append(f"required object {object_id} must have migration_status required")
    if item.get("owner") not in {"backend_operations", "app_api", "worker_runtime", "release_operations", "platform"}:
        errors.append(f"required object {object_id} has unknown owner {item.get('owner')}")

    validation = item.get("validation", {})
    for field in ("method", "query", "sensitivity"):
        if field not in validation:
            errors.append(f"required object {object_id} validation missing {field}")
    if validation.get("sensitivity") not in {"metadata_only", "aggregate_only"}:
        errors.append(f"required object {object_id} validation sensitivity must be safe")
    query = str(validation.get("query", "")).lower()
    if "select *" in query:
        errors.append(f"required object {object_id} validation query must not select row data")


def validate_exclusion(errors: list[str], item: dict, seen_ids: set[str]) -> None:
    object_id = item.get("id", "")
    if not OBJECT_ID_RE.match(object_id):
        errors.append(f"invalid excluded object id: {object_id}")
    if object_id in seen_ids:
        errors.append(f"duplicate object id across manifest: {object_id}")
    seen_ids.add(object_id)
    for field in ("object_type", "name", "classification", "owner", "reason", "cutover_risk"):
        if field not in item:
            errors.append(f"excluded object {object_id} missing {field}")
    if item.get("classification") not in {"replace", "keep_on_supabase_temporarily", "exclude_with_reason"}:
        errors.append(f"excluded object {object_id} must be replace, keep_on_supabase_temporarily, or exclude_with_reason")
    if len(str(item.get("reason", "")).strip()) < 20:
        errors.append(f"excluded object {object_id} needs a specific reason")
    if len(str(item.get("cutover_risk", "")).strip()) < 20:
        errors.append(f"excluded object {object_id} needs a cutover risk assessment")


def main() -> int:
    manifest = load_json(MANIFEST_PATH)
    errors: list[str] = []

    if manifest.get("manifest_id") != "supabase-backend-postgres-parity":
        errors.append("manifest_id must be supabase-backend-postgres-parity")
    if manifest.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")

    for field in ("source_inventory", "runbook", "change_freeze_runbook", "access_runbook"):
        require_path(errors, str(manifest.get(field, "")), field)

    owners = manifest.get("owners", {})
    for owner in ("backend_operations", "app_api", "worker_runtime", "release_operations", "platform"):
        if owner not in owners:
            errors.append(f"missing owner mapping: {owner}")

    source_projects = manifest.get("source_projects", {})
    production = source_projects.get("production", {})
    for field in ("project_ref_secret", "db_url_secret", "api_url_secret", "access_token_secret"):
        name = production.get(field, "")
        if not SECRET_RE.match(name):
            errors.append(f"production {field} must be an uppercase secret name")
    backend_target = source_projects.get("backend_target", {})
    if backend_target.get("public_5432_allowed") is not False:
        errors.append("backend target must explicitly forbid public 5432")
    if backend_target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("backend target network_path must use the approved SSH tunnel")

    seen_ids: set[str] = set()
    required_objects = manifest.get("required_objects", [])
    if len(required_objects) < 10:
        errors.append("manifest must list required database objects")
    for item in required_objects:
        validate_required_object(errors, item, seen_ids)

    object_ids = {item.get("id") for item in required_objects}
    for required_id in ("table.public.articles", "table.public.rss_feeds", "extension.pg_trgm"):
        if required_id not in object_ids:
            errors.append(f"missing required object: {required_id}")

    for item in manifest.get("excluded_objects", []):
        validate_exclusion(errors, item, seen_ids)
    if len(manifest.get("excluded_objects", [])) < 4:
        errors.append("manifest must document excluded/replaced Supabase platform areas")

    behavior_ids = {item.get("id") for item in manifest.get("required_behavior", [])}
    for behavior in ("roles-grants-rls", "migration-history", "single-writer"):
        if behavior not in behavior_ids:
            errors.append(f"missing required behavior gate: {behavior}")

    strategy = manifest.get("validation_strategy", {})
    if strategy.get("checksums", {}).get("required_for_high_value_tables") is not True:
        errors.append("checksum strategy must be required for high-value tables")
    if strategy.get("sequences", {}).get("required") is not True:
        errors.append("sequence strategy must be required")
    if strategy.get("schema_behavior", {}).get("required") is not True:
        errors.append("schema behavior validation must be required")

    workflow = manifest.get("workflow_contract", {})
    if workflow.get("restore", {}).get("production_target_allowed_by_default") is not False:
        errors.append("restore workflow contract must forbid production targets by default")
    if workflow.get("validation", {}).get("failed_required_check_blocks_cutover") is not True:
        errors.append("failed required validation checks must block cutover")
    if set(workflow.get("validation", {}).get("report_states", [])) != ALLOWED_REPORT_STATES:
        errors.append("validation report states must be pass/fail/skipped_with_reason/warning")
    if workflow.get("cutover", {}).get("requires_explicit_approval") is not True:
        errors.append("cutover must require explicit approval")
    if workflow.get("cutover", {}).get("supabase_remains_writer_until_approved") is not True:
        errors.append("manifest must keep Supabase as writer until approved")

    policy = manifest.get("manifest_change_policy", {})
    if policy.get("required_when_schema_changes") is not True:
        errors.append("manifest changes must be required for schema changes")

    for value in walk_values(manifest):
        if has_secret_value(value):
            errors.append("manifest appears to contain a connection string or secret value")
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Supabase backend PostgreSQL parity manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
