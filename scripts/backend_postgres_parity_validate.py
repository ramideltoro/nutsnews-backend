#!/usr/bin/env python3
"""Compare Supabase source and backend PostgreSQL target using safe metadata."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-parity.json"
SAFE_TYPES = {"table", "view", "materialized_view", "schema", "extension", "routine"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_psql(db_url: str, query: str) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        return None, "psql_not_installed"
    except subprocess.TimeoutExpired:
        return None, "query_timeout"
    except subprocess.CalledProcessError:
        return None, "query_failed"
    return proc.stdout.strip(), None


def skipped(item: dict, reason: str) -> dict:
    return {
        "id": item.get("id"),
        "object_type": item.get("object_type"),
        "status": "skipped_with_reason",
        "reason": reason,
    }


def validate_item(item: dict, source_db_url: str, target_db_url: str) -> dict:
    validation = item.get("validation", {})
    query = validation.get("query", "")
    if item.get("object_type") not in SAFE_TYPES:
        return skipped(item, "unsupported_object_type")
    if not query or "select *" in query.lower():
        return skipped(item, "unsafe_or_missing_query")
    if not query.lower().lstrip().startswith("select"):
        return skipped(item, "validation_query_not_metadata_safe")

    source_value, source_error = run_psql(source_db_url, query)
    target_value, target_error = run_psql(target_db_url, query)
    if source_error or target_error:
        return {
            "id": item.get("id"),
            "object_type": item.get("object_type"),
            "status": "fail",
            "source_error": source_error,
            "target_error": target_error,
        }

    if source_value == target_value:
        status = "pass"
    else:
        status = "fail"
    return {
        "id": item.get("id"),
        "object_type": item.get("object_type"),
        "status": status,
        "source_value": source_value,
        "target_value": target_value,
        "sensitivity": validation.get("sensitivity"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_STAGING_SUPABASE_DB_URL")
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    target_db_url = os.environ.get(args.target_db_url_env, "").strip()

    checks: list[dict] = []
    if args.offline or not source_db_url or not target_db_url:
        reason = "offline mode" if args.offline else "missing_source_or_target_db_url"
        checks = [skipped(item, reason) for item in manifest.get("required_objects", [])]
    else:
        checks = [validate_item(item, source_db_url, target_db_url) for item in manifest.get("required_objects", [])]

    failed = [check["id"] for check in checks if check["status"] == "fail"]
    skipped_checks = [check["id"] for check in checks if check["status"] == "skipped_with_reason"]
    if failed:
        status = "fail"
    elif skipped_checks:
        status = "blocked" if not args.offline else "skipped_with_reason"
    else:
        status = "pass"

    report = {
        "status": status,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "manifest": str(manifest_path.relative_to(ROOT) if manifest_path.is_absolute() and manifest_path.is_relative_to(ROOT) else manifest_path),
        "manifest_version": manifest.get("version"),
        "source_db_url_env": args.source_db_url_env,
        "target_db_url_env": args.target_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "target_db_url_present": bool(target_db_url),
        "check_count": len(checks),
        "failed_required_checks": failed,
        "skipped_checks": skipped_checks,
        "checks": checks,
        "safe_metadata_only": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.enforce and status != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
