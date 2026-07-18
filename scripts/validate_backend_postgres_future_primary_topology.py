#!/usr/bin/env python3
"""Validate the backend PostgreSQL future-primary topology manifest."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_PATH = ROOT / "docs" / "backend-postgres-future-primary-topology.json"
DOC_PATH = ROOT / "docs" / "backend-postgres-future-primary-topology.md"
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FORBIDDEN_VALUE_MARKERS = (
    "postgres://",
    "postgresql://",
    "password=",
    "service_role=",
    "supabase.co",
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


def require_path(errors: list[str], relative_path: str, field: str) -> None:
    if not relative_path:
        errors.append(f"missing path field: {field}")
        return
    if not (ROOT / relative_path).exists():
        errors.append(f"{field} points to missing file: {relative_path}")


def validate_secret_names(errors: list[str], names: list[str], field: str) -> None:
    for name in names:
        if not SECRET_NAME_RE.match(name):
            errors.append(f"{field} must contain uppercase secret names only: {name}")


def main() -> int:
    topology = load_json(TOPOLOGY_PATH)
    errors: list[str] = []

    if not DOC_PATH.exists():
        errors.append(f"missing narrative topology doc: {DOC_PATH.relative_to(ROOT)}")

    if topology.get("topology_id") != "backend-postgres-future-primary-topology":
        errors.append("topology_id must be backend-postgres-future-primary-topology")
    if topology.get("issue") != 210:
        errors.append("issue must be 210")
    if topology.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if topology.get("version") != 1:
        errors.append("version must be 1")

    scope = topology.get("scope", {})
    out_of_scope = set(scope.get("out_of_scope", []))
    for forbidden_scope in (
        "active-active database topology",
        "bidirectional replication",
        "public PostgreSQL listener",
    ):
        if forbidden_scope not in out_of_scope:
            errors.append(f"scope must explicitly exclude {forbidden_scope}")

    source = topology.get("source", {})
    if source.get("authoritative_writer") != "production_supabase":
        errors.append("source authoritative_writer must remain production_supabase")
    if source.get("direct_connection_secret") != "NUTSNEWS_PRODUCTION_SUPABASE_DB_DIRECT_URL":
        errors.append("source must use the production Supabase direct DB secret name")
    if source.get("pooler_connection_allowed_for_logical_replication") is not False:
        errors.append("pooler connections must be forbidden for logical replication")
    validate_secret_names(
        errors,
        [
            source.get("direct_connection_secret", ""),
            source.get("project_ref_secret", ""),
            source.get("access_token_secret", ""),
        ],
        "source secret references",
    )

    target = topology.get("target_database", {})
    if target.get("name") != "nutsnews_primary_shadow":
        errors.append("target database must be nutsnews_primary_shadow")
    if target.get("purpose") != "future_primary_shadow":
        errors.append("target purpose must be future_primary_shadow")
    distinct_from = set(target.get("must_be_distinct_from", []))
    for database_name in ("nutsnews_restore_rehearsal", "nutsnews_backup_restore_proof"):
        if database_name not in distinct_from:
            errors.append(f"target database must be distinct from {database_name}")

    network = topology.get("network", {})
    if network.get("backend_postgres_access") != "loopback_only":
        errors.append("backend PostgreSQL access must be loopback_only")
    if network.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("network path must use SSH tunnel to loopback PostgreSQL")
    if network.get("public_5432_allowed") is not False:
        errors.append("public 5432 must be explicitly forbidden")
    if network.get("production_mutation_boundary") != "production-backend":
        errors.append("production mutation boundary must be production-backend")

    write_policy = topology.get("write_policy", {})
    if write_policy.get("single_writer") != "production_supabase":
        errors.append("write policy must keep production_supabase as the single writer")
    if write_policy.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")
    for field in (
        "backend_app_worker_writes_allowed_before_cutover",
        "active_active_allowed",
        "bidirectional_replication_allowed",
    ):
        if write_policy.get(field) is not False:
            errors.append(f"write policy {field} must be false")
    if write_policy.get("writer_pause_required_for_cutover") is not True:
        errors.append("writer pause must be required for cutover")
    if write_policy.get("production_cutover_requires_protected_workflow") != "production-backend":
        errors.append("cutover must require the production-backend protected workflow")

    replication = topology.get("replication", {})
    if replication.get("direction") != "production_supabase_to_backend_postgres":
        errors.append("replication must be production_supabase_to_backend_postgres")
    if replication.get("mode") != "one_way_logical_replication":
        errors.append("replication mode must be one_way_logical_replication")
    if replication.get("subscription_copy_data_after_snapshot_restore") is not False:
        errors.append("subscription copy_data must be false after snapshot restore")
    if replication.get("active_active_allowed") is not False:
        errors.append("replication must forbid active-active")
    if replication.get("bidirectional_allowed") is not False:
        errors.append("replication must forbid bidirectional replication")
    if replication.get("required_before_cutover") is not True:
        errors.append("replication must be required before cutover")
    require_path(errors, str(replication.get("publication_manifest", "")), "replication.publication_manifest")

    roles = topology.get("role_contract", {}).get("database_roles", [])
    role_ids = {role.get("id") for role in roles}
    for role_id in (
        "postgres",
        "nutsnews_migration_restore",
        "nutsnews_migration_validation",
        "nutsnews_migration_replication",
        "nutsnews_readonly",
        "nutsnews_app",
        "anon",
        "authenticated",
        "service_role",
    ):
        if role_id not in role_ids:
            errors.append(f"missing database role contract: {role_id}")
    for role in roles:
        role_id = str(role.get("id", ""))
        if not ROLE_NAME_RE.match(role_id):
            errors.append(f"invalid role id: {role_id}")
        secret_names = role.get("secret_names", [])
        if not isinstance(secret_names, list):
            errors.append(f"role {role_id} secret_names must be a list")
            continue
        validate_secret_names(errors, secret_names, f"role {role_id} secret_names")
        if role_id == "nutsnews_app" and role.get("writes_allowed_before_cutover") is not False:
            errors.append("nutsnews_app writes must be disabled before cutover")

    workflow_issues = {gate.get("issue") for gate in topology.get("workflow_gates", [])}
    for issue in (211, 212, 213, 214, 215, 216, 217):
        if issue not in workflow_issues:
            errors.append(f"missing workflow gate for issue {issue}")
    for gate in topology.get("workflow_gates", []):
        evidence = gate.get("required_evidence", [])
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"workflow gate {gate.get('issue')} must list required evidence")
        boundary = gate.get("mutation_boundary")
        if boundary not in {"production-backend", "read_only_or_shadow_only"}:
            errors.append(f"workflow gate {gate.get('issue')} has invalid mutation boundary {boundary}")

    artifact_policy = topology.get("artifact_policy", {})
    if artifact_policy.get("safe_metadata_only") is not True:
        errors.append("artifact policy must require safe metadata only")
    forbidden_evidence = set(artifact_policy.get("forbidden_evidence", []))
    for item in ("connection_strings", "database_urls", "secret_values", "passwords", "database_dumps", "row_data"):
        if item not in forbidden_evidence:
            errors.append(f"artifact policy must forbid {item}")
    allowed_evidence = set(artifact_policy.get("allowed_evidence", []))
    for item in ("workflow_run_url", "artifact_id", "snapshot_id", "database_name", "checksum_digests", "watermark_lsn"):
        if item not in allowed_evidence:
            errors.append(f"artifact policy must allow safe evidence field {item}")

    cutover = topology.get("cutover_and_retirement_gates", {})
    if cutover.get("cutover_issue") != 119:
        errors.append("cutover issue must be 119")
    if cutover.get("post_cutover_issue") != 114:
        errors.append("post-cutover issue must be 114")
    if cutover.get("cutover_allowed_before_all_shadow_gates_pass") is not False:
        errors.append("cutover must be blocked before all shadow gates pass")
    if cutover.get("post_cutover_retirement_allowed_before_cutover_complete") is not False:
        errors.append("post-cutover retirement must be blocked before cutover completes")

    validation = topology.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_backend_postgres_future_primary_topology.py":
        errors.append("validation must name the local topology validator")
    require_path(errors, str(validation.get("ci_workflow", "")), "validation.ci_workflow")

    for value in walk_values(topology):
        if isinstance(value, str):
            lowered = value.lower()
            if any(marker in lowered for marker in FORBIDDEN_VALUE_MARKERS):
                errors.append("topology manifest appears to contain a DB URL, provider host, or secret value")
                break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend PostgreSQL future-primary topology is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
