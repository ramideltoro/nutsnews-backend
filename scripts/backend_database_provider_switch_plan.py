#!/usr/bin/env python3
"""Emit a non-mutating backend database provider switch plan."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWITCH_PATH = ROOT / "docs" / "backend-database-provider-switch.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="supabase_primary")
    parser.add_argument("--environment", default="non-production")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    switch = json.loads(SWITCH_PATH.read_text(encoding="utf-8"))
    modes = {item["mode"]: item for item in switch["modes"]}
    if args.mode not in modes:
        raise SystemExit(f"unsupported provider mode: {args.mode}")

    selected = modes[args.mode]
    blockers: list[str] = []
    if args.mode == "backend_postgres_primary" and args.confirmation != switch["production_primary_confirmation"]:
        blockers.append("missing_backend_postgres_primary_confirmation")
    if args.environment == "production" and args.mode != "supabase_primary":
        blockers.append("production_switch_requires_protected_cutover_workflow")

    report = {
        "status": "blocked" if blockers else "dry_run_ready",
        "checked_at_utc": utc_now(),
        "mode": args.mode,
        "environment": args.environment,
        "writer": selected["writer"],
        "serves_production_responses": selected["serves_production_responses"],
        "mutation_performed": False,
        "required_environment": switch["environment_variables"],
        "deployment_order": switch["deployment_order"],
        "rollback": switch["rollback"],
        "blockers": blockers,
        "safe_default": switch["safe_default"],
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
