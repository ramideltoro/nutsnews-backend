#!/usr/bin/env python3
"""Prove staging logical replication propagates insert, update, and delete."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


CANDIDATES = [
    ("public", "runtime_feature_flags"),
    ("public", "worker_runs"),
    ("public", "ai_usage_runs"),
    ("public", "quota_usage_events"),
    ("public", "feed_health"),
    ("public", "migration_schema_contract"),
    ("public", "release_readiness"),
]
TEXT_UDTS = {"text", "varchar", "bpchar", "citext", "name"}
INT_UDTS = {"int2", "int4", "int8"}
NUMERIC_UDTS = {"numeric", "float4", "float8"}
TIME_UDTS = {"timestamp", "timestamptz"}
DATE_UDTS = {"date"}
BOOL_UDTS = {"bool"}
UUID_UDTS = {"uuid"}
JSON_UDTS = {"json", "jsonb"}
PROBE_TOKEN_PREFIX = "codex_db_migration_probe_"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def qident(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum() or not identifier[0].isalpha():
        raise ValueError(f"unsafe identifier: {identifier}")
    return '"' + identifier.replace('"', '""') + '"'


def qtable(schema: str, table: str) -> str:
    return f"{qident(schema)}.{qident(table)}"


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def safe_psql_error(stderr: str) -> str:
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line.startswith("ERROR:"):
            continue
        line = line.replace(PROBE_TOKEN_PREFIX, "probe_")
        return line[:240]
    return "query_failed"


def run_psql(db_url: str, sql: str, timeout: int = 30) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-X", "-At", db_url, "-c", sql],
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
    except subprocess.CalledProcessError as exc:
        return None, safe_psql_error(exc.stderr or "")
    return proc.stdout.strip(), None


def column_metadata(db_url: str, schema: str, table: str) -> tuple[list[dict[str, Any]], str | None]:
    sql = f"""
with pk as (
  select kcu.column_name, kcu.ordinal_position
  from information_schema.table_constraints tc
  join information_schema.key_column_usage kcu
    on kcu.constraint_schema = tc.constraint_schema
   and kcu.constraint_name = tc.constraint_name
   and kcu.table_schema = tc.table_schema
   and kcu.table_name = tc.table_name
  where tc.constraint_type = 'PRIMARY KEY'
    and tc.table_schema = {literal(schema)}
    and tc.table_name = {literal(table)}
)
select coalesce(jsonb_agg(jsonb_build_object(
  'name', c.column_name,
  'data_type', c.data_type,
  'udt_name', c.udt_name,
  'nullable', c.is_nullable = 'YES',
  'has_default', c.column_default is not null,
  'identity', c.is_identity = 'YES',
  'generated', c.is_generated <> 'NEVER',
  'pk_ordinal', pk.ordinal_position
) order by c.ordinal_position), '[]'::jsonb)::text
from information_schema.columns c
left join pk on pk.column_name = c.column_name
where c.table_schema = {literal(schema)}
  and c.table_name = {literal(table)}
"""
    raw, error = run_psql(db_url, sql)
    if error:
        return [], error
    return json.loads(raw or "[]"), None


def supported(column: dict[str, Any]) -> bool:
    udt = str(column.get("udt_name", ""))
    return udt in TEXT_UDTS | INT_UDTS | NUMERIC_UDTS | TIME_UDTS | DATE_UDTS | BOOL_UDTS | UUID_UDTS | JSON_UDTS


def value_sql(column: dict[str, Any], token: str, phase: str) -> str:
    udt = str(column.get("udt_name", ""))
    suffix = "updated" if phase == "updated" else "initial"
    if udt in TEXT_UDTS:
        name = str(column.get("name", ""))
        short = token[:12]
        short_suffix = "u" if phase == "updated" else "i"
        if name in {"status", "run_status"}:
            return literal("completed" if phase == "updated" else "started")
        if name in {"worker", "worker_name", "job_name", "task_name"}:
            return literal(f"codex_probe_{short}")
        if name in {"event", "event_name", "event_type", "usage_type"}:
            return literal("codex_probe")
        if name in {"provider", "model", "model_name"}:
            return literal("codex_probe")
        if name == "key":
            return literal(f"codex_probe_{short}_{short_suffix}")
        if name == "migration_head":
            return literal(f"20260718170000_codex_probe_{short_suffix}")
        if name == "schema_fingerprint":
            return literal(f"sha256:{token}")
        if name == "schema_version":
            return literal(f"0.0.0-codex-probe.{short_suffix}")
        return literal(f"{PROBE_TOKEN_PREFIX}{short}_{short_suffix}")
    if udt in UUID_UDTS:
        return literal(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{token}:{column['name']}:{phase}"))) + "::uuid"
    if udt in BOOL_UDTS:
        return "true" if phase == "updated" else "false"
    if udt in INT_UDTS:
        base = 12000 if udt == "int2" else 1000000000
        return str(base + (1 if phase == "updated" else 0))
    if udt in NUMERIC_UDTS:
        return "1000001.25" if phase == "updated" else "1000000.25"
    if udt in TIME_UDTS:
        return literal(datetime.now(UTC).replace(microsecond=0).isoformat()) + ("::timestamptz" if udt == "timestamptz" else "::timestamp")
    if udt in DATE_UDTS:
        return literal(date.today().isoformat()) + "::date"
    if udt in JSON_UDTS:
        payload = json.dumps({"codex_db_migration_probe": token, "phase": phase}, sort_keys=True)
        return literal(payload) + f"::{udt}"
    raise ValueError(f"unsupported type: {udt}")


def pk_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([column for column in columns if column.get("pk_ordinal") is not None], key=lambda column: int(column["pk_ordinal"]))


def required_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        column
        for column in columns
        if not column.get("nullable")
        and not column.get("has_default")
        and not column.get("identity")
        and not column.get("generated")
    ]


def mutable_column(columns: list[dict[str, Any]], primary_key: list[dict[str, Any]]) -> dict[str, Any] | None:
    pk_names = {column["name"] for column in primary_key}
    preferred = [
        column
        for column in columns
        if column["name"] not in pk_names
        and not column.get("identity")
        and not column.get("generated")
        and supported(column)
        and str(column.get("udt_name", "")) in TEXT_UDTS | BOOL_UDTS | TIME_UDTS | DATE_UDTS | JSON_UDTS | INT_UDTS | NUMERIC_UDTS | UUID_UDTS
    ]
    return preferred[0] if preferred else None


def column_summary(column: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(column.get("name", "")),
        "udt_name": str(column.get("udt_name", "")),
    }


def where_from_pk(primary_key: list[dict[str, Any]], pk_values: dict[str, str]) -> str:
    return " and ".join(f"{qident(column['name'])}::text = {literal(str(pk_values[column['name']]))}" for column in primary_key)


def poll(db_url: str, sql: str, expected: str, timeout_seconds: int) -> tuple[bool, int, str | None]:
    started = time.monotonic()
    last_error: str | None = None
    while time.monotonic() - started <= timeout_seconds:
        raw, error = run_psql(db_url, sql)
        if error:
            last_error = error
        elif (raw or "").strip() == expected:
            return True, int(time.monotonic() - started), None
        time.sleep(2)
    return False, int(time.monotonic() - started), last_error


def attempt_candidate(source_url: str, target_url: str, schema: str, table: str, poll_timeout: int) -> dict[str, Any]:
    table_name = f"{schema}.{table}"
    columns, error = column_metadata(source_url, schema, table)
    if error:
        return {"table": table_name, "status": "blocked", "blockers": ["source_metadata_query_failed"], "error": error}
    if not columns:
        return {"table": table_name, "status": "blocked", "blockers": ["candidate_table_missing"]}

    primary_key = pk_columns(columns)
    if not primary_key:
        return {"table": table_name, "status": "blocked", "blockers": ["candidate_primary_key_missing"]}

    required = required_columns(columns)
    unsupported_required = [column["name"] for column in required if not supported(column)]
    if unsupported_required:
        return {"table": table_name, "status": "blocked", "blockers": ["candidate_required_column_type_unsupported"]}

    update_column = mutable_column(columns, primary_key)
    if not update_column:
        return {"table": table_name, "status": "blocked", "blockers": ["candidate_mutable_column_missing"]}

    schema_summary = {
        "primary_key_columns": [column_summary(column) for column in primary_key],
        "required_columns": [column_summary(column) for column in required],
        "update_column": column_summary(update_column),
    }

    token = uuid.uuid4().hex
    insert_columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for column in required + [update_column]:
        if column["name"] not in seen:
            insert_columns.append(column)
            seen.add(column["name"])

    insert_sql = f"""
insert into {qtable(schema, table)}
({', '.join(qident(column['name']) for column in insert_columns)})
values ({', '.join(value_sql(column, token, 'initial') for column in insert_columns)})
returning jsonb_build_object(
  'pk', jsonb_build_object({', '.join(literal(column['name']) + ', ' + qident(column['name']) + '::text' for column in primary_key)})
)::text
"""
    raw, error = run_psql(source_url, insert_sql)
    if error:
        return {
            "table": table_name,
            "status": "blocked",
            "blockers": ["candidate_probe_insert_failed"],
            "error": error,
            "schema": schema_summary,
        }

    pk_values = json.loads(raw or "{}").get("pk", {})
    where_sql = where_from_pk(primary_key, pk_values)
    source_cleanup_sql = f"delete from {qtable(schema, table)} where {where_sql}"
    timings: dict[str, int] = {}
    blockers: list[str] = []

    try:
        insert_ok, seconds, poll_error = poll(
            target_url,
            f"select count(*)::int from {qtable(schema, table)} where {where_sql}",
            "1",
            poll_timeout,
        )
        timings["insert_seconds"] = seconds
        if not insert_ok:
            blockers.append("insert_not_observed_on_target")
            if poll_error:
                blockers.append("target_insert_poll_failed")
            return {
                "table": table_name,
                "status": "fail",
                "blockers": blockers,
                "timings": timings,
                "schema": schema_summary,
                "insert_propagated": False,
                "update_propagated": False,
                "delete_propagated": False,
            }

        update_sql = f"""
update {qtable(schema, table)}
set {qident(update_column['name'])} = {value_sql(update_column, token, 'updated')}
where {where_sql}
returning {qident(update_column['name'])}::text
"""
        updated_text, error = run_psql(source_url, update_sql)
        if error or not updated_text:
            blockers.append("source_probe_update_failed")
            return {
                "table": table_name,
                "status": "fail",
                "blockers": blockers,
                "timings": timings,
                "schema": schema_summary,
                "insert_propagated": True,
                "update_propagated": False,
                "delete_propagated": False,
            }

        update_ok, seconds, poll_error = poll(
            target_url,
            f"select coalesce((select {qident(update_column['name'])}::text from {qtable(schema, table)} where {where_sql} limit 1), '')",
            updated_text.strip(),
            poll_timeout,
        )
        timings["update_seconds"] = seconds
        if not update_ok:
            blockers.append("update_not_observed_on_target")
            if poll_error:
                blockers.append("target_update_poll_failed")
            return {
                "table": table_name,
                "status": "fail",
                "blockers": blockers,
                "timings": timings,
                "schema": schema_summary,
                "insert_propagated": True,
                "update_propagated": False,
                "delete_propagated": False,
            }

        _, error = run_psql(source_url, source_cleanup_sql)
        if error:
            blockers.append("source_probe_delete_failed")
            return {
                "table": table_name,
                "status": "fail",
                "blockers": blockers,
                "timings": timings,
                "schema": schema_summary,
                "insert_propagated": True,
                "update_propagated": True,
                "delete_propagated": False,
            }

        delete_ok, seconds, poll_error = poll(
            target_url,
            f"select count(*)::int from {qtable(schema, table)} where {where_sql}",
            "0",
            poll_timeout,
        )
        timings["delete_seconds"] = seconds
        if not delete_ok:
            blockers.append("delete_not_observed_on_target")
            if poll_error:
                blockers.append("target_delete_poll_failed")
            return {
                "table": table_name,
                "status": "fail",
                "blockers": blockers,
                "timings": timings,
                "schema": schema_summary,
                "insert_propagated": True,
                "update_propagated": True,
                "delete_propagated": False,
            }

        return {
            "table": table_name,
            "status": "pass",
            "blockers": [],
            "timings": timings,
            "schema": schema_summary,
            "insert_propagated": True,
            "update_propagated": True,
            "delete_propagated": True,
            "max_step_seconds": max(timings.values(), default=0),
        }
    finally:
        run_psql(source_url, source_cleanup_sql)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_STAGING_SUPABASE_DB_URL")
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--environment-name", choices=("staging",), default="staging")
    parser.add_argument("--output", default="")
    parser.add_argument("--poll-timeout-seconds", type=int, default=90)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    started_at = utc_now()
    source_url = os.environ.get(args.source_db_url_env, "").strip()
    target_url = os.environ.get(args.target_db_url_env, "").strip()
    candidate_attempts: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None

    if not source_url or not target_url:
        status = "blocked"
        blockers = ["missing_source_or_target_db_url"]
    else:
        blockers = []
        status = "blocked"
        for schema, table in CANDIDATES:
            attempt = attempt_candidate(source_url, target_url, schema, table, args.poll_timeout_seconds)
            candidate_attempts.append({k: attempt[k] for k in ("table", "status", "blockers", "error", "schema") if k in attempt})
            if attempt["status"] == "blocked":
                continue
            result = attempt
            status = attempt["status"]
            blockers = attempt.get("blockers", [])
            break
        if result is None:
            blockers = ["no_eligible_candidate_table"] + [blocker for attempt in candidate_attempts for blocker in attempt.get("blockers", [])]

    completed_at = utc_now()
    report = {
        "status": status,
        "environment_name": args.environment_name,
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "source_db_url_env": args.source_db_url_env,
        "target_db_url_env": args.target_db_url_env,
        "source_db_url_present": bool(source_url),
        "target_db_url_present": bool(target_url),
        "candidate_attempts": candidate_attempts,
        "blockers": blockers,
        "safe_metadata_only": True,
    }
    if result:
        report.update(
            {
                "table": result.get("table"),
                "insert_propagated": result.get("insert_propagated", False),
                "update_propagated": result.get("update_propagated", False),
                "delete_propagated": result.get("delete_propagated", False),
                "timings": result.get("timings", {}),
                "max_step_seconds": result.get("max_step_seconds", max(result.get("timings", {}).values(), default=0)),
                "schema": result.get("schema", {}),
            }
        )

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if args.enforce and status != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
