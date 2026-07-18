#!/usr/bin/env python3
"""Validate backend PostgreSQL primary shadow restore metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "status",
    "snapshot_id",
    "source",
    "target_database",
    "restore_scope",
    "duration_seconds",
    "validation_status",
    "validation_report",
    "operator",
    "completed_at_utc",
    "rpo_seconds",
    "rto_seconds",
    "manifest_version",
    "safe_metadata_only",
    "production_cutover_blocker_cleared",
    "workflow_url",
    "dump_checksums",
    "status_artifacts",
}
STATES = {"planned", "pass", "fail", "blocked", "not_configured"}
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=", "supabase.co")
CHECKSUM_FIELDS = {
    "public_schema_sha256",
    "public_data_sha256",
    "history_schema_sha256",
    "history_data_sha256",
}


def walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)
    else:
        yield value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="docs/backend-postgres-primary-shadow-restore.example.json")
    args = parser.parse_args()

    path = Path(args.path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing primary shadow restore report: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid primary shadow restore report JSON: {exc}") from exc

    if data.get("status") == "not_configured":
        if data.get("safe_metadata_only") is True:
            print("backend PostgreSQL primary shadow restore metadata is valid")
            return 0
        print("ERROR: not_configured restore metadata must be safe_metadata_only", file=sys.stderr)
        return 1

    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")

    if data.get("status") not in STATES:
        errors.append("status is invalid")
    if data.get("validation_status") not in STATES:
        errors.append("validation_status is invalid")
    if data.get("source") != "production_supabase_public_logical_dump":
        errors.append("source must be production_supabase_public_logical_dump")
    if data.get("target_database") != "nutsnews_primary_shadow":
        errors.append("target_database must be nutsnews_primary_shadow")
    if data.get("restore_scope") != "production_supabase_to_backend_primary_shadow":
        errors.append("restore_scope must target the backend primary shadow")
    if data.get("operator") != "production-backend protected workflow":
        errors.append("operator must be the protected workflow")
    if data.get("safe_metadata_only") is not True:
        errors.append("safe_metadata_only must be true")
    if data.get("manifest_version") != 1:
        errors.append("manifest_version must be 1")

    for field in ("duration_seconds", "rpo_seconds", "rto_seconds"):
        value = data.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"{field} must be null or a non-negative integer")

    checksums = data.get("dump_checksums", {})
    if set(checksums) != CHECKSUM_FIELDS:
        errors.append("dump_checksums must include public and migration-history schema/data digests")
    for field, value in checksums.items():
        if value != "pending" and (not isinstance(value, str) or len(value) != 64):
            errors.append(f"dump checksum {field} must be pending or a SHA-256 digest")

    artifacts = data.get("status_artifacts", [])
    if not isinstance(artifacts, list):
        errors.append("status_artifacts must be a list")
    else:
        if "/var/lib/nutsnews/postgres/primary-shadow-restore.json" not in artifacts:
            errors.append("status_artifacts must include primary shadow restore status")
        if "/var/lib/nutsnews/postgres/status.json" not in artifacts:
            errors.append("status_artifacts must include PostgreSQL status")

    if data.get("production_cutover_blocker_cleared") is True:
        if data.get("status") != "pass" or data.get("validation_status") != "pass":
            errors.append("production_cutover_blocker_cleared requires pass statuses")
        if not str(data.get("workflow_url", "")).startswith("https://github.com/ramideltoro/nutsnews-backend/actions/runs/"):
            errors.append("passing restore reports must include the workflow run URL")

    for value in walk_values(data):
        if isinstance(value, str):
            lowered = value.lower()
            if any(marker in lowered for marker in FORBIDDEN_MARKERS):
                errors.append("restore report must not include secrets, provider hostnames, or database URLs")
                break

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend PostgreSQL primary shadow restore metadata is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
