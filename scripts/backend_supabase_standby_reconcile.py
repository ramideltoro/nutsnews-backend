#!/usr/bin/env python3
"""Reconcile backend PostgreSQL primary with existing production Supabase standby.

The report intentionally emits safe metadata only: row counts, digests, schema
fingerprints, sequence positions, and status codes. The optional apply mode
copies rows from backend PostgreSQL to Supabase but never prints row data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "backend-supabase-standby-reconciliation.json"
DEFAULT_SOURCE_ENV = "NUTSNEWS_BACKEND_PRIMARY_DB_URL"
DEFAULT_TARGET_ENV = "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL"
APPLY_CONFIRMATION = "backfill-existing-production-supabase-from-backend-primary"
DEFAULT_BATCH_SIZE = 500
PSQL_TIMEOUT_SECONDS = 120
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class ReconcileError(Exception):
    """Safe failure marker; messages must not contain secrets or row data."""


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str

    @property
    def id(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def sql(self) -> str:
        return f"{quote_ident(self.schema)}.{quote_ident(self.name)}"


@dataclass(frozen=True)
class ParsedDbUrl:
    host: str
    port: str
    database: str
    username: str
    password: str
    sslmode: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def quote_ident(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ReconcileError("invalid_identifier")
    return '"' + value.replace('"', '""') + '"'


def parse_relation(value: str) -> Relation:
    parts = value.split(".")
    if len(parts) != 2:
        raise ReconcileError("invalid_relation")
    schema, name = parts
    if not IDENTIFIER_RE.fullmatch(schema) or not IDENTIFIER_RE.fullmatch(name):
        raise ReconcileError("invalid_relation")
    return Relation(schema=schema, name=name)


def parse_db_url(db_url: str) -> ParsedDbUrl:
    try:
        parsed = urlsplit(db_url)
    except ValueError as exc:
        raise ReconcileError("invalid_database_url") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ReconcileError("invalid_database_url")
    if not parsed.hostname:
        raise ReconcileError("invalid_database_url")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ReconcileError("invalid_database_url") from exc
    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not database or not username:
        raise ReconcileError("invalid_database_url")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    sslmode = query.get("sslmode", [None])[0]
    return ParsedDbUrl(
        host=parsed.hostname,
        port=str(port),
        database=database,
        username=username,
        password=password,
        sslmode=sslmode,
    )


def psql_env(db_url: str) -> dict[str, str]:
    target = parse_db_url(db_url)
    env = {
        "PATH": DEFAULT_PATH,
        "PGAPPNAME": "nutsnews-standby-reconcile",
        "PGCONNECT_TIMEOUT": "10",
        "PGDATABASE": target.database,
        "PGHOST": target.host,
        "PGPORT": target.port,
        "PGUSER": target.username,
    }
    if target.password:
        env["PGPASSWORD"] = target.password
    if target.sslmode:
        env["PGSSLMODE"] = target.sslmode
    return env


def run_psql(db_url: str, query: str, *, timeout: int = PSQL_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
    psql = shutil.which("psql", path=DEFAULT_PATH)
    if not psql:
        return None, "psql_not_installed"
    try:
        completed = subprocess.run(
            [
                psql,
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--quiet",
                "--tuples-only",
                "--no-align",
            ],
            input=query.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=psql_env(db_url),
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "query_timeout"
    if completed.returncode != 0:
        return None, "query_failed"
    return completed.stdout.decode("utf-8", errors="replace").strip(), None


def run_psql_to_file(db_url: str, query: str, output_path: Path, *, timeout: int = PSQL_TIMEOUT_SECONDS) -> str | None:
    psql = shutil.which("psql", path=DEFAULT_PATH)
    if not psql:
        return "psql_not_installed"
    try:
        with output_path.open("wb") as output:
            completed = subprocess.run(
                [
                    psql,
                    "--no-psqlrc",
                    "--set=ON_ERROR_STOP=1",
                    "--quiet",
                    "--command",
                    query,
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                env=psql_env(db_url),
                shell=False,
                timeout=timeout,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return "copy_export_failed"
    if completed.returncode != 0:
        return "copy_export_failed"
    return None


def run_psql_script(db_url: str, script_path: Path, *, timeout: int = PSQL_TIMEOUT_SECONDS) -> str | None:
    psql = shutil.which("psql", path=DEFAULT_PATH)
    if not psql:
        return "psql_not_installed"
    try:
        completed = subprocess.run(
            [
                psql,
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--quiet",
                "--file",
                str(script_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=psql_env(db_url),
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "copy_import_failed"
    if completed.returncode != 0:
        return "copy_import_failed"
    return None


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql_meta_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def relation_column_metadata_sql(relation: Relation) -> str:
    return f"""
    select coalesce(jsonb_agg(
      jsonb_build_object(
        'name', column_name,
        'ordinal_position', ordinal_position,
        'data_type', data_type,
        'udt_name', udt_name,
        'is_nullable', is_nullable,
        'is_generated', is_generated,
        'identity_generation', identity_generation,
        'default_md5', md5(coalesce(column_default, ''))
      )
      order by ordinal_position
    ), '[]'::jsonb)::text
    from information_schema.columns
    where table_schema = {sql_literal(relation.schema)}
      and table_name = {sql_literal(relation.name)}
    """


def usable_columns(metadata_text: str) -> list[str]:
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise ReconcileError("invalid_column_metadata") from exc
    columns: list[str] = []
    for item in metadata:
        name = str(item.get("name", ""))
        if not IDENTIFIER_RE.fullmatch(name):
            raise ReconcileError("invalid_column_metadata")
        if item.get("is_generated") == "ALWAYS":
            continue
        columns.append(name)
    return columns


def count_sql(relation: Relation) -> str:
    return f"select count(*)::bigint from {relation.sql}"


def row_checksum_sql(relation: Relation, columns: list[str], primary_key: list[str]) -> str:
    if not columns or not primary_key:
        raise ReconcileError("invalid_checksum_contract")
    select_columns = ", ".join(quote_ident(column) for column in columns)
    order_by = ", ".join(f"_nutsnews_row.{quote_ident(column)}" for column in primary_key)
    return f"""
    select md5(coalesce(string_agg(md5(row_to_json(_nutsnews_row)::text), ',' order by {order_by}), ''))
    from (
      select {select_columns}
      from {relation.sql}
      order by {', '.join(quote_ident(column) for column in primary_key)}
    ) as _nutsnews_row
    """


def schema_fingerprint_sql(table_names: list[str]) -> str:
    table_literals = ", ".join(sql_literal(parse_relation(table).name) for table in table_names)
    return f"""
    with selected_tables as (
      select c.oid, n.nspname, c.relname, c.relkind
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public'
        and c.relkind in ('r', 'p')
        and c.relname in ({table_literals})
    ),
    columns as (
      select jsonb_agg(jsonb_build_object(
        'schema', n.nspname,
        'table', c.relname,
        'column', a.attname,
        'position', a.attnum,
        'type', format_type(a.atttypid, a.atttypmod),
        'not_null', a.attnotnull,
        'generated', a.attgenerated,
        'identity', a.attidentity,
        'default_md5', md5(coalesce(pg_get_expr(d.adbin, d.adrelid), ''))
      ) order by n.nspname, c.relname, a.attnum) as value
      from pg_attribute a
      join pg_class c on c.oid = a.attrelid
      join pg_namespace n on n.oid = c.relnamespace
      left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
      where a.attnum > 0
        and not a.attisdropped
        and c.oid in (select oid from selected_tables)
    ),
    constraints as (
      select jsonb_agg(jsonb_build_object(
        'schema', n.nspname,
        'table', c.relname,
        'constraint', con.conname,
        'type', con.contype,
        'definition_md5', md5(pg_get_constraintdef(con.oid))
      ) order by n.nspname, c.relname, con.conname) as value
      from pg_constraint con
      join pg_class c on c.oid = con.conrelid
      join pg_namespace n on n.oid = c.relnamespace
      where c.oid in (select oid from selected_tables)
    ),
    indexes as (
      select jsonb_agg(jsonb_build_object(
        'schema', schemaname,
        'table', tablename,
        'index', indexname,
        'definition_md5', md5(indexdef)
      ) order by schemaname, tablename, indexname) as value
      from pg_indexes
      where schemaname = 'public'
        and tablename in ({table_literals})
    )
    select jsonb_build_object(
      'columns', coalesce((select value from columns), '[]'::jsonb),
      'constraints', coalesce((select value from constraints), '[]'::jsonb),
      'indexes', coalesce((select value from indexes), '[]'::jsonb)
    )::text
    """


def migration_contract_sql() -> str:
    return """
    select coalesce(jsonb_agg(jsonb_build_object(
      'legacy_schema_version', legacy_schema_version,
      'migration_head', migration_head,
      'expected_schema_fingerprint', expected_schema_fingerprint,
      'actual_schema_fingerprint', actual_schema_fingerprint
    ) order by migration_head), '[]'::jsonb)::text
    from public.nutsnews_migration_schema_contract()
    """


def sequence_state_sql(sequence: Relation) -> str:
    return f"""
    select jsonb_build_object(
      'last_value', sequence_state.last_value,
      'is_called', sequence_state.is_called,
      'increment_by', pg_sequences.increment_by
    )::text
    from {sequence.sql} as sequence_state
    join pg_sequences
      on pg_sequences.schemaname = {sql_literal(sequence.schema)}
     and pg_sequences.sequencename = {sql_literal(sequence.name)}
    """


def sequence_next_value(state: dict[str, Any]) -> int | None:
    last_value = state.get("last_value")
    increment_by = state.get("increment_by")
    is_called = state.get("is_called")
    if not isinstance(last_value, int) or not isinstance(increment_by, int) or not isinstance(is_called, bool):
        return None
    return last_value + increment_by if is_called else last_value


def query_value(db_url: str, query: str) -> tuple[str | None, str | None]:
    value, error = run_psql(db_url, query)
    return value, error


def validate_schema(manifest: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    table_names = [table["name"] for table in manifest.get("tables", [])]
    schema_query = schema_fingerprint_sql(table_names)
    source_schema, source_schema_error = query_value(source_db_url, schema_query)
    target_schema, target_schema_error = query_value(target_db_url, schema_query)
    source_contract, source_contract_error = query_value(source_db_url, migration_contract_sql())
    target_contract, target_contract_error = query_value(target_db_url, migration_contract_sql())

    errors = {
        "source_schema_error": source_schema_error,
        "target_schema_error": target_schema_error,
        "source_contract_error": source_contract_error,
        "target_contract_error": target_contract_error,
    }
    active_errors = {key: value for key, value in errors.items() if value}
    if active_errors:
        return {
            "id": "schema-fingerprint",
            "status": "fail",
            "errors": active_errors,
            "sensitivity": "metadata_hash_only",
        }

    assert source_schema is not None
    assert target_schema is not None
    assert source_contract is not None
    assert target_contract is not None
    source_schema_hash = sha256_text(source_schema)
    target_schema_hash = sha256_text(target_schema)
    source_contract_hash = sha256_text(source_contract)
    target_contract_hash = sha256_text(target_contract)
    status = "pass" if source_schema_hash == target_schema_hash and source_contract_hash == target_contract_hash else "fail"
    return {
        "id": "schema-fingerprint",
        "status": status,
        "source_schema_sha256": source_schema_hash,
        "target_schema_sha256": target_schema_hash,
        "source_schema_bytes": len(source_schema.encode("utf-8")),
        "target_schema_bytes": len(target_schema.encode("utf-8")),
        "source_migration_contract_sha256": source_contract_hash,
        "target_migration_contract_sha256": target_contract_hash,
        "manifest_schema_fingerprint": manifest.get("manifest_schema_fingerprint"),
        "sensitivity": "metadata_hash_only",
    }


def validate_table(table: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    relation = parse_relation(str(table.get("name", "")))
    primary_key = [str(column) for column in table.get("primary_key", [])]
    for column in primary_key:
        if not IDENTIFIER_RE.fullmatch(column):
            raise ReconcileError("invalid_primary_key")

    source_count, source_count_error = query_value(source_db_url, count_sql(relation))
    target_count, target_count_error = query_value(target_db_url, count_sql(relation))
    source_columns_text, source_columns_error = query_value(source_db_url, relation_column_metadata_sql(relation))
    target_columns_text, target_columns_error = query_value(target_db_url, relation_column_metadata_sql(relation))
    errors = {
        "source_count_error": source_count_error,
        "target_count_error": target_count_error,
        "source_columns_error": source_columns_error,
        "target_columns_error": target_columns_error,
    }
    active_errors = {key: value for key, value in errors.items() if value}
    if active_errors:
        return {
            "id": f"table.{relation.id}",
            "name": relation.id,
            "status": "fail",
            "errors": active_errors,
            "sensitivity": "aggregate_and_hash_only",
        }

    assert source_columns_text is not None
    assert target_columns_text is not None
    source_columns = usable_columns(source_columns_text)
    target_columns = usable_columns(target_columns_text)
    columns_match = source_columns_text == target_columns_text and source_columns == target_columns
    checksum_source = checksum_target = None
    checksum_source_error = checksum_target_error = None
    if columns_match:
        checksum_query = row_checksum_sql(relation, source_columns, primary_key)
        checksum_source, checksum_source_error = query_value(source_db_url, checksum_query)
        checksum_target, checksum_target_error = query_value(target_db_url, checksum_query)

    source_count_int = parse_int(source_count)
    target_count_int = parse_int(target_count)
    status = "pass"
    reasons: list[str] = []
    if source_count_int is None or target_count_int is None:
        status = "fail"
        reasons.append("invalid_count")
    elif source_count_int != target_count_int:
        status = "fail"
        reasons.append("row_count_mismatch")
    if not columns_match:
        status = "fail"
        reasons.append("column_metadata_mismatch")
    if checksum_source_error or checksum_target_error:
        status = "fail"
        reasons.append("checksum_query_failed")
    elif columns_match and checksum_source != checksum_target:
        status = "fail"
        reasons.append("row_checksum_mismatch")

    return {
        "id": f"table.{relation.id}",
        "name": relation.id,
        "status": status,
        "reasons": reasons,
        "source_count": source_count_int,
        "target_count": target_count_int,
        "target_lag_rows": None if source_count_int is None or target_count_int is None else max(0, source_count_int - target_count_int),
        "source_column_count": len(source_columns),
        "target_column_count": len(target_columns),
        "column_metadata_sha256_source": sha256_text(source_columns_text),
        "column_metadata_sha256_target": sha256_text(target_columns_text),
        "source_row_checksum": checksum_source,
        "target_row_checksum": checksum_target,
        "checksum_source_error": checksum_source_error,
        "checksum_target_error": checksum_target_error,
        "sensitivity": "aggregate_and_hash_only",
    }


def validate_sequence(item: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    sequence = parse_relation(str(item.get("name", "")))
    table = parse_relation(str(item.get("table", "")))
    column = str(item.get("column", ""))
    if not IDENTIFIER_RE.fullmatch(column):
        raise ReconcileError("invalid_sequence_column")
    source_state_text, source_state_error = query_value(source_db_url, sequence_state_sql(sequence))
    target_state_text, target_state_error = query_value(target_db_url, sequence_state_sql(sequence))
    source_max_text, source_max_error = query_value(source_db_url, f"select coalesce(max({quote_ident(column)}), 0)::bigint from {table.sql}")
    target_max_text, target_max_error = query_value(target_db_url, f"select coalesce(max({quote_ident(column)}), 0)::bigint from {table.sql}")
    errors = {
        "source_state_error": source_state_error,
        "target_state_error": target_state_error,
        "source_max_error": source_max_error,
        "target_max_error": target_max_error,
    }
    active_errors = {key: value for key, value in errors.items() if value}
    if active_errors:
        return {
            "id": f"sequence.{sequence.id}",
            "name": sequence.id,
            "table": table.id,
            "status": "fail",
            "errors": active_errors,
            "sensitivity": "sequence_metadata_only",
        }

    try:
        source_state = json.loads(source_state_text or "{}")
        target_state = json.loads(target_state_text or "{}")
    except json.JSONDecodeError as exc:
        raise ReconcileError("invalid_sequence_metadata") from exc
    source_max = parse_int(source_max_text)
    target_max = parse_int(target_max_text)
    source_last = source_state.get("last_value")
    target_last = target_state.get("last_value")
    target_next = sequence_next_value(target_state)
    source_next = sequence_next_value(source_state)

    reasons: list[str] = []
    if not isinstance(source_last, int):
        reasons.append("source_last_value_missing")
    if not isinstance(target_last, int):
        reasons.append("target_last_value_missing")
    if isinstance(source_last, int) and isinstance(target_last, int) and target_last < source_last:
        reasons.append("target_last_value_lt_source_last_value")
    if source_max is None or target_max is None or target_next is None:
        reasons.append("invalid_max_or_next_value")
    else:
        if target_next <= target_max:
            reasons.append("target_next_value_not_above_target_max_id")
        if target_next <= source_max:
            reasons.append("target_next_value_not_above_source_max_id")
    return {
        "id": f"sequence.{sequence.id}",
        "name": sequence.id,
        "table": table.id,
        "column": column,
        "status": "pass" if not reasons else "fail",
        "reasons": reasons,
        "source_last_value": source_last,
        "source_next_value": source_next,
        "target_last_value": target_last,
        "target_next_value": target_next,
        "source_max_id": source_max,
        "target_max_id": target_max,
        "sensitivity": "sequence_metadata_only",
    }


def validate_standby(manifest: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [validate_schema(manifest, source_db_url, target_db_url)]
    checks.extend(validate_table(table, source_db_url, target_db_url) for table in manifest.get("tables", []))
    checks.extend(validate_sequence(sequence, source_db_url, target_db_url) for sequence in manifest.get("sequences", []))
    failed = [check["id"] for check in checks if check["status"] == "fail"]
    return {
        "status": "fail" if failed else "pass",
        "check_count": len(checks),
        "failed_required_checks": failed,
        "checks": checks,
    }


def fetch_columns_for_apply(db_url: str, relation: Relation) -> list[str]:
    columns_text, columns_error = query_value(db_url, relation_column_metadata_sql(relation))
    if columns_error:
        raise ReconcileError("apply_column_metadata_unavailable")
    if columns_text is None:
        raise ReconcileError("apply_column_metadata_unavailable")
    return usable_columns(columns_text)


def apply_table_backfill(
    source_db_url: str,
    target_db_url: str,
    table: dict[str, Any],
    *,
    batch_size: int,
    workdir: Path,
) -> dict[str, Any]:
    relation = parse_relation(str(table["name"]))
    primary_key = [str(column) for column in table.get("primary_key", [])]
    if not primary_key:
        raise ReconcileError("missing_primary_key")
    for column in primary_key:
        if not IDENTIFIER_RE.fullmatch(column):
            raise ReconcileError("invalid_primary_key")
    match_key = [str(column) for column in table.get("backfill_key") or primary_key]
    if not match_key:
        raise ReconcileError("missing_backfill_key")
    for column in match_key:
        if not IDENTIFIER_RE.fullmatch(column):
            raise ReconcileError("invalid_backfill_key")

    columns = fetch_columns_for_apply(source_db_url, relation)
    if not columns:
        raise ReconcileError("missing_apply_columns")
    missing_match_columns = [column for column in match_key if column not in columns]
    if missing_match_columns:
        raise ReconcileError("backfill_key_column_missing")
    quoted_columns = ", ".join(quote_ident(column) for column in columns)
    temp_select_columns = ", ".join(f"s.{quote_ident(column)}" for column in columns)
    quoted_pk = ", ".join(quote_ident(column) for column in primary_key)
    non_pk_columns = [column for column in columns if column not in primary_key]
    if non_pk_columns:
        update_assignments = ", ".join(
            f"{quote_ident(column)} = s.{quote_ident(column)}" for column in non_pk_columns
        )
        update_sql = f"update {relation.sql} as t set {update_assignments}"
    else:
        update_sql = ""
    temp_table = quote_ident(f"standby_backfill_{relation.name}")
    match_predicate = " and ".join(
        f"t.{quote_ident(column)} is not distinct from s.{quote_ident(column)}" for column in match_key
    )
    pk_predicate = " and ".join(
        f"t.{quote_ident(column)} is not distinct from s.{quote_ident(column)}" for column in primary_key
    )
    delete_collision_sql = ""
    if match_key != primary_key:
        delete_collision_sql = (
            f"delete from {relation.sql} as t using {temp_table} as s "
            f"where {pk_predicate} and not ({match_predicate});"
        )
    delete_missing_sql = (
        f"delete from {relation.sql} as t "
        f"where not exists (select 1 from {temp_table} as s where {match_predicate});"
    )
    select_sql = f"select {quoted_columns} from {relation.sql} order by {quoted_pk}"
    source_count_text, source_count_error = query_value(source_db_url, count_sql(relation))
    if source_count_error:
        raise ReconcileError("apply_source_count_unavailable")
    source_count = parse_int(source_count_text)
    if source_count is None:
        raise ReconcileError("apply_source_count_unavailable")

    csv_path = workdir / f"{relation.name}.csv"
    script_path = workdir / f"{relation.name}.sql"
    copy_query = f"copy ({select_sql}) to stdout with (format csv)"
    export_error = run_psql_to_file(source_db_url, copy_query, csv_path)
    if export_error:
        raise ReconcileError(export_error)

    target_insert_sql = "\n".join(
        [
            "begin;",
            f"create temp table {temp_table} (like {relation.sql} including defaults) on commit drop;",
            f"\\copy {temp_table} ({quoted_columns}) from {psql_meta_literal(str(csv_path))} with (format csv)",
            f"alter table {relation.sql} disable trigger user;",
            *([delete_collision_sql] if delete_collision_sql else []),
            delete_missing_sql,
            *([f"{update_sql} from {temp_table} as s where {match_predicate};"] if update_sql else []),
            f"insert into {relation.sql} ({quoted_columns})",
            f"select {temp_select_columns}",
            f"from {temp_table} as s",
            f"where not exists (select 1 from {relation.sql} as t where {match_predicate});",
            f"alter table {relation.sql} enable trigger user;",
            "commit;",
            "",
        ]
    )
    script_path.write_text(target_insert_sql, encoding="utf-8")
    import_error = run_psql_script(target_db_url, script_path)
    if import_error:
        raise ReconcileError(f"{import_error}:{relation.id}")

    return {
        "table": relation.id,
        "status": "applied",
        "rows_seen": source_count,
        "batches": max(1, (source_count + batch_size - 1) // batch_size) if source_count else 0,
        "column_count": len(columns),
        "backfill_key": match_key,
        "mirror_delete_absent_target_rows": True,
        "user_triggers_disabled_during_apply": True,
        "sensitivity": "counts_only",
    }


def apply_sequence_safety(source_db_url: str, target_db_url: str, item: dict[str, Any]) -> dict[str, Any]:
    sequence = parse_relation(str(item["name"]))
    table = parse_relation(str(item["table"]))
    column = str(item["column"])
    source_state_text, source_state_error = query_value(source_db_url, sequence_state_sql(sequence))
    source_max_text, source_max_error = query_value(source_db_url, f"select coalesce(max({quote_ident(column)}), 0)::bigint from {table.sql}")
    if source_state_error or source_max_error:
        raise ReconcileError("source_sequence_metadata_unavailable")
    source_state = json.loads(source_state_text or "{}")
    source_last = source_state.get("last_value")
    source_max = parse_int(source_max_text)
    if not isinstance(source_last, int) or source_max is None:
        raise ReconcileError("invalid_source_sequence_metadata")
    target_max_text, target_max_error = query_value(target_db_url, f"select coalesce(max({quote_ident(column)}), 0)::bigint from {table.sql}")
    target_max = parse_int(target_max_text)
    if target_max_error or target_max is None:
        raise ReconcileError("target_sequence_metadata_unavailable")
    next_value = max(source_last, source_max, target_max) + 1
    _, setval_error = query_value(target_db_url, f"select setval({sql_literal(sequence.id)}::regclass, {next_value}, false)")
    if setval_error:
        raise ReconcileError("target_sequence_setval_failed")
    return {
        "sequence": sequence.id,
        "table": table.id,
        "column": column,
        "status": "set",
        "set_to_next_value": next_value,
        "sensitivity": "sequence_metadata_only",
    }


def apply_backfill(manifest: dict[str, Any], source_db_url: str, target_db_url: str, *, batch_size: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nutsnews-standby-backfill-") as tmpdir:
        workdir = Path(tmpdir)
        table_by_name = {table["name"]: table for table in manifest.get("tables", [])}
        ordered_names = manifest.get("backfill", {}).get("table_order", [])
        table_results = [
            apply_table_backfill(source_db_url, target_db_url, table_by_name[name], batch_size=batch_size, workdir=workdir)
            for name in ordered_names
            if name in table_by_name
        ]
        sequence_results = [
            apply_sequence_safety(source_db_url, target_db_url, sequence)
            for sequence in manifest.get("sequences", [])
        ]
    return {
        "status": "applied",
        "table_count": len(table_results),
        "sequence_count": len(sequence_results),
        "tables": table_results,
        "sequences": sequence_results,
        "safe_metadata_only": True,
    }


def skipped_validation(manifest: dict[str, Any], reason: str) -> dict[str, Any]:
    checks = [{"id": "schema-fingerprint", "status": "skipped_with_reason", "reason": reason}]
    checks.extend(
        {"id": f"table.{table.get('name')}", "status": "skipped_with_reason", "reason": reason}
        for table in manifest.get("tables", [])
    )
    checks.extend(
        {"id": f"sequence.{sequence.get('name')}", "status": "skipped_with_reason", "reason": reason}
        for sequence in manifest.get("sequences", [])
    )
    return {
        "status": "skipped_with_reason",
        "check_count": len(checks),
        "failed_required_checks": [],
        "checks": checks,
    }


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    target_db_url = os.environ.get(args.target_db_url_env, "").strip()
    credentials_present = bool(source_db_url and target_db_url)
    report: dict[str, Any] = {
        "status": "blocked",
        "mode": args.mode,
        "checked_at_utc": utc_now(),
        "issue": "ramideltoro/nutsnews#498",
        "epic": "ramideltoro/nutsnews#223",
        "manifest": str(manifest_path.relative_to(ROOT) if manifest_path.is_absolute() and manifest_path.is_relative_to(ROOT) else manifest_path),
        "manifest_version": manifest.get("version"),
        "source_label": "backend_postgres_primary",
        "target_label": "existing_production_supabase_standby",
        "source_db_url_env": args.source_db_url_env,
        "target_db_url_env": args.target_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "target_db_url_present": bool(target_db_url),
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }

    if args.offline or not credentials_present:
        reason = "offline mode" if args.offline else "missing_source_or_target_db_url"
        report["pre_reconciliation"] = skipped_validation(manifest, reason)
        report["post_reconciliation"] = report["pre_reconciliation"]
        report["backfill"] = {"status": "skipped_with_reason", "reason": reason, "safe_metadata_only": True}
        report["status"] = "skipped_with_reason" if args.offline else "blocked"
        return report, 0 if args.offline or not args.enforce else 1

    pre = validate_standby(manifest, source_db_url, target_db_url)
    report["pre_reconciliation"] = pre
    backfill: dict[str, Any]
    if args.mode == "apply-backfill":
        confirmation = os.environ.get("NUTSNEWS_STANDBY_RECONCILE_CONFIRMATION", "")
        if confirmation != APPLY_CONFIRMATION:
            raise ReconcileError("apply_confirmation_missing")
        backfill = apply_backfill(manifest, source_db_url, target_db_url, batch_size=args.batch_size)
        post = validate_standby(manifest, source_db_url, target_db_url)
    else:
        backfill = {
            "status": "not_required" if pre["status"] == "pass" else "required",
            "safe_metadata_only": True,
        }
        post = pre

    report["backfill"] = backfill
    report["post_reconciliation"] = post
    report["status"] = post["status"]
    return report, 0 if (not args.enforce or post["status"] == "pass") else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-db-url-env", default=DEFAULT_SOURCE_ENV)
    parser.add_argument("--target-db-url-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument("--output", default="")
    parser.add_argument("--mode", choices=("report", "apply-backfill"), default="report")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 5000:
        raise ReconcileError("invalid_batch_size")
    return args


def main_args(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv)
        report, exit_code = build_report(args)
    except ReconcileError as exc:
        report = {
            "status": "fail",
            "checked_at_utc": utc_now(),
            "error": str(exc),
            "safe_metadata_only": True,
        }
        exit_code = 1
    except Exception:
        report = {
            "status": "fail",
            "checked_at_utc": utc_now(),
            "error": "unexpected_reconciliation_error",
            "safe_metadata_only": True,
        }
        exit_code = 1

    text = json.dumps(report, indent=2, sort_keys=True)
    output = args.output if args is not None else ""
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return exit_code


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
