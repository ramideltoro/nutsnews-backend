#!/usr/bin/env python3
"""Validate the backend Redis/Valkey no-install decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "backend-redis-valkey-decision.json"
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    decision = load_json(DECISION_PATH)
    baseline = load_json(BASELINE_PATH)
    errors: list[str] = []

    if decision.get("decision") != "do_not_install_now":
        errors.append("decision must remain do_not_install_now until a concrete workload is approved")

    if decision.get("install_allowed") is not False:
        errors.append("install_allowed must be false for the current no-install decision")

    if decision.get("runtime_dependency_required") is not False:
        errors.append("runtime_dependency_required must be false while Redis/Valkey is not installed")

    public_ports = {int(entry["port"]) for entry in baseline.get("public_tcp_ports", [])}
    if not public_ports.issubset({22, 80, 443}):
        errors.append(f"service baseline exposes unsupported public ports while Redis/Valkey is not installed: {sorted(public_ports)}")

    not_deployed = set(baseline.get("not_deployed", []))
    if "Redis or Valkey" not in not_deployed:
        errors.append("service baseline no longer marks Redis or Valkey as not_deployed; update this decision")

    workloads = decision.get("workloads", {})
    for workload in ("queue", "cache", "rate_limiter", "session_state", "feed_coordination", "admin_task_progress", "retry_buffer"):
        if workload not in workloads:
            errors.append(f"missing workload decision: {workload}")

    future_gate = decision.get("future_install_gate", {})
    for required in (
        "workload_owner",
        "network",
        "authentication",
        "resource_limits",
        "persistence",
        "backup",
        "health_check",
        "observability",
        "fallback",
    ):
        if required not in future_gate:
            errors.append(f"missing future install gate: {required}")

    if "never expose" not in future_gate.get("network", ""):
        errors.append("future network gate must explicitly forbid public Redis/Valkey exposure")

    if future_gate.get("protected_apply_required") is not True:
        errors.append("future install gate must require protected apply")

    if len(decision.get("app_evidence", [])) < 3:
        errors.append("decision must include app evidence for no-install reasoning")

    if decision.get("validation", {}).get("live_verification") != "read-only until a later approved protected apply":
        errors.append("live verification must remain read-only until approved protected apply")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Redis/Valkey decision is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
