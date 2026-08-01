#!/usr/bin/env python3
"""Project a backend health report into bounded host-observability state."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CONCLUSIONS = {"success", "failure", "cancelled", "unknown"}
EXPECTED_INTERVAL_SECONDS = 24 * 60 * 60


def read_report(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def normalized_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def bounded_count(value: Any, *, maximum: int = 10_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def normalized_conclusion(value: Any) -> str:
    candidate = str(value or "unknown").lower()
    return candidate if candidate in CONCLUSIONS else "unknown"


def build_state(
    report: dict[str, Any],
    previous_report: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
    *,
    step_outcome: str,
    generated_at: str,
) -> dict[str, Any]:
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    if not (
        previous_state.get("schema_version") == 1
        and previous_state.get("safe_metadata_only") is True
        and previous_state.get("source") == "github_actions"
    ):
        previous_state = {}
    workflow = report.get("workflow") if isinstance(report.get("workflow"), dict) else {}
    previous_workflow = (
        previous_report.get("workflow")
        if isinstance(previous_report.get("workflow"), dict)
        else {}
    )
    current_run_at = normalized_timestamp(report.get("last_report_run_at"))
    report_available = bool(current_run_at and workflow)
    step_conclusion = normalized_conclusion(step_outcome)
    step_failed = step_conclusion in {"failure", "cancelled"}
    conclusion = (
        step_conclusion
        if step_failed
        else normalized_conclusion(workflow.get("conclusion") or step_outcome)
    )
    if not report_available and conclusion == "success":
        conclusion = "unknown"

    last_success_at = normalized_timestamp(
        workflow.get("last_success_at")
        or report.get("last_report_success_at")
        or previous_workflow.get("last_success_at")
        or previous_report.get("last_report_success_at")
        or previous_state.get("last_success_at_utc")
    )
    critical_checks = bounded_count(workflow.get("critical_check_count"))
    if critical_checks is None:
        critical_checks = bounded_count(previous_state.get("critical_checks"))
    previous_failures = bounded_count(previous_workflow.get("consecutive_failure_count"))
    if previous_failures is None:
        previous_failures = bounded_count(previous_state.get("consecutive_failures"))
    previous_failures = previous_failures or 0
    current_failures = bounded_count(workflow.get("consecutive_failure_count"))
    if step_failed:
        current_failures = max(current_failures or 0, previous_failures + 1)
    elif current_failures is None:
        current_failures = previous_failures + 1 if conclusion in {"failure", "cancelled", "unknown"} else 0

    return {
        "schema_version": 1,
        "safe_metadata_only": True,
        "source": "github_actions",
        "conclusion_scope": "health_report_step",
        "available": report_available,
        "conclusion": conclusion,
        "last_run_at_utc": current_run_at or normalized_timestamp(generated_at),
        "last_success_at_utc": last_success_at,
        "consecutive_failures": min(current_failures, 10_000),
        "critical_checks": critical_checks,
        "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
    }


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.chmod(temporary_path, 0o644)
    os.replace(temporary_path, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--previous-report", type=Path)
    parser.add_argument("--previous-state", type=Path)
    parser.add_argument("--step-outcome", default="unknown")
    parser.add_argument("--generated-at", default=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = build_state(
        read_report(args.report),
        read_report(args.previous_report),
        read_report(args.previous_state),
        step_outcome=args.step_outcome,
        generated_at=args.generated_at,
    )
    write_atomic(args.output, state)
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
