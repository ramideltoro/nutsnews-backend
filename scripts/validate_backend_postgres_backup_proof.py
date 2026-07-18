#!/usr/bin/env python3
"""Validate backend PostgreSQL backup restore proof metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_FIELDS = {
    "status",
    "snapshot_id",
    "backup_source",
    "restore_target",
    "restore_scope",
    "duration_seconds",
    "validation_status",
    "validation_report",
    "operator",
    "completed_at_utc",
    "rpo_seconds",
    "rto_seconds",
    "backup_freshness_status",
    "restore_health_status",
    "status_artifacts",
    "manifest_version",
    "safe_metadata_only",
    "production_cutover_blocker_cleared",
}
STATES = {"planned", "pass", "fail", "blocked"}
RESTORE_SCOPES = {"backend_postgresql_rehearsal_database", "backend_postgresql_primary_shadow_database"}
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="docs/backend-postgres-backup-restore-proof.example.json")
    args = parser.parse_args()

    path = Path(args.path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing backup proof file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid backup proof JSON: {exc}") from exc

    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")

    for field in ("status", "validation_status", "backup_freshness_status", "restore_health_status"):
        if data.get(field) not in STATES:
            errors.append(f"{field} must be one of: {', '.join(sorted(STATES))}")

    if data.get("backup_source") != "backend_postgresql_restic_snapshot":
        errors.append("backup_source must prove the backup came from backend PostgreSQL restic coverage")
    if data.get("restore_scope") not in RESTORE_SCOPES:
        errors.append("restore_scope must target an approved isolated backend PostgreSQL source database")
    if data.get("safe_metadata_only") is not True:
        errors.append("safe_metadata_only must be true")
    if data.get("manifest_version") != 1:
        errors.append("manifest_version must be 1")

    for field in ("duration_seconds", "rpo_seconds", "rto_seconds"):
        value = data.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"{field} must be null or a non-negative integer")

    artifacts = data.get("status_artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) < 2:
        errors.append("status_artifacts must list backup and restore status outputs")
    else:
        if not any(str(item).startswith("/var/lib/nutsnews/backups") for item in artifacts):
            errors.append("status_artifacts must include /var/lib/nutsnews/backups")
        if "/var/lib/nutsnews/postgres/status.json" not in artifacts:
            errors.append("status_artifacts must include /var/lib/nutsnews/postgres/status.json")

    if data.get("production_cutover_blocker_cleared") is True:
        for field in ("status", "validation_status", "backup_freshness_status", "restore_health_status"):
            if data.get(field) != "pass":
                errors.append("production_cutover_blocker_cleared requires all proof statuses to pass")

    forbidden = json.dumps(data).lower()
    if any(marker in forbidden for marker in FORBIDDEN_MARKERS):
        errors.append("backup proof must not include secrets, tokens, service-role text, or database URLs")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend PostgreSQL backup restore proof metadata is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
