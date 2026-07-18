#!/usr/bin/env python3
"""Compare Supabase source and backend PostgreSQL target using safe metadata."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-parity.json"
SAFE_TYPES = {"table", "view", "materialized_view", "schema", "extension", "routine"}
DEFAULT_LIVE_REPLICATION_WAIT_SECONDS = 120
DEFAULT_LIVE_REPLICATION_POLL_INTERVAL_SECONDS = 5


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


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def skipped(item: dict, reason: str) -> dict:
    return {
        "id": item.get("id"),
        "object_type": item.get("object_type"),
        "status": "skipped_with_reason",
        "reason": reason,
    }


def live_replication_tolerance(item: dict) -> dict:
    tolerance = item.get("validation", {}).get("live_replication_tolerance", {})
    if not isinstance(tolerance, dict) or not tolerance.get("enabled"):
        return {}
    return tolerance


def validate_live_replication_item(
    item: dict,
    source_db_url: str,
    target_db_url: str,
    wait_seconds: int,
    poll_interval_seconds: int,
) -> dict:
    validation = item.get("validation", {})
    query = validation.get("query", "")
    source_baseline, source_error = run_psql(source_db_url, query)
    if source_error:
        return {
            "id": item.get("id"),
            "object_type": item.get("object_type"),
            "status": "fail",
            "source_error": source_error,
            "target_error": None,
            "validation_method": "row_count_live_replication_watermark",
        }

    baseline_count = parse_int(source_baseline)
    if baseline_count is None:
        return {
            "id": item.get("id"),
            "object_type": item.get("object_type"),
            "status": "fail",
            "source_error": "invalid_source_count",
            "target_error": None,
            "source_value": source_baseline,
            "validation_method": "row_count_live_replication_watermark",
        }

    deadline = time.monotonic() + wait_seconds
    attempts = 0
    last_target_value: str | None = None
    last_source_current: str | None = source_baseline
    last_target_error: str | None = None
    last_source_error: str | None = None
    while True:
        attempts += 1
        target_value, target_error = run_psql(target_db_url, query)
        source_current, source_current_error = run_psql(source_db_url, query)
        last_target_value = target_value
        last_source_current = source_current
        last_target_error = target_error
        last_source_error = source_current_error

        target_count = parse_int(target_value)
        current_count = parse_int(source_current)
        if target_error or source_current_error:
            break
        if target_count is None:
            last_target_error = "invalid_target_count"
            break
        if current_count is None:
            last_source_error = "invalid_source_count"
            break
        if baseline_count <= target_count <= current_count:
            return {
                "id": item.get("id"),
                "object_type": item.get("object_type"),
                "status": "pass",
                "source_value": str(current_count),
                "target_value": str(target_count),
                "source_baseline_value": str(baseline_count),
                "target_lag_rows_from_current": current_count - target_count,
                "attempts": attempts,
                "validation_method": "row_count_live_replication_watermark",
                "sensitivity": validation.get("sensitivity"),
            }
        if time.monotonic() >= deadline:
            break
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)

    target_count = parse_int(last_target_value)
    return {
        "id": item.get("id"),
        "object_type": item.get("object_type"),
        "status": "fail",
        "source_value": last_source_current,
        "target_value": last_target_value,
        "source_baseline_value": str(baseline_count),
        "target_lag_rows_from_baseline": None if target_count is None else max(0, baseline_count - target_count),
        "source_error": last_source_error,
        "target_error": last_target_error,
        "attempts": attempts,
        "reason": "target_did_not_reach_source_baseline",
        "validation_method": "row_count_live_replication_watermark",
        "sensitivity": validation.get("sensitivity"),
    }


def validate_item(
    item: dict,
    source_db_url: str,
    target_db_url: str,
    wait_seconds: int,
    poll_interval_seconds: int,
) -> dict:
    validation = item.get("validation", {})
    query = validation.get("query", "")
    if item.get("object_type") not in SAFE_TYPES:
        return skipped(item, "unsupported_object_type")
    if not query or "select *" in query.lower():
        return skipped(item, "unsafe_or_missing_query")
    if not query.lower().lstrip().startswith("select"):
        return skipped(item, "validation_query_not_metadata_safe")

    if live_replication_tolerance(item):
        return validate_live_replication_item(
            item,
            source_db_url,
            target_db_url,
            wait_seconds,
            poll_interval_seconds,
        )

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


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_STAGING_SUPABASE_DB_URL")
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--live-replication-wait-seconds", type=int, default=DEFAULT_LIVE_REPLICATION_WAIT_SECONDS)
    parser.add_argument(
        "--live-replication-poll-interval-seconds",
        type=int,
        default=DEFAULT_LIVE_REPLICATION_POLL_INTERVAL_SECONDS,
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    target_db_url = os.environ.get(args.target_db_url_env, "").strip()

    checks: list[dict] = []
    if args.offline or not source_db_url or not target_db_url:
        reason = "offline mode" if args.offline else "missing_source_or_target_db_url"
        checks = [skipped(item, reason) for item in manifest.get("required_objects", [])]
    else:
        checks = [
            validate_item(
                item,
                source_db_url,
                target_db_url,
                args.live_replication_wait_seconds,
                args.live_replication_poll_interval_seconds,
            )
            for item in manifest.get("required_objects", [])
        ]

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


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
