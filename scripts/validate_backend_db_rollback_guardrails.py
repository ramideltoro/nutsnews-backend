#!/usr/bin/env python3
"""Validate backend DB rollback and single-writer guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARDRAILS_PATH = ROOT / "docs" / "backend-db-rollback-guardrails.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    guardrails = load_json(GUARDRAILS_PATH)
    errors: list[str] = []

    if guardrails.get("contract_id") != "backend-db-rollback-guardrails":
        errors.append("contract_id must be backend-db-rollback-guardrails")
    if guardrails.get("issue") != 118:
        errors.append("issue must be 118")
    for dependency_issue in (109, 111, 117):
        if dependency_issue not in guardrails.get("dependency_issues", []):
            errors.append(f"missing dependency issue: {dependency_issue}")
    for dependency in (
        "docs/backend-api-compatibility-contract.json",
        "docs/backend-database-provider-switch.json",
    ):
        if dependency not in guardrails.get("depends_on", []):
            errors.append(f"missing dependency: {dependency}")
    if guardrails.get("sync_back_to_supabase_supported") is not False:
        errors.append("sync_back_to_supabase_supported must be false until proven")
    if "write conflicts" not in guardrails.get("sync_back_reason", ""):
        errors.append("sync_back_reason must explain write-conflict risk")

    phases = {item.get("phase"): item for item in guardrails.get("phases", [])}
    for phase in ("supabase_primary", "shadow_reads", "final_catch_up", "backend_primary", "rollback_window"):
        if phase not in phases:
            errors.append(f"missing phase: {phase}")
            continue
        item = phases[phase]
        if item.get("supabase_writes_allowed") and item.get("backend_postgres_writes_allowed"):
            errors.append(f"phase allows split-brain writes: {phase}")
        if not item.get("split_brain_check"):
            errors.append(f"missing split-brain check: {phase}")

    if phases.get("final_catch_up", {}).get("authoritative_writer") != "none_until_switch":
        errors.append("final_catch_up must pause all writers")
    if phases.get("backend_primary", {}).get("supabase_writes_allowed") is not False:
        errors.append("backend_primary must forbid Supabase writes")

    pause_ids = {item.get("id") for item in guardrails.get("writer_pause_verification", [])}
    for required in ("app_provider_mode", "worker_paused", "supabase_no_new_writes"):
        if required not in pause_ids:
            errors.append(f"missing writer pause verification: {required}")

    rollback = guardrails.get("rollback_window", {})
    if len(rollback.get("safe_when", [])) < 3:
        errors.append("rollback safe_when must include concrete requirements")
    if len(rollback.get("forward_recovery_required_when", [])) < 3:
        errors.append("forward recovery boundary must include concrete requirements")

    blockers = "\n".join(guardrails.get("cutover_blockers", []))
    for required in ("No phase may allow writes to both", "writer pause verification", "Staging rehearsal"):
        if required not in blockers:
            errors.append(f"missing cutover blocker: {required}")

    validation = guardrails.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_backend_db_rollback_guardrails.py":
        errors.append("local_validator must name this validator")
    if validation.get("dry_run_workflow") != ".github/workflows/backend-db-rollback-guardrails-dry-run.yml":
        errors.append("dry_run_workflow must name the workflow")
    if "blocked until staging rehearsal" not in validation.get("live_status", ""):
        errors.append("live_status must record staging rehearsal blocker")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend DB rollback guardrails are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
