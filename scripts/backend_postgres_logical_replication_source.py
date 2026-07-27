#!/usr/bin/env python3
"""Configure safe source-side logical replication objects."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-logical-replication-plan.json"
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
CLEANUP_SCOPE = "obsolete_supabase_to_backend_migration_logical_replication_only"
PRESERVED_HOT_STANDBY_RESOURCES = [
    "existing_production_supabase_standby",
    "supabase-standby_environment_and_NUTSNEWS_STANDBY_SUPABASE_secrets",
    "backend_to_supabase_sync_relay_service_timer_env_contract_reports",
    "standby_manifest_and_failover_approval_guardrails",
]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quote_ident(name: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def quote_table(name: str) -> str:
    parts = name.split(".")
    if len(parts) != 2:
        raise ValueError(f"expected schema-qualified table: {name}")
    return ".".join(quote_ident(part) for part in parts)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_psql(db_url: str, sql: str, timeout: int = 60) -> tuple[str, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-X", "-At", db_url, "-c", sql],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return "", "psql_not_installed"
    except subprocess.TimeoutExpired:
        return "", "query_timeout"
    if proc.returncode != 0:
        return "", "query_failed"
    return proc.stdout.strip(), None


def values_rows(tables: list[str]) -> str:
    rows = []
    for table in tables:
        schema, name = table.split(".", 1)
        rows.append(f"({sql_literal(schema)}, {sql_literal(name)})")
    return ", ".join(rows)


def direct_url_status(db_url: str) -> dict[str, object]:
    parsed = urlparse(db_url)
    host = parsed.hostname or ""
    return {
        "present": bool(db_url),
        "scheme": parsed.scheme,
        "port": parsed.port,
        "pooler": "pooler.supabase.com" in host or parsed.port == 6543,
        "database_present": bool((parsed.path or "/").strip("/")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("status", "setup", "teardown-dry-run", "teardown"), default="status")
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_SOURCE_DB_URL")
    parser.add_argument("--environment-name", choices=("staging", "production"), default="staging")
    parser.add_argument("--output", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    names = plan["replication_names"]
    publication = names["publication"]
    slot = names["slot"]
    tables = plan["publication_tables"]
    table_sql = ", ".join(quote_table(table) for table in tables)
    values_sql = values_rows(tables)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    url_status = direct_url_status(source_db_url)
    blockers: list[str] = []

    if not source_db_url:
        blockers.append("missing_source_db_url")
    if url_status["pooler"]:
        blockers.append("source_db_url_is_pooler")
    if url_status["port"] != 5432:
        blockers.append("source_db_url_not_direct_5432")

    checks: dict[str, object] = {}
    if not blockers:
        wal_level, error = run_psql(source_db_url, "show wal_level")
        checks["wal_level"] = wal_level if not error else error
        if error:
            blockers.append("source_connection_failed")
        elif wal_level != "logical":
            blockers.append("source_wal_level_not_logical")

    if not blockers:
        table_check_sql = f"""
with required(schema_name, table_name) as (values {values_sql})
select coalesce(jsonb_agg(schema_name || '.' || table_name order by schema_name, table_name), '[]'::jsonb)::text
from required r
where not exists (
  select 1
  from information_schema.tables t
  where t.table_schema = r.schema_name
    and t.table_name = r.table_name
)
"""
        raw, error = run_psql(source_db_url, table_check_sql)
        missing_tables = json.loads(raw or "[]") if not error else []
        checks["missing_publication_tables"] = missing_tables if not error else error
        if error:
            blockers.append("source_table_check_failed")
        if missing_tables:
            blockers.append("source_publication_tables_missing")

    if not blockers:
        identity_sql = f"""
with required(schema_name, table_name) as (values {values_sql})
select coalesce(jsonb_agg(r.schema_name || '.' || r.table_name order by r.schema_name, r.table_name), '[]'::jsonb)::text
from required r
join pg_namespace n on n.nspname = r.schema_name
join pg_class c on c.relnamespace = n.oid and c.relname = r.table_name and c.relkind = 'r'
where c.relreplident = 'd'
  and not exists (
    select 1
    from pg_index i
    where i.indrelid = c.oid
      and i.indisprimary
  )
"""
        raw, error = run_psql(source_db_url, identity_sql)
        missing_identity = json.loads(raw or "[]") if not error else []
        checks["missing_replica_identity"] = missing_identity if not error else error
        if error:
            blockers.append("source_replica_identity_check_failed")
        if missing_identity:
            blockers.append("source_replica_identity_missing")

    if args.operation == "setup" and not blockers:
        create_publication_sql = f"""
do $$
begin
  if not exists (select 1 from pg_publication where pubname = {sql_literal(publication)}) then
    execute 'create publication {quote_ident(publication)}';
  end if;
end $$;
alter publication {quote_ident(publication)} set table {table_sql};
"""
        _, error = run_psql(source_db_url, create_publication_sql)
        if error:
            blockers.append("source_publication_setup_failed")

    if args.operation == "setup" and not blockers:
        create_slot_sql = f"""
do $$
begin
  if not exists (select 1 from pg_replication_slots where slot_name = {sql_literal(slot)}) then
    perform pg_create_logical_replication_slot({sql_literal(slot)}, 'pgoutput');
  end if;
end $$;
"""
        _, error = run_psql(source_db_url, create_slot_sql)
        if error:
            blockers.append("source_slot_setup_failed")

    publication_status = "skipped"
    publication_count = 0
    if not blockers:
        publication_sql = f"""
select count(*)::int
from pg_publication
where pubname = {sql_literal(publication)}
"""
        raw, error = run_psql(source_db_url, publication_sql)
        if error:
            blockers.append("source_publication_status_failed")
            publication_status = "unknown"
        else:
            publication_count = int(raw or "0")
            publication_status = "configured" if publication_count else "not_configured"

    slot_status = "skipped"
    slot_count = 0
    slots: list[dict[str, object]] = []
    if not blockers:
        slot_sql = f"""
select coalesce(jsonb_agg(jsonb_build_object(
  'slot_name', slot_name,
  'active', active,
  'restart_lsn_present', restart_lsn is not null,
  'confirmed_flush_lsn_present', confirmed_flush_lsn is not null
) order by slot_name), '[]'::jsonb)::text
from pg_replication_slots
where slot_name = {sql_literal(slot)}
"""
        raw, error = run_psql(source_db_url, slot_sql)
        if error:
            blockers.append("source_slot_status_failed")
            slot_status = "unknown"
        else:
            slots = json.loads(raw or "[]")
            slot_count = len(slots)
            slot_status = "configured" if slots else "not_configured"
        checks["slots"] = slots

    teardown_actions: list[str] = []
    if args.operation in {"teardown-dry-run", "teardown"}:
        teardown_actions = [
            "drop_backend_subscription_before_source_teardown",
            "drop_source_replication_slot_if_inactive",
            "drop_source_publication",
        ]

    if args.operation == "teardown" and not blockers:
        active_slots = [slot_info for slot_info in slots if slot_info.get("active")]
        if active_slots:
            blockers.append("source_slot_active")
        else:
            teardown_sql = f"""
do $$
begin
  if exists (select 1 from pg_replication_slots where slot_name = {sql_literal(slot)}) then
    perform pg_drop_replication_slot({sql_literal(slot)});
  end if;
end $$;
drop publication if exists {quote_ident(publication)};
"""
            _, error = run_psql(source_db_url, teardown_sql)
            if error:
                blockers.append("source_teardown_failed")
            else:
                publication_count = 0
                publication_status = "not_configured"
                slot_count = 0
                slot_status = "not_configured"
                slots = []
                checks["slots"] = slots

    status = "blocked" if blockers else "pass"
    report = {
        "status": status,
        "checked_at_utc": utc_now(),
        "operation": args.operation,
        "environment_name": args.environment_name,
        "source_db_url_env": args.source_db_url_env,
        "source_url": url_status,
        "publication": publication,
        "slot": slot,
        "publication_table_count": len(tables),
        "publication_status": publication_status,
        "publication_count": publication_count,
        "slot_status": slot_status,
        "slot_count": slot_count,
        "cleanup_scope": CLEANUP_SCOPE if args.operation in {"teardown-dry-run", "teardown"} else "not_cleanup",
        "allowed_cleanup_resource_prefix": "nutsnews_backend_migration_",
        "preserved_hot_standby_resources": PRESERVED_HOT_STANDBY_RESOURCES,
        "teardown_actions": teardown_actions,
        "checks": checks,
        "blockers": blockers,
        "safe_metadata_only": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if args.enforce and status != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
