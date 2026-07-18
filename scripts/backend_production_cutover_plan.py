#!/usr/bin/env python3
"""Render a non-mutating production cutover plan."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-production-cutover-plan.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", default="dry-run")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--staging-evidence-url", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if args.operation not in plan["operations"]:
        raise SystemExit(f"unsupported cutover operation: {args.operation}")

    blockers = []
    if args.operation == "dry-run" and args.confirmation not in {"", plan["dry_run_confirmation"]}:
        blockers.append("unexpected_dry_run_confirmation")
    if args.operation != "dry-run":
        if args.confirmation != plan["mutation_confirmation"]:
            blockers.append("missing_production_cutover_confirmation")
        if not args.staging_evidence_url:
            blockers.append("missing_staging_rehearsal_evidence")
        blockers.extend(plan["remaining_external_cutover_blockers"])
        blockers.append("mutation_paths_blocked_until_coordinated_cutover")

    report = {
        "status": "blocked" if blockers else "dry_run_ready",
        "checked_at_utc": utc_now(),
        "operation": args.operation,
        "production_environment": plan["production_environment"],
        "mutation_performed": False,
        "preflight_gates": plan["preflight_gates"],
        "production_sequence": plan["production_sequence"],
        "abort_criteria": plan["abort_criteria"],
        "rollback_decision_points": plan["rollback_decision_points"],
        "completed_database_gate_evidence": plan.get("completed_database_gate_evidence", []),
        "remaining_external_cutover_blockers": plan.get("remaining_external_cutover_blockers", []),
        "staging_evidence_url": args.staging_evidence_url or None,
        "blockers": blockers,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
