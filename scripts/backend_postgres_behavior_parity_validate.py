#!/usr/bin/env python3
"""Compare Supabase and backend PostgreSQL behavior catalogs using safe hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-behavior-parity.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_psql(db_url: str, query: str) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=True,
        )
    except FileNotFoundError:
        return None, "psql_not_installed"
    except subprocess.TimeoutExpired:
        return None, "query_timeout"
    except subprocess.CalledProcessError:
        return None, "query_failed"
    return proc.stdout.strip(), None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def skipped(check: dict, reason: str) -> dict:
    return {
        "id": check.get("id"),
        "category": check.get("category"),
        "status": "skipped_with_reason",
        "reason": reason,
        "sensitivity": check.get("sensitivity"),
    }


def validate_check(check: dict, source_db_url: str, target_db_url: str) -> dict:
    query = str(check.get("query", ""))
    if not query.lower().lstrip().startswith("select"):
        return skipped(check, "validation_query_not_select")
    if "select *" in query.lower():
        return skipped(check, "validation_query_not_metadata_safe")

    source_value, source_error = run_psql(source_db_url, query)
    target_value, target_error = run_psql(target_db_url, query)
    if source_error or target_error:
        return {
            "id": check.get("id"),
            "category": check.get("category"),
            "status": "fail",
            "source_error": source_error,
            "target_error": target_error,
            "sensitivity": check.get("sensitivity"),
        }

    assert source_value is not None
    assert target_value is not None
    source_digest = digest(source_value)
    target_digest = digest(target_value)
    return {
        "id": check.get("id"),
        "category": check.get("category"),
        "status": "pass" if source_digest == target_digest else "fail",
        "source_sha256": source_digest,
        "target_sha256": target_digest,
        "source_bytes": len(source_value.encode("utf-8")),
        "target_bytes": len(target_value.encode("utf-8")),
        "sensitivity": check.get("sensitivity"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_PRODUCTION_SUPABASE_DB_DIRECT_URL")
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--source-label", default="production_supabase")
    parser.add_argument("--target-label", default="nutsnews_primary_shadow")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    target_db_url = os.environ.get(args.target_db_url_env, "").strip()
    catalog_checks = manifest.get("catalog_checks", [])

    if args.offline or not source_db_url or not target_db_url:
        reason = "offline mode" if args.offline else "missing_source_or_target_db_url"
        checks = [skipped(check, reason) for check in catalog_checks]
    else:
        checks = [validate_check(check, source_db_url, target_db_url) for check in catalog_checks]

    failed = [check["id"] for check in checks if check["status"] == "fail"]
    skipped_checks = [check["id"] for check in checks if check["status"] == "skipped_with_reason"]
    if failed:
        status = "fail"
    elif skipped_checks:
        status = "skipped_with_reason" if args.offline else "blocked"
    else:
        status = "pass"

    report = {
        "status": status,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "manifest": str(manifest_path.relative_to(ROOT) if manifest_path.is_absolute() and manifest_path.is_relative_to(ROOT) else manifest_path),
        "manifest_version": manifest.get("version"),
        "source_label": args.source_label,
        "target_label": args.target_label,
        "source_db_url_env": args.source_db_url_env,
        "target_db_url_env": args.target_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "target_db_url_present": bool(target_db_url),
        "check_count": len(checks),
        "failed_required_checks": failed,
        "skipped_checks": skipped_checks,
        "checks": checks,
        "safe_metadata_only": True,
        "production_cutover_blocker_cleared": status == "pass",
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
