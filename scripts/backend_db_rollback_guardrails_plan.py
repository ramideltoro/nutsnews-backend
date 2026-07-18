#!/usr/bin/env python3
"""Emit a non-mutating rollback guardrail report for a migration phase."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARDRAILS_PATH = ROOT / "docs" / "backend-db-rollback-guardrails.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="supabase_primary")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    guardrails = json.loads(GUARDRAILS_PATH.read_text(encoding="utf-8"))
    phases = {item["phase"]: item for item in guardrails["phases"]}
    if args.phase not in phases:
        raise SystemExit(f"unsupported migration phase: {args.phase}")
    phase = phases[args.phase]
    blockers = []
    if phase["supabase_writes_allowed"] and phase["backend_postgres_writes_allowed"]:
        blockers.append("split_brain_write_risk")
    if args.phase in {"final_catch_up", "backend_primary", "rollback_window"}:
        blockers.append("requires_live_writer_pause_evidence")

    report = {
        "status": "blocked" if blockers else "dry_run_ready",
        "checked_at_utc": utc_now(),
        "phase": args.phase,
        "provider_mode": phase["provider_mode"],
        "authoritative_writer": phase["authoritative_writer"],
        "supabase_writes_allowed": phase["supabase_writes_allowed"],
        "backend_postgres_writes_allowed": phase["backend_postgres_writes_allowed"],
        "split_brain_check": phase["split_brain_check"],
        "writer_pause_verification": guardrails["writer_pause_verification"],
        "rollback_window": guardrails["rollback_window"],
        "mutation_performed": False,
        "blockers": blockers,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
