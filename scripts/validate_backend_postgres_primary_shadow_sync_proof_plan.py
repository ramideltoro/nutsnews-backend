#!/usr/bin/env python3
"""Validate the backend PostgreSQL primary shadow sync proof plan."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-primary-shadow-sync-proof-plan.json"
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=", "supabase.co")


def main() -> int:
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing sync proof plan: {PLAN_PATH}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid sync proof plan JSON: {exc}") from exc

    errors: list[str] = []
    if plan.get("plan_id") != "backend-postgres-primary-shadow-sync-proof":
        errors.append("plan_id must be backend-postgres-primary-shadow-sync-proof")
    if plan.get("issue") != 214:
        errors.append("issue must be 214")
    if plan.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if set(plan.get("depends_on_issues", [])) != {211, 212, 213, 215}:
        errors.append("sync proof must depend on issues 211, 212, 213, and 215")
    if not SECRET_RE.match(plan.get("source", {}).get("db_url_secret", "")):
        errors.append("source db_url_secret must be an uppercase secret name")

    target = plan.get("target", {})
    if target.get("database") != "nutsnews_primary_shadow":
        errors.append("target database must be nutsnews_primary_shadow")
    if target.get("database_variable") != "NUTSNEWS_BACKEND_POSTGRES_PRIMARY_SHADOW_DATABASE":
        errors.append("target database variable must be NUTSNEWS_BACKEND_POSTGRES_PRIMARY_SHADOW_DATABASE")
    if target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("target network path must use SSH tunnel to loopback PostgreSQL")
    if target.get("public_5432_allowed") is not False:
        errors.append("target must forbid public 5432")

    workflow_names = {workflow.get("name") for workflow in plan.get("proof_workflows", [])}
    if workflow_names != {"replication_health", "object_parity", "behavior_parity"}:
        errors.append("proof workflows must include replication_health, object_parity, and behavior_parity")
    for workflow in plan.get("proof_workflows", []):
        path = workflow.get("path", "")
        if not path or not (ROOT / path).exists():
            errors.append(f"workflow path is missing: {path}")
        if not workflow.get("artifact"):
            errors.append(f"workflow {workflow.get('name')} must define an artifact")
        if not workflow.get("required_evidence"):
            errors.append(f"workflow {workflow.get('name')} must list required evidence")

    artifact_policy = plan.get("artifact_policy", {})
    if artifact_policy.get("safe_metadata_only") is not True:
        errors.append("artifact policy must be safe metadata only")
    for item in ("database_urls", "passwords", "tokens", "wal_contents", "database_dumps", "row_data"):
        if item not in artifact_policy.get("forbidden_evidence", []):
            errors.append(f"artifact policy must forbid {item}")

    cutover = plan.get("cutover_policy", {})
    if cutover.get("failed_proof_blocks_cutover") is not True:
        errors.append("failed proof must block cutover")
    if cutover.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")
    if cutover.get("production_write_probe_allowed") is not False:
        errors.append("production write probes must be forbidden")

    validator = plan.get("validation", {}).get("local_validator", "").removeprefix("python3 ")
    if not validator or not (ROOT / validator).exists():
        errors.append("local validator path is invalid")

    serialized = json.dumps(plan).lower()
    if any(marker in serialized for marker in FORBIDDEN_MARKERS):
        errors.append("sync proof plan must not include secrets, provider hostnames, or database URLs")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend PostgreSQL primary shadow sync proof plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
