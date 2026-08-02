#!/usr/bin/env python3
"""Validate safe metadata emitted by the backend release migration runner."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def validate(report: dict) -> list[str]:
    errors: list[str] = []
    exact = {
        "version": 1,
        "status": "pass",
        "safe_metadata_only": True,
        "target_database": "nutsnews_primary_shadow",
        "transactional": True,
        "advisory_lock": "nutsnews:backend-release-migration",
    }
    for name, expected in exact.items():
        if report.get(name) != expected:
            errors.append(f"{name} must be {expected!r}")
    for name, pattern in {
        "source_commit": r"[0-9a-f]{40}",
        "starting_migration_head": r"[0-9]{14}",
        "migration_head": r"[0-9]{14}",
        "schema_version": r"[0-9]{14}",
        "pre_schema_sha256": r"[0-9a-f]{64}",
        "backup_proof_url": r"https://github\.com/ramideltoro/nutsnews-backend/actions/runs/[1-9][0-9]*",
        "completed_at_utc": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T.+Z",
    }.items():
        if not re.fullmatch(pattern, str(report.get(name) or "")):
            errors.append(f"{name} has an invalid safe-metadata value")
    count = report.get("applied_migration_count")
    if not isinstance(count, int) or count < 0 or count > 2:
        errors.append("applied_migration_count must be between zero and two")
    if "restore the exact encrypted backup proof snapshot" not in str(report.get("rollback") or ""):
        errors.append("rollback guidance must reference the exact encrypted backup proof snapshot")
    forbidden = re.compile(r"(?i)(postgres(?:ql)?://|password|bearer\s|token[=:]|private[_ -]?key|row_data)")
    if forbidden.search(json.dumps(report, sort_keys=True)):
        errors.append("report contains forbidden secret or row-data material")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"backend release migration report is missing or invalid: {exc}") from None
    errors = validate(report)
    if errors:
        raise SystemExit("backend release migration report failed validation:\n- " + "\n- ".join(errors))
    print("Backend PostgreSQL release migration report is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
