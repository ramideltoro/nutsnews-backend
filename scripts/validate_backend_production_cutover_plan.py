#!/usr/bin/env python3
"""Validate the backend production cutover plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-production-cutover-plan.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    plan = load_json(PLAN_PATH)
    errors: list[str] = []

    if plan.get("plan_id") != "backend-production-cutover-plan":
        errors.append("plan_id must be backend-production-cutover-plan")
    if plan.get("issue") != 116:
        errors.append("issue must be 116")
    for issue in (115, 118):
        if issue not in plan.get("depends_on", []):
            errors.append(f"missing dependency issue: {issue}")
    if plan.get("production_environment") != "production-backend":
        errors.append("production_environment must be production-backend")
    if plan.get("mutation_confirmation") != "execute-production-db-cutover":
        errors.append("mutation confirmation must be explicit")
    if plan.get("dry_run_confirmation") != "plan-production-cutover-only":
        errors.append("dry-run confirmation must be explicit")

    for operation in (
        "dry-run",
        "preflight-only",
        "final-replication-catchup",
        "final-dump-restore",
        "switch-provider",
        "rollback",
    ):
        if operation not in plan.get("operations", []):
            errors.append(f"missing operation: {operation}")

    gates = "\n".join(plan.get("preflight_gates", []))
    for required in ("backup", "replication", "parity", "smoke", "rollback", "staging rehearsal"):
        if required not in gates:
            errors.append(f"missing preflight gate: {required}")

    sequence = plan.get("production_sequence", [])
    for required in ("pause app and worker writers", "verify writer pause", "switch provider mode to backend_postgres_primary"):
        if required not in sequence:
            errors.append(f"missing production sequence step: {required}")

    aborts = "\n".join(plan.get("abort_criteria", []))
    for required in ("missing staging rehearsal evidence", "writer pause", "smoke test failure"):
        if required not in aborts:
            errors.append(f"missing abort criteria: {required}")

    rollback = "\n".join(plan.get("rollback_decision_points", []))
    if "forward recovery" not in rollback or "supabase_primary" not in rollback:
        errors.append("rollback decision points must include supabase_primary and forward recovery")

    workflow = plan.get("workflow", {})
    if workflow.get("path") != ".github/workflows/backend-production-cutover.yml":
        errors.append("workflow path must be backend-production-cutover.yml")
    if workflow.get("protected_environment") != "production-backend":
        errors.append("workflow must use production-backend")
    if workflow.get("mutates_production_by_default") is not False:
        errors.append("workflow must not mutate production by default")

    validation = plan.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_backend_production_cutover_plan.py":
        errors.append("local_validator must name this validator")
    if "blocked until staging rehearsal evidence" not in validation.get("live_status", ""):
        errors.append("live_status must record staging evidence blocker")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend production cutover plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
