#!/usr/bin/env python3
"""Collect safe backend PostgreSQL benchmark and tuning evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

try:
    from scripts.backend_postgres_smoke_tests import READ_CHECKS
except ModuleNotFoundError:  # pragma: no cover - script-path execution
    from backend_postgres_smoke_tests import READ_CHECKS


METADATA_QUERIES = {
    "database_size": "select pg_database_size(current_database())::bigint",
    "connection_count": "select count(*)::bigint from pg_stat_activity",
    "public_relation_sizes": """
select coalesce(jsonb_agg(jsonb_build_object(
  'schema', schemaname,
  'relation', relname,
  'total_bytes', pg_total_relation_size(format('%I.%I', schemaname, relname)::regclass),
  'index_bytes', pg_indexes_size(format('%I.%I', schemaname, relname)::regclass)
) order by pg_total_relation_size(format('%I.%I', schemaname, relname)::regclass) desc), '[]'::jsonb)::text
from pg_stat_user_tables
where schemaname = 'public'
""",
    "maintenance_settings": """
select jsonb_build_object(
  'log_min_duration_statement', current_setting('log_min_duration_statement', true),
  'log_autovacuum_min_duration', current_setting('log_autovacuum_min_duration', true),
  'track_io_timing', current_setting('track_io_timing', true),
  'autovacuum', current_setting('autovacuum', true),
  'max_connections', current_setting('max_connections', true),
  'shared_buffers', current_setting('shared_buffers', true),
  'work_mem', current_setting('work_mem', true)
)::text
""",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_psql(db_url: str, query: str, *, timeout: int = 60) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=True,
        )
    except FileNotFoundError:
        return None, "psql_not_installed"
    except subprocess.TimeoutExpired:
        return None, "query_timeout"
    except subprocess.CalledProcessError:
        return None, "query_failed"
    return proc.stdout.strip(), None


def benchmark_query(db_url: str, check: dict, max_ms: int) -> dict:
    explain = f"explain (analyze, buffers, format json) {check['explain']}"
    raw, error = run_psql(db_url, explain, timeout=90)
    if error:
        return {"id": check["id"], "category": check["category"], "status": "fail", "error": error}
    try:
        plan = json.loads(raw)[0]
    except (TypeError, json.JSONDecodeError, IndexError, KeyError):
        return {"id": check["id"], "category": check["category"], "status": "fail", "error": "invalid_explain_json"}
    execution_ms = float(plan.get("Execution Time", 0))
    return {
        "id": check["id"],
        "category": check["category"],
        "status": "healthy" if execution_ms <= max_ms else "warning",
        "execution_ms": round(execution_ms, 3),
        "latency_target_ms": max_ms,
    }


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--max-query-ms", type=int, default=500)
    args = parser.parse_args(argv)

    db_url = os.environ.get(args.db_url_env, "").strip()
    blockers: list[str] = []
    metadata: dict[str, object] = {}
    benchmarks: list[dict] = []

    if args.offline:
        status = "skipped_with_reason"
        blockers.append("offline_mode_no_live_benchmarks")
        benchmarks = [
            {
                "id": check["id"],
                "category": check["category"],
                "status": "skipped_with_reason",
                "latency_target_ms": args.max_query_ms,
            }
            for check in READ_CHECKS
        ]
    elif not db_url:
        status = "blocked"
        blockers.append("missing_db_url")
    else:
        status = "healthy"
        for name, query in METADATA_QUERIES.items():
            raw, error = run_psql(db_url, query)
            metadata[name] = {"status": "fail", "error": error} if error else {"status": "healthy", "value": raw}
            if error:
                status = "fail"
                blockers.append(f"{name}_{error}")
        benchmarks = [benchmark_query(db_url, check, args.max_query_ms) for check in READ_CHECKS]
        if any(item["status"] == "fail" for item in benchmarks):
            status = "fail"
            blockers.append("benchmark_query_failed")
        elif any(item["status"] == "warning" for item in benchmarks):
            status = "warning"
            blockers.append("benchmark_latency_target_exceeded")

    report = {
        "status": status,
        "checked_at_utc": utc_now(),
        "db_url_env": args.db_url_env,
        "db_url_present": bool(db_url),
        "safe_metadata_only": True,
        "latency_target_ms": args.max_query_ms,
        "metadata": metadata,
        "benchmarks": benchmarks,
        "tuning_contract": {
            "slow_query_logging": "log_min_duration_statement <= 500ms",
            "autovacuum": "enabled with autovacuum logging",
            "connection_pooling_decision": "blocked until live benchmark connection counts exist",
            "upgrade_trigger": "sustained latency warnings, disk over 80%, or connection pressure",
        },
        "blockers": blockers,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
