#!/usr/bin/env python3
"""Run critical NutsNews PostgreSQL smoke tests using safe metadata only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


READ_CHECKS = [
    {
        "id": "public_feed_snapshot_count",
        "category": "public_feed",
        "query": "select count(*)::bigint from public.public_feed_snapshot",
        "explain": "select original_url from public.public_feed_snapshot order by published_on_site_at desc nulls last limit 10",
    },
    {
        "id": "article_detail_lookup",
        "category": "article",
        "query": "select count(*)::bigint from public.articles where original_url is not null",
        "explain": "select id from public.articles where original_url is not null order by published_on_site_at desc nulls last limit 1",
    },
    {
        "id": "search_articles_function",
        "category": "search",
        "query": "select count(*)::bigint from public.search_articles('news', 5, 0)",
        "explain": "select original_url from public.search_articles('news', 5, 0) limit 5",
    },
    {
        "id": "worker_runs_recent_count",
        "category": "worker",
        "query": "select count(*)::bigint from public.worker_runs",
        "explain": "select id from public.worker_runs order by run_started_at desc limit 5",
    },
    {
        "id": "admin_reviews_recent_count",
        "category": "admin",
        "query": "select count(*)::bigint from public.article_ai_reviews",
        "explain": "select original_url from public.article_ai_reviews order by reviewed_at desc limit 5",
    },
    {
        "id": "quota_usage_count",
        "category": "quota",
        "query": "select count(*)::bigint from public.quota_usage_events",
        "explain": "select id from public.quota_usage_events order by created_at desc limit 5",
    },
    {
        "id": "release_readiness_singleton",
        "category": "release_readiness",
        "query": "select count(*)::bigint from public.release_readiness where singleton is true",
        "explain": "select schema_version from public.release_readiness where singleton is true limit 1",
    },
    {
        "id": "feed_health_dashboard_count",
        "category": "dashboard",
        "query": "select count(*)::bigint from public.feed_health",
        "explain": "select feed_url from public.feed_health order by last_success_at desc nulls last limit 5",
    },
]

WRITE_CHECKS = [
    {
        "id": "quota_usage_insert_update_delete_rollback",
        "category": "quota",
        "query": """
begin;
insert into public.quota_usage_events (event_type, event_source, provider, quantity, metadata)
values ('migration_smoke_test', 'backend_postgres_smoke_tests', 'backend_postgres', 0, '{"rollback": true}'::jsonb)
returning id;
update public.quota_usage_events
set metadata = metadata || '{"updated": true}'::jsonb
where event_type = 'migration_smoke_test'
  and event_source = 'backend_postgres_smoke_tests';
delete from public.quota_usage_events
where event_type = 'migration_smoke_test'
  and event_source = 'backend_postgres_smoke_tests';
rollback;
""",
    },
    {
        "id": "worker_run_insert_rollback",
        "category": "worker",
        "query": """
begin;
insert into public.worker_runs (
  run_started_at,
  run_completed_at,
  run_source,
  request_id,
  shard_index,
  feeds_per_shard,
  max_ai_reviews,
  success,
  duration_ms
)
values (
  now(),
  now(),
  'manual',
  'backend-postgres-smoke-tests',
  -1,
  0,
  0,
  true,
  0
)
returning id;
rollback;
""",
    },
]


def run_psql(db_url: str, query: str, *, timeout: int = 30) -> tuple[str | None, str | None, int | None]:
    started = datetime.now(timezone.utc)
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
        return None, "psql_not_installed", None
    except subprocess.TimeoutExpired:
        return None, "query_timeout", None
    except subprocess.CalledProcessError:
        return None, "query_failed", None
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return proc.stdout.strip(), None, duration_ms


def skipped_check(item: dict, reason: str, mode: str) -> dict:
    return {
        "id": item["id"],
        "category": item["category"],
        "mode": mode,
        "status": "skipped_with_reason",
        "reason": reason,
    }


def run_read_check(db_url: str, item: dict) -> dict:
    value, error, duration_ms = run_psql(db_url, item["query"])
    explain_error = None
    explain_duration_ms = None
    if not error:
        _, explain_error, explain_duration_ms = run_psql(db_url, "explain (format json, costs false) " + item["explain"])
    return {
        "id": item["id"],
        "category": item["category"],
        "mode": "read",
        "status": "fail" if error or explain_error else "pass",
        "value": value if error is None else None,
        "duration_ms": duration_ms,
        "explain_checked": explain_error is None,
        "explain_duration_ms": explain_duration_ms,
        "error": error or explain_error,
    }


def run_write_check(db_url: str, item: dict) -> dict:
    _value, error, duration_ms = run_psql(db_url, item["query"])
    return {
        "id": item["id"],
        "category": item["category"],
        "mode": "write_rollback",
        "status": "fail" if error else "pass",
        "duration_ms": duration_ms,
        "rollback_only": True,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    target_db_url = os.environ.get(args.target_db_url_env, "").strip()
    skip_reason = "offline mode" if args.offline else "missing_target_db_url"
    checks: list[dict] = []

    for item in READ_CHECKS:
        checks.append(skipped_check(item, skip_reason, "read") if args.offline or not target_db_url else run_read_check(target_db_url, item))
    for item in WRITE_CHECKS:
        checks.append(
            skipped_check(item, skip_reason, "write_rollback") if args.offline or not target_db_url else run_write_check(target_db_url, item)
        )

    failed = [check["id"] for check in checks if check["status"] == "fail"]
    skipped = [check["id"] for check in checks if check["status"] == "skipped_with_reason"]
    status = "fail" if failed else "skipped_with_reason" if skipped else "pass"
    report = {
        "status": status,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "target_db_url_env": args.target_db_url_env,
        "target_db_url_present": bool(target_db_url),
        "check_count": len(checks),
        "checks": checks,
        "failed_checks": failed,
        "skipped_checks": skipped,
        "safe_metadata_only": True,
        "production_supabase_mutated": False,
        "write_checks_rollback_only": True,
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
