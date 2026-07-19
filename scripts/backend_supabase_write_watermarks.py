#!/usr/bin/env python3
"""Verify Supabase production write watermarks do not advance during a pause."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


WATERMARK_SQL = r"""
with watermarks(table_name, metrics) as (
  select 'worker_runs', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at),
    'max_run_started_at', max(run_started_at),
    'max_run_completed_at', max(run_completed_at)
  ) from public.worker_runs
  union all
  select 'ai_usage_runs', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at),
    'max_run_started_at', max(run_started_at),
    'max_run_completed_at', max(run_completed_at)
  ) from public.ai_usage_runs
  union all
  select 'feed_health', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at),
    'max_updated_at', max(updated_at),
    'max_last_checked_at', max(last_checked_at),
    'max_last_success_at', max(last_success_at),
    'max_last_failure_at', max(last_failure_at)
  ) from public.feed_health
  union all
  select 'quota_usage_events', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at)
  ) from public.quota_usage_events
  union all
  select 'runtime_feature_flags', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_created_at', max(created_at),
    'max_updated_at', max(updated_at)
  ) from public.runtime_feature_flags
  union all
  select 'article_summaries', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at),
    'max_updated_at', max(updated_at)
  ) from public.article_summaries
  union all
  select 'articles', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_created_at', max(created_at),
    'max_published_on_site_at', max(published_on_site_at)
  ) from public.articles
  union all
  select 'article_ai_reviews', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_reviewed_at', max(reviewed_at)
  ) from public.article_ai_reviews
  union all
  select 'rss_feeds', jsonb_build_object(
    'row_count', count(*)::bigint,
    'max_id', max(id),
    'max_created_at', max(created_at)
  ) from public.rss_feeds
  union all
  select 'release_readiness', jsonb_build_object(
    'row_count', count(*)::bigint,
    'schema_version', max(schema_version)
  ) from public.release_readiness
)
select jsonb_build_object(
  'checked_at_utc', to_char(transaction_timestamp() at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'table_count', count(*)::int,
  'watermarks', jsonb_object_agg(table_name, metrics order by table_name),
  'safe_metadata_only', true
)::text
from watermarks;
"""


URL_RE = re.compile(r"postgres(?:ql)?://\S+", re.IGNORECASE)
PASSWORD_RE = re.compile(r"(password=)[^\s]+", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitized_psql_error(stderr: str) -> str:
    text = PASSWORD_RE.sub(r"\1***", URL_RE.sub("postgresql://***", stderr.strip()))
    lower = text.lower()
    if "password authentication failed" in lower or "authentication failed" in lower:
        category = "psql_auth_failed"
    elif "permission denied" in lower:
        category = "psql_permission_failed"
    elif "does not exist" in lower or "syntax error" in lower:
        category = "psql_sql_shape_failed"
    elif "could not translate host" in lower or "could not connect" in lower or "connection to server" in lower:
        category = "psql_connection_failed"
    else:
        category = "psql_query_failed"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    detail = " | ".join(lines[-3:])[:500] if lines else "no_stderr"
    return f"{category}: {detail}"


def run_psql_json(db_url: str) -> dict[str, object]:
    proc = subprocess.run(
        ["psql", "--no-psqlrc", "-X", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", WATERMARK_SQL],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(sanitized_psql_error(proc.stderr))
    return json.loads(proc.stdout.strip())


def changed_tables(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    first_marks = first.get("watermarks", {})
    second_marks = second.get("watermarks", {})
    if not isinstance(first_marks, dict) or not isinstance(second_marks, dict):
        raise RuntimeError("watermark report shape is invalid")
    changed: dict[str, object] = {}
    for table_name in sorted(set(first_marks) | set(second_marks)):
        if first_marks.get(table_name) != second_marks.get(table_name):
            changed[table_name] = {
                "first": first_marks.get(table_name),
                "second": second_marks.get(table_name),
            }
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url-env", default="NUTSNEWS_PRODUCTION_SUPABASE_DB_URL")
    parser.add_argument("--observe-seconds", type=int, default=120)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if args.observe_seconds < 30 or args.observe_seconds > 600:
        raise SystemExit("observe seconds must be between 30 and 600")
    db_url = os.environ.get(args.db_url_env, "").strip()
    if not db_url:
        raise SystemExit(f"missing required DB URL env: {args.db_url_env}")

    first = run_psql_json(db_url)
    time.sleep(args.observe_seconds)
    second = run_psql_json(db_url)
    changed = changed_tables(first, second)
    report = {
        "status": "pass" if not changed else "fail",
        "checked_at_utc": utc_now(),
        "db_url_env": args.db_url_env,
        "observe_seconds": args.observe_seconds,
        "first_checked_at_utc": first.get("checked_at_utc"),
        "second_checked_at_utc": second.get("checked_at_utc"),
        "table_count": second.get("table_count"),
        "no_watermark_changes": not changed,
        "changed_tables": changed,
        "safe_metadata_only": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
