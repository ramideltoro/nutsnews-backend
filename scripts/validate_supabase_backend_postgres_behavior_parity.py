#!/usr/bin/env python3
"""Validate the Supabase-to-backend PostgreSQL behavior parity manifest."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "supabase-backend-postgres-behavior-parity.json"
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CHECK_ID_RE = re.compile(r"^[a-z0-9_]+$")
REQUIRED_CATEGORIES = {
    "schemas",
    "extensions",
    "routines",
    "triggers",
    "indexes",
    "constraints",
    "sequences",
    "rls_policies",
    "grants",
    "default_privileges",
    "roles",
    "migration_history",
}
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=", "supabase.co")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing behavior parity manifest: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid behavior parity manifest JSON: {exc}") from exc


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
    manifest = load_json(MANIFEST_PATH)
    errors: list[str] = []

    if manifest.get("manifest_id") != "supabase-backend-postgres-behavior-parity":
        errors.append("manifest_id must be supabase-backend-postgres-behavior-parity")
    if manifest.get("issue") != 215:
        errors.append("issue must be 215")
    if manifest.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if manifest.get("version") != 1:
        errors.append("version must be 1")
    if manifest.get("safe_metadata_only") is not True:
        errors.append("safe_metadata_only must be true")
    if manifest.get("comparison_method") != "compare_catalog_query_sha256":
        errors.append("comparison_method must be compare_catalog_query_sha256")

    source = manifest.get("source", {})
    if source.get("environment") != "production":
        errors.append("source environment must be production")
    if source.get("direct_connection_required") is not True:
        errors.append("source must require a direct DB connection")
    if not SECRET_RE.match(source.get("db_url_secret", "")):
        errors.append("source db_url_secret must be an uppercase secret name")

    target = manifest.get("target", {})
    if target.get("database") != "nutsnews_primary_shadow":
        errors.append("target database must be nutsnews_primary_shadow")
    if target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("target network path must use SSH tunnel to loopback PostgreSQL")
    if target.get("public_5432_allowed") is not False:
        errors.append("target must forbid public 5432")

    if set(manifest.get("required_categories", [])) != REQUIRED_CATEGORIES:
        errors.append("required_categories must match the required behavior category set")

    seen_ids: set[str] = set()
    seen_categories: set[str] = set()
    for check in manifest.get("catalog_checks", []):
        check_id = check.get("id", "")
        category = check.get("category", "")
        query = str(check.get("query", ""))
        if not CHECK_ID_RE.match(check_id):
            errors.append(f"invalid catalog check id: {check_id}")
        if check_id in seen_ids:
            errors.append(f"duplicate catalog check id: {check_id}")
        seen_ids.add(check_id)
        if category not in REQUIRED_CATEGORIES:
            errors.append(f"catalog check {check_id} has invalid category {category}")
        seen_categories.add(category)
        if check.get("sensitivity") != "metadata_hash_only":
            errors.append(f"catalog check {check_id} must be metadata_hash_only")
        if not query.lower().lstrip().startswith("select"):
            errors.append(f"catalog check {check_id} query must start with select")
        if "select *" in query.lower():
            errors.append(f"catalog check {check_id} query must not select raw rows")
        if not any(token in query.lower() for token in ("jsonb_agg", "count(", "md5(")):
            errors.append(f"catalog check {check_id} must aggregate or hash metadata")

    missing_categories = REQUIRED_CATEGORIES - seen_categories
    for category in sorted(missing_categories):
        errors.append(f"missing catalog check category: {category}")

    workflow = manifest.get("workflow", {})
    require_path(errors, workflow.get("path", ""), "workflow.path")
    if workflow.get("mode") != "validate-production-shadow":
        errors.append("workflow mode must be validate-production-shadow")
    if workflow.get("artifact") != "backend-postgres-behavior-parity-validation":
        errors.append("workflow artifact name is incorrect")

    validation = manifest.get("validation", {})
    for field in ("manifest_validator", "offline_validator"):
        command = validation.get(field, "")
        relative_path = command.removeprefix("python3 ").split(" ", 1)[0]
        require_path(errors, relative_path, f"validation.{field}")

    cutover = manifest.get("cutover_policy", {})
    if cutover.get("failed_required_check_blocks_cutover") is not True:
        errors.append("failed behavior parity checks must block cutover")
    if cutover.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")
    if cutover.get("app_worker_writes_to_backend_allowed") is not False:
        errors.append("app/worker writes to backend must remain disabled")

    for value in walk_values(manifest):
        if isinstance(value, str):
            lowered = value.lower()
            if any(marker in lowered for marker in FORBIDDEN_MARKERS):
                errors.append("behavior parity manifest must not include secrets, provider hostnames, or database URLs")
                break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Supabase backend PostgreSQL behavior parity manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
