#!/usr/bin/env python3
"""Validate the backend PostgreSQL primary shadow backup/restore proof plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-primary-shadow-backup-restore-proof-plan.json"
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=", "supabase.co")


def main() -> int:
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing backup restore proof plan: {PLAN_PATH}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid backup restore proof plan JSON: {exc}") from exc

    errors: list[str] = []
    if plan.get("plan_id") != "backend-postgres-primary-shadow-backup-restore-proof":
        errors.append("plan_id must be backend-postgres-primary-shadow-backup-restore-proof")
    if plan.get("issue") != 216:
        errors.append("issue must be 216")
    if plan.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if set(plan.get("depends_on_issues", [])) != {211, 212}:
        errors.append("proof plan must depend on issues 211 and 212")
    if plan.get("source_database") != "nutsnews_primary_shadow":
        errors.append("source database must be nutsnews_primary_shadow")
    if plan.get("isolated_restore_target") != "nutsnews_primary_shadow_backup_restore_proof":
        errors.append("isolated restore target is incorrect")

    workflow = plan.get("workflow", {})
    path = workflow.get("path", "")
    if not path or not (ROOT / path).exists():
        errors.append("workflow path is invalid")
    if workflow.get("source_database_input") != "primary-shadow":
        errors.append("workflow must use source_database=primary-shadow")
    if workflow.get("confirmation") != "prove-backend-postgres-backup-restore":
        errors.append("workflow confirmation is incorrect")

    artifact_policy = plan.get("artifact_policy", {})
    if artifact_policy.get("safe_metadata_only") is not True:
        errors.append("artifact policy must be safe metadata only")
    for field in ("snapshot_id", "restore_target", "duration_seconds", "validation_status", "rpo_seconds", "rto_seconds", "operator"):
        if field not in artifact_policy.get("required_fields", []):
            errors.append(f"artifact policy missing {field}")
    for item in ("database_urls", "passwords", "tokens", "database_dumps", "row_data"):
        if item not in artifact_policy.get("forbidden_evidence", []):
            errors.append(f"artifact policy must forbid {item}")

    cutover = plan.get("cutover_policy", {})
    if cutover.get("failed_proof_blocks_cutover") is not True:
        errors.append("failed proof must block cutover")
    if cutover.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")

    for command in plan.get("validation", {}).values():
        path_text = str(command).removeprefix("python3 ").split(" ", 1)[0]
        if not (ROOT / path_text).exists():
            errors.append(f"validation path is invalid: {path_text}")

    serialized = json.dumps(plan).lower()
    if any(marker in serialized for marker in FORBIDDEN_MARKERS):
        errors.append("proof plan must not include secrets, provider hostnames, or database URLs")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend PostgreSQL primary shadow backup/restore proof plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
