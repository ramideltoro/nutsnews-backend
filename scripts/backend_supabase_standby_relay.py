#!/usr/bin/env python3
"""Backend-local relay from private PostgreSQL primary to Supabase standby.

The relay is intentionally pull/push from the backend side:

* backend PostgreSQL is never exposed for inbound Supabase connectivity;
* source triggers append bounded change events to a private local ledger;
* a locked systemd job drains the ledger outbound to Supabase;
* reports contain safe metadata only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "backend-supabase-standby-reconciliation.json"
DEFAULT_SOURCE_ENV = "NUTSNEWS_BACKEND_PRIMARY_DB_URL"
DEFAULT_TARGET_ENV = "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL"
DEFAULT_RELAY_SCHEMA = "nutsnews_standby_relay"
DEFAULT_RELAY_ROLE = "nutsnews_migration_replication"
DEFAULT_BATCH_SIZE = 100
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PSQL_TIMEOUT_SECONDS = 120
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
SAFE_STATUS_KEYS = ("status", "mode", "checked_at_utc", "safe_metadata_only", "blockers")
# Guardrail marker: target_apply_failure_does_not_ack_source_events.


class RelayError(Exception):
    """Safe failure marker; messages must not include secrets or row values."""


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
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quote_ident(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise RelayError("invalid_identifier")
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_relation(value: str) -> Relation:
    parts = value.split(".")
    if len(parts) != 2:
        raise RelayError("invalid_relation")
    schema, name = parts
    if not IDENTIFIER_RE.fullmatch(schema) or not IDENTIFIER_RE.fullmatch(name):
        raise RelayError("invalid_relation")
    return Relation(schema=schema, name=name)


def parse_db_url(db_url: str) -> ParsedDbUrl:
    try:
        parsed = urlsplit(db_url)
    except ValueError as exc:
        raise RelayError("invalid_database_url") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RelayError("invalid_database_url")
    if not parsed.hostname:
        raise RelayError("invalid_database_url")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise RelayError("invalid_database_url") from exc
    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not database or not username:
        raise RelayError("invalid_database_url")
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


def database_url_blockers(source_db_url: str, target_db_url: str) -> list[str]:
    blockers: list[str] = []
    try:
        source = parse_db_url(source_db_url)
    except RelayError:
        blockers.append("source_database_url_invalid")
    else:
        if source.host not in {"127.0.0.1", "localhost", "::1"}:
            blockers.append("source_database_not_loopback")
        if source.port != "5432":
            blockers.append("source_database_not_5432")

    try:
        target = parse_db_url(target_db_url)
    except RelayError:
        blockers.append("target_database_url_invalid")
    else:
        if target.port != "5432":
            blockers.append("target_database_not_direct_5432")
        if "pooler.supabase.com" in target.host:
            blockers.append("target_database_url_is_pooler")
        if not target.host.endswith(".supabase.co"):
            blockers.append("target_database_not_supabase_direct_host")
    return blockers


def psql_env(db_url: str, app_name: str = "nutsnews-standby-relay") -> dict[str, str]:
    target = parse_db_url(db_url)
    env = {
        "PATH": DEFAULT_PATH,
        "PGAPPNAME": app_name,
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


def run_psql_url(db_url: str, sql: str, *, timeout: int = PSQL_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
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
            input=sql.encode("utf-8"),
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


def run_psql_database(database: str, sql: str, *, timeout: int = PSQL_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
    if not IDENTIFIER_RE.fullmatch(database):
        return None, "invalid_database"
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
                "--dbname",
                database,
            ],
            input=sql.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": DEFAULT_PATH, "PGAPPNAME": "nutsnews-standby-relay-install"},
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "query_timeout"
    if completed.returncode != 0:
        return None, "query_failed"
    return completed.stdout.decode("utf-8", errors="replace").strip(), None


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_tables(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    tables = manifest.get("tables", [])
    if not isinstance(tables, list) or not tables:
        raise RelayError("missing_tables")
    return tables


def manifest_sequences(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    sequences = manifest.get("sequences", [])
    if not isinstance(sequences, list):
        raise RelayError("missing_sequences")
    return sequences


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("contract_id") != "backend-supabase-standby-reconciliation":
        raise RelayError("invalid_manifest_contract")
    if manifest.get("issue") != "ramideltoro/nutsnews#498":
        raise RelayError("invalid_reconciliation_prerequisite")
    if manifest.get("epic") != "ramideltoro/nutsnews#223":
        raise RelayError("invalid_epic_link")
    source = manifest.get("source", {})
    target = manifest.get("target", {})
    safety = manifest.get("safety", {})
    if source.get("label") != "backend_postgres_primary":
        raise RelayError("invalid_source")
    if source.get("public_5432_allowed") is not False:
        raise RelayError("source_public_5432_not_forbidden")
    if target.get("label") != "existing_production_supabase_standby":
        raise RelayError("invalid_target")
    if target.get("existing_production_supabase_project") is not True:
        raise RelayError("target_not_existing_supabase")
    if target.get("create_new_supabase_project") is not False:
        raise RelayError("new_supabase_project_not_forbidden")
    if target.get("create_nutsnews_standby_database") is not False:
        raise RelayError("standby_database_creation_not_forbidden")
    if safety.get("safe_metadata_only_report") is not True:
        raise RelayError("unsafe_report_policy")
    if safety.get("app_worker_writes_to_supabase_before_failover") is not False:
        raise RelayError("supabase_writes_not_failover_guarded")

    seen_tables: set[str] = set()
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table.get("name", "")))
        if relation.id in seen_tables:
            raise RelayError("duplicate_table")
        seen_tables.add(relation.id)
        primary_key = table.get("primary_key", [])
        if not isinstance(primary_key, list) or not primary_key:
            raise RelayError("missing_primary_key")
        for column in primary_key:
            if not isinstance(column, str) or not IDENTIFIER_RE.fullmatch(column):
                raise RelayError("invalid_primary_key")

    seen_sequences: set[str] = set()
    for sequence in manifest_sequences(manifest):
        relation = parse_relation(str(sequence.get("name", "")))
        if relation.id in seen_sequences:
            raise RelayError("duplicate_sequence")
        seen_sequences.add(relation.id)
        parse_relation(str(sequence.get("table", "")))
        column = str(sequence.get("column", ""))
        if not IDENTIFIER_RE.fullmatch(column):
            raise RelayError("invalid_sequence_column")


def relation_column_metadata_sql(relation: Relation) -> str:
    return f"""
    select coalesce(jsonb_agg(
      jsonb_build_object(
        'name', column_name,
        'ordinal_position', ordinal_position,
        'data_type', data_type,
        'udt_name', udt_name,
        'default_md5', md5(coalesce(column_default, '')),
        'is_generated', is_generated,
        'identity_generation', identity_generation
      )
      order by ordinal_position
    ), '[]'::jsonb)::text
    from information_schema.columns
    where table_schema = {sql_literal(relation.schema)}
      and table_name = {sql_literal(relation.name)}
    """


def usable_columns_from_metadata(metadata_text: str) -> list[str]:
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise RelayError("invalid_column_metadata") from exc
    if not isinstance(metadata, list) or not metadata:
        raise RelayError("invalid_column_metadata")
    columns: list[str] = []
    for item in metadata:
        if not isinstance(item, dict):
            raise RelayError("invalid_column_metadata")
        name = str(item.get("name", ""))
        if not IDENTIFIER_RE.fullmatch(name):
            raise RelayError("invalid_column_metadata")
        if item.get("is_generated") == "ALWAYS":
            continue
        if item.get("identity_generation") == "ALWAYS":
            continue
        columns.append(name)
    if not columns:
        raise RelayError("no_usable_columns")
    return columns


def target_usable_columns(target_db_url: str, relation: Relation) -> list[str]:
    text, error = run_psql_url(target_db_url, relation_column_metadata_sql(relation))
    if error or text is None:
        raise RelayError("target_column_metadata_unavailable")
    return usable_columns_from_metadata(text)


def source_identity_blockers(source_db_url: str, relay_schema: str) -> str:
    sql = f"select {quote_ident(relay_schema)}.identity_blockers()::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_identity_check_failed")
    return text


def source_schema_metadata(source_db_url: str, relay_schema: str) -> dict[str, Any]:
    sql = f"select {quote_ident(relay_schema)}.schema_metadata()::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_column_metadata_unavailable")
    try:
        metadata = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RelayError("source_column_metadata_unavailable") from exc
    if not isinstance(metadata, dict):
        raise RelayError("source_column_metadata_unavailable")
    return metadata


def source_identity_check_sql(manifest: dict[str, Any]) -> str:
    rows = []
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table["name"]))
        primary_key = [str(column) for column in table["primary_key"]]
        rows.append(
            "("
            f"{sql_literal(relation.schema)}, "
            f"{sql_literal(relation.name)}, "
            f"array[{', '.join(sql_literal(column) for column in primary_key)}]::text[]"
            ")"
        )
    values_sql = ", ".join(rows)
    return f"""
    with required(schema_name, table_name, primary_key) as (values {values_sql}),
    table_state as (
      select
        r.schema_name,
        r.table_name,
        r.primary_key,
        c.oid as relid,
        c.relreplident,
        coalesce((
          select array_agg(kcu.column_name::text order by kcu.ordinal_position)
          from information_schema.table_constraints tc
          join information_schema.key_column_usage kcu
            on kcu.constraint_schema = tc.constraint_schema
           and kcu.constraint_name = tc.constraint_name
           and kcu.table_schema = tc.table_schema
           and kcu.table_name = tc.table_name
          where tc.constraint_type = 'PRIMARY KEY'
            and tc.table_schema = r.schema_name
            and tc.table_name = r.table_name
        ), array[]::text[]) as actual_primary_key
      from required r
      left join pg_namespace n on n.nspname = r.schema_name
      left join pg_class c on c.relnamespace = n.oid and c.relname = r.table_name and c.relkind in ('r', 'p')
    )
    select coalesce(jsonb_agg(schema_name || '.' || table_name order by schema_name, table_name), '[]'::jsonb)::text
    from table_state
    where relid is null
       or actual_primary_key <> primary_key
       or (relreplident = 'd' and cardinality(actual_primary_key) = 0)
       or relreplident = 'n'
    """


def target_schema_check(manifest: dict[str, Any], source_db_url: str, target_db_url: str, relay_schema: str) -> list[str]:
    blockers: list[str] = []
    try:
        source_missing = source_identity_blockers(source_db_url, relay_schema)
        source_error = None
    except RelayError:
        source_missing = None
        source_error = "source_identity_check_failed"
    target_missing, target_error = run_psql_url(target_db_url, source_identity_check_sql(manifest))
    if source_error:
        blockers.append("source_identity_check_failed")
    if target_error:
        blockers.append("target_identity_check_failed")
    if source_missing and source_missing != "[]":
        blockers.append("source_unsafe_table_identity")
    if target_missing and target_missing != "[]":
        blockers.append("target_unsafe_table_identity")
    if blockers:
        return blockers
    try:
        source_metadata_by_relation = source_schema_metadata(source_db_url, relay_schema)
    except RelayError as exc:
        return [str(exc)]
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table["name"]))
        target_metadata, target_metadata_error = run_psql_url(target_db_url, relation_column_metadata_sql(relation))
        if target_metadata_error or target_metadata is None:
            blockers.append("target_column_metadata_unavailable")
            break
        try:
            target_metadata_json = json.loads(target_metadata)
        except json.JSONDecodeError:
            blockers.append("target_column_metadata_unavailable")
            break
        if source_metadata_by_relation.get(relation.id) != target_metadata_json:
            blockers.append("schema_mismatch")
            break
    return blockers


def source_trigger_function_sql(relay_schema: str) -> str:
    schema = quote_ident(relay_schema)
    return f"""
    create or replace function {schema}.record_change()
    returns trigger
    language plpgsql
    security definer
    set search_path = pg_catalog, {schema}, public
    as $$
    declare
      payload jsonb;
      key_index integer;
      key_name text;
      primary_key jsonb := '{{}}'::jsonb;
    begin
      if tg_op = 'DELETE' then
        payload := to_jsonb(old);
      else
        payload := to_jsonb(new);
      end if;

      if tg_nargs < 2 then
        raise exception 'relay trigger missing primary key arguments';
      end if;

      for key_index in 1..tg_nargs - 1 loop
        key_name := tg_argv[key_index];
        if key_name is null or not (payload ? key_name) then
          raise exception 'relay trigger primary key missing';
        end if;
        primary_key := primary_key || jsonb_build_object(key_name, payload -> key_name);
      end loop;

      insert into {schema}.events(relation_name, operation, primary_key, row_data, source_xid)
      values (
        tg_argv[0],
        lower(tg_op),
        primary_key,
        case when tg_op = 'DELETE' then null else payload end,
        txid_current()
      );

      if tg_op = 'DELETE' then
        return old;
      end if;
      return new;
    end;
    $$;
    """


def source_status_functions_sql(manifest: dict[str, Any], relay_schema: str, relay_role: str) -> str:
    schema = quote_ident(relay_schema)
    role = quote_ident(relay_role)
    metadata_rows = []
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table["name"]))
        metadata_rows.append(
            "("
            f"{sql_literal(relation.id)}, "
            f"{sql_literal(relation.schema)}, "
            f"{sql_literal(relation.name)}"
            ")"
        )
    metadata_values_sql = ", ".join(metadata_rows)
    sequence_rows = []
    for sequence in manifest_sequences(manifest):
        sequence_relation = parse_relation(str(sequence["name"]))
        table_relation = parse_relation(str(sequence["table"]))
        column = str(sequence["column"])
        sequence_rows.append(
            "select jsonb_build_object("
            f"'name', {sql_literal(sequence_relation.id)}, "
            f"'table', {sql_literal(table_relation.id)}, "
            f"'column', {sql_literal(column)}, "
            "'last_value', sequence_state.last_value, "
            "'is_called', sequence_state.is_called, "
            "'increment_by', pg_sequences.increment_by, "
            f"'max_id', (select coalesce(max({quote_ident(column)}), 0)::bigint from {table_relation.sql})"
            f") as item from {sequence_relation.sql} as sequence_state "
            "join pg_sequences "
            f"on pg_sequences.schemaname = {sql_literal(sequence_relation.schema)} "
            f"and pg_sequences.sequencename = {sql_literal(sequence_relation.name)}"
        )
    sequence_union = "\nunion all\n".join(sequence_rows) if sequence_rows else "select null::jsonb as item where false"
    return f"""
    create or replace function {schema}.identity_blockers()
    returns text
    language sql
    security definer
    set search_path = pg_catalog, information_schema, public
    as $$
      {source_identity_check_sql(manifest)}
    $$;

    create or replace function {schema}.schema_metadata()
    returns text
    language sql
    security definer
    set search_path = pg_catalog, information_schema, public
    as $$
      with required(relation_name, schema_name, table_name) as (
        values {metadata_values_sql}
      ),
      metadata as (
        select
          required.relation_name,
          coalesce(jsonb_agg(
            jsonb_build_object(
              'name', column_state.column_name,
              'ordinal_position', column_state.ordinal_position,
              'data_type', column_state.data_type,
              'udt_name', column_state.udt_name,
              'default_md5', md5(coalesce(column_state.column_default, '')),
              'is_generated', column_state.is_generated,
              'identity_generation', column_state.identity_generation
            )
            order by column_state.ordinal_position
          ) filter (where column_state.column_name is not null), '[]'::jsonb) as columns
        from required
        left join information_schema.columns as column_state
          on column_state.table_schema = required.schema_name
         and column_state.table_name = required.table_name
        group by required.relation_name
      )
      select coalesce(jsonb_object_agg(relation_name, columns order by relation_name), '{{}}'::jsonb)::text
      from metadata;
    $$;

    create or replace function {schema}.fetch_batch(batch_limit integer)
    returns jsonb
    language sql
    security definer
    set search_path = pg_catalog, {schema}, public
    as $$
      select coalesce(jsonb_agg(jsonb_build_object(
        'id', id,
        'relation_name', relation_name,
        'operation', operation,
        'primary_key', primary_key,
        'row_data', row_data
      ) order by id), '[]'::jsonb)
      from (
        select id, relation_name, operation, primary_key, row_data
        from {schema}.events
        order by id
        limit greatest(1, least(batch_limit, 1000))
      ) events;
    $$;

    create or replace function {schema}.ack_events(through_id bigint)
    returns bigint
    language sql
    security definer
    set search_path = pg_catalog, {schema}, public
    as $$
      with deleted as (
        delete from {schema}.events
        where id <= through_id
        returning 1
      )
      select count(*)::bigint from deleted;
    $$;

    create or replace function {schema}.relay_status()
    returns jsonb
    language sql
    security definer
    set search_path = pg_catalog, {schema}, public
    as $$
      select jsonb_build_object(
        'pending_events', count(*)::bigint,
        'oldest_event_age_seconds', coalesce(extract(epoch from clock_timestamp() - min(queued_at))::bigint, 0),
        'max_event_id_present', max(id) is not null
      )
      from {schema}.events;
    $$;

    create or replace function {schema}.sequence_snapshot()
    returns jsonb
    language sql
    security definer
    set search_path = pg_catalog, {schema}, public
    as $$
      select coalesce(jsonb_agg(item order by item->>'name'), '[]'::jsonb)
      from (
        {sequence_union}
      ) sequences
      where item is not null;
    $$;

    revoke all on schema {schema} from public;
    grant usage on schema {schema} to {role};
    grant execute on function {schema}.identity_blockers() to {role};
    grant execute on function {schema}.schema_metadata() to {role};
    grant execute on function {schema}.fetch_batch(integer) to {role};
    grant execute on function {schema}.ack_events(bigint) to {role};
    grant execute on function {schema}.relay_status() to {role};
    grant execute on function {schema}.sequence_snapshot() to {role};
    """


def source_install_sql(manifest: dict[str, Any], *, relay_schema: str, relay_role: str) -> str:
    validate_manifest(manifest)
    schema = quote_ident(relay_schema)
    statements = [
        "begin;",
        f"create schema if not exists {schema};",
        f"""
        create table if not exists {schema}.events (
          id bigserial primary key,
          queued_at timestamptz not null default clock_timestamp(),
          relation_name text not null,
          operation text not null check (operation in ('insert', 'update', 'delete')),
          primary_key jsonb not null,
          row_data jsonb,
          source_xid bigint not null,
          attempt_count integer not null default 0
        );
        """,
        f"create index if not exists {relay_schema}_events_relation_id_idx on {schema}.events (relation_name, id);",
        f"""
        create table if not exists {schema}.control (
          singleton boolean primary key default true check (singleton),
          installed_at timestamptz not null default clock_timestamp(),
          manifest_schema_fingerprint text not null,
          relay_contract text not null
        );
        """,
        f"""
        insert into {schema}.control(singleton, manifest_schema_fingerprint, relay_contract)
        values (true, {sql_literal(str(manifest.get("manifest_schema_fingerprint", "")))}, 'backend-to-supabase-standby-relay')
        on conflict (singleton) do update set
          manifest_schema_fingerprint = excluded.manifest_schema_fingerprint,
          relay_contract = excluded.relay_contract;
        """,
        source_trigger_function_sql(relay_schema),
        source_status_functions_sql(manifest, relay_schema, relay_role),
    ]
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table["name"]))
        primary_key = [str(column) for column in table["primary_key"]]
        trigger_args = [relation.id, *primary_key]
        arg_sql = ", ".join(sql_literal(value) for value in trigger_args)
        statements.append(f"drop trigger if exists nutsnews_standby_relay_capture on {relation.sql};")
        statements.append(
            f"""
            create trigger nutsnews_standby_relay_capture
            after insert or update or delete on {relation.sql}
            for each row execute function {schema}.record_change({arg_sql});
            """
        )
    statements.extend(["commit;", ""])
    return "\n".join(statements)


def source_remove_sql(manifest: dict[str, Any], *, relay_schema: str) -> str:
    schema = quote_ident(relay_schema)
    statements = ["begin;"]
    for table in manifest_tables(manifest):
        relation = parse_relation(str(table["name"]))
        statements.append(f"drop trigger if exists nutsnews_standby_relay_capture on {relation.sql};")
    statements.append(f"drop schema if exists {schema} cascade;")
    statements.extend(["commit;", ""])
    return "\n".join(statements)


def fetch_events(source_db_url: str, relay_schema: str, batch_size: int) -> list[dict[str, Any]]:
    sql = f"select {quote_ident(relay_schema)}.fetch_batch({int(batch_size)})::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_fetch_batch_failed")
    try:
        events = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise RelayError("source_fetch_batch_invalid") from exc
    if not isinstance(events, list):
        raise RelayError("source_fetch_batch_invalid")
    return events


def ack_events(source_db_url: str, relay_schema: str, through_id: int) -> int:
    sql = f"select {quote_ident(relay_schema)}.ack_events({int(through_id)})::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_ack_failed")
    try:
        return int(text)
    except ValueError as exc:
        raise RelayError("source_ack_invalid") from exc


def relay_status(source_db_url: str, relay_schema: str) -> dict[str, Any]:
    sql = f"select {quote_ident(relay_schema)}.relay_status()::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_status_failed")
    try:
        status = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RelayError("source_status_invalid") from exc
    if not isinstance(status, dict):
        raise RelayError("source_status_invalid")
    return status


def source_sequence_snapshot(source_db_url: str, relay_schema: str) -> list[dict[str, Any]]:
    sql = f"select {quote_ident(relay_schema)}.sequence_snapshot()::text"
    text, error = run_psql_url(source_db_url, sql)
    if error or text is None:
        raise RelayError("source_sequence_snapshot_failed")
    try:
        snapshot = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RelayError("source_sequence_snapshot_invalid") from exc
    if not isinstance(snapshot, list):
        raise RelayError("source_sequence_snapshot_invalid")
    return snapshot


def event_relation(manifest: dict[str, Any], event: dict[str, Any]) -> tuple[Relation, list[str]]:
    relation_name = event.get("relation_name")
    if not isinstance(relation_name, str):
        raise RelayError("event_relation_invalid")
    for table in manifest_tables(manifest):
        if table.get("name") == relation_name:
            relation = parse_relation(relation_name)
            primary_key = [str(column) for column in table.get("primary_key", [])]
            return relation, primary_key
    raise RelayError("event_relation_not_in_manifest")


def build_upsert_sql(relation: Relation, primary_key: list[str], columns: list[str], row_data: dict[str, Any]) -> str:
    if not isinstance(row_data, dict):
        raise RelayError("event_row_invalid")
    for column in primary_key:
        if column not in columns:
            raise RelayError("primary_key_missing_from_target_columns")
    insert_columns = [column for column in columns if column in row_data]
    if not insert_columns:
        raise RelayError("event_row_has_no_usable_columns")
    column_sql = ", ".join(quote_ident(column) for column in insert_columns)
    select_sql = ", ".join(quote_ident(column) for column in insert_columns)
    conflict_sql = ", ".join(quote_ident(column) for column in primary_key)
    update_columns = [column for column in insert_columns if column not in set(primary_key)]
    if update_columns:
        update_sql = "do update set " + ", ".join(
            f"{quote_ident(column)} = excluded.{quote_ident(column)}" for column in update_columns
        )
    else:
        update_sql = "do nothing"
    payload = json.dumps(row_data, separators=(",", ":"), sort_keys=True)
    return f"""
    with _payload as (
      select * from jsonb_populate_record(null::{relation.sql}, {sql_literal(payload)}::jsonb)
    )
    insert into {relation.sql} ({column_sql})
    select {select_sql} from _payload
    on conflict ({conflict_sql}) {update_sql};
    """


def build_delete_sql(relation: Relation, primary_key: list[str], primary_key_data: dict[str, Any]) -> str:
    if not isinstance(primary_key_data, dict):
        raise RelayError("event_primary_key_invalid")
    for column in primary_key:
        if column not in primary_key_data:
            raise RelayError("event_primary_key_missing")
    payload = json.dumps(primary_key_data, separators=(",", ":"), sort_keys=True)
    predicates = " and ".join(f"target.{quote_ident(column)} is not distinct from _pk.{quote_ident(column)}" for column in primary_key)
    return f"""
    with _pk as (
      select * from jsonb_populate_record(null::{relation.sql}, {sql_literal(payload)}::jsonb)
    )
    delete from {relation.sql} as target
    using _pk
    where {predicates};
    """


def build_apply_sql(manifest: dict[str, Any], target_db_url: str, events: list[dict[str, Any]]) -> str:
    relation_columns: dict[str, list[str]] = {}
    statements = [
        "begin;",
        "set local lock_timeout = '5s';",
        "set local statement_timeout = '30s';",
    ]
    for event in events:
        if not isinstance(event, dict):
            raise RelayError("event_invalid")
        relation, primary_key = event_relation(manifest, event)
        columns = relation_columns.get(relation.id)
        if columns is None:
            columns = target_usable_columns(target_db_url, relation)
            relation_columns[relation.id] = columns
        operation = event.get("operation")
        if operation in {"insert", "update"}:
            row_data = event.get("row_data")
            if not isinstance(row_data, dict):
                raise RelayError("event_row_invalid")
            statements.append(build_upsert_sql(relation, primary_key, columns, row_data))
        elif operation == "delete":
            primary_key_data = event.get("primary_key")
            if not isinstance(primary_key_data, dict):
                raise RelayError("event_primary_key_invalid")
            statements.append(build_delete_sql(relation, primary_key, primary_key_data))
        else:
            raise RelayError("event_operation_invalid")
    statements.extend(["commit;", ""])
    return "\n".join(statements)


def apply_events(manifest: dict[str, Any], target_db_url: str, events: list[dict[str, Any]]) -> int:
    if not events:
        return 0
    sql = build_apply_sql(manifest, target_db_url, events)
    _, error = run_psql_url(target_db_url, sql)
    if error:
        raise RelayError("target_apply_failed")
    return len(events)


def sequence_next_value(state: dict[str, Any]) -> int | None:
    last_value = state.get("last_value")
    increment_by = state.get("increment_by")
    is_called = state.get("is_called")
    if not isinstance(last_value, int) or not isinstance(increment_by, int) or not isinstance(is_called, bool):
        return None
    return last_value + increment_by if is_called else last_value


def target_sequence_state_sql(sequence: Relation, table: Relation, column: str) -> str:
    return f"""
    select jsonb_build_object(
      'last_value', sequence_state.last_value,
      'is_called', sequence_state.is_called,
      'increment_by', pg_sequences.increment_by,
      'max_id', (select coalesce(max({quote_ident(column)}), 0)::bigint from {table.sql})
    )::text
    from {sequence.sql} as sequence_state
    join pg_sequences
      on pg_sequences.schemaname = {sql_literal(sequence.schema)}
     and pg_sequences.sequencename = {sql_literal(sequence.name)}
    """


def advance_sequences(target_db_url: str, source_snapshot: list[dict[str, Any]]) -> dict[str, int]:
    checked = 0
    advanced = 0
    statements = ["begin;", "set local statement_timeout = '30s';"]
    for item in source_snapshot:
        if not isinstance(item, dict):
            raise RelayError("source_sequence_snapshot_invalid")
        sequence = parse_relation(str(item.get("name", "")))
        table = parse_relation(str(item.get("table", "")))
        column = str(item.get("column", ""))
        if not IDENTIFIER_RE.fullmatch(column):
            raise RelayError("invalid_sequence_column")
        checked += 1
        source_next = sequence_next_value(item)
        source_max = item.get("max_id")
        target_text, target_error = run_psql_url(target_db_url, target_sequence_state_sql(sequence, table, column))
        if target_error or target_text is None:
            raise RelayError("target_sequence_state_failed")
        try:
            target_state = json.loads(target_text)
        except json.JSONDecodeError as exc:
            raise RelayError("target_sequence_state_invalid") from exc
        target_next = sequence_next_value(target_state)
        target_max = target_state.get("max_id")
        if not all(isinstance(value, int) for value in (source_next, source_max, target_next, target_max)):
            raise RelayError("sequence_metadata_invalid")
        desired_next = max(int(source_next), int(source_max) + 1, int(target_next), int(target_max) + 1)
        if int(target_next) < desired_next:
            statements.append(f"select setval({sql_literal(sequence.id)}::regclass, {desired_next}, false);")
            advanced += 1
    statements.extend(["commit;", ""])
    if advanced:
        _, error = run_psql_url(target_db_url, "\n".join(statements))
        if error:
            raise RelayError("target_sequence_advance_failed")
    return {"checked": checked, "advanced": advanced}


def run_offline(manifest: dict[str, Any], mode: str) -> dict[str, Any]:
    validate_manifest(manifest)
    return {
        "status": "skipped_with_reason",
        "mode": mode,
        "checked_at_utc": utc_now(),
        "reason": "offline",
        "tables": len(manifest_tables(manifest)),
        "sequences": len(manifest_sequences(manifest)),
        "source": "backend_postgres_primary_private",
        "target": "existing_production_supabase_standby",
        "backend_public_5432_allowed": False,
        "supports": {
            "insert": True,
            "update": True,
            "delete": True,
            "sequence_readiness": True,
        },
        "safe_metadata_only": True,
        "blockers": [],
    }


def install_source(manifest: dict[str, Any], source_db_name: str, relay_schema: str, relay_role: str) -> dict[str, Any]:
    sql = source_install_sql(manifest, relay_schema=relay_schema, relay_role=relay_role)
    _, error = run_psql_database(source_db_name, sql)
    blockers = [error] if error else []
    return {
        "status": "pass" if not blockers else "blocked",
        "mode": "install-source",
        "checked_at_utc": utc_now(),
        "source_database": "backend_postgres_primary",
        "relay_schema": relay_schema,
        "tables_configured": len(manifest_tables(manifest)) if not blockers else 0,
        "sequences_configured": len(manifest_sequences(manifest)) if not blockers else 0,
        "safe_metadata_only": True,
        "blockers": blockers,
    }


def remove_source(manifest: dict[str, Any], source_db_name: str, relay_schema: str) -> dict[str, Any]:
    sql = source_remove_sql(manifest, relay_schema=relay_schema)
    _, error = run_psql_database(source_db_name, sql)
    blockers = [error] if error else []
    return {
        "status": "pass" if not blockers else "blocked",
        "mode": "remove-source",
        "checked_at_utc": utc_now(),
        "source_database": "backend_postgres_primary",
        "relay_schema": relay_schema,
        "safe_metadata_only": True,
        "blockers": blockers,
    }


def run_status(manifest: dict[str, Any], source_db_url: str, target_db_url: str, relay_schema: str) -> dict[str, Any]:
    validate_manifest(manifest)
    blockers = database_url_blockers(source_db_url, target_db_url)
    if not blockers:
        blockers = target_schema_check(manifest, source_db_url, target_db_url, relay_schema)
    status: dict[str, Any] = {}
    if not blockers:
        try:
            status = relay_status(source_db_url, relay_schema)
        except RelayError as exc:
            blockers.append(str(exc))
    return {
        "status": "pass" if not blockers else "blocked",
        "mode": "status",
        "checked_at_utc": utc_now(),
        "source": "backend_postgres_primary_private",
        "target": "existing_production_supabase_standby",
        "relay": status,
        "safe_metadata_only": True,
        "blockers": blockers,
    }


def run_once(
    manifest: dict[str, Any],
    source_db_url: str,
    target_db_url: str,
    relay_schema: str,
    batch_size: int,
) -> dict[str, Any]:
    validate_manifest(manifest)
    blockers = database_url_blockers(source_db_url, target_db_url)
    if not blockers:
        blockers = target_schema_check(manifest, source_db_url, target_db_url, relay_schema)
    fetched = applied = acked = 0
    sequence_result = {"checked": 0, "advanced": 0}
    remaining: dict[str, Any] = {}
    if not blockers:
        try:
            events = fetch_events(source_db_url, relay_schema, batch_size)
            fetched = len(events)
            if events:
                applied = apply_events(manifest, target_db_url, events)
                max_id = max(int(event["id"]) for event in events)
                acked = ack_events(source_db_url, relay_schema, max_id)
            sequence_result = advance_sequences(target_db_url, source_sequence_snapshot(source_db_url, relay_schema))
            remaining = relay_status(source_db_url, relay_schema)
        except RelayError as exc:
            blockers.append(str(exc))
    return {
        "status": "pass" if not blockers else "blocked",
        "mode": "run-once",
        "checked_at_utc": utc_now(),
        "source": "backend_postgres_primary_private",
        "target": "existing_production_supabase_standby",
        "events": {
            "fetched": fetched,
            "applied": applied,
            "acked": acked,
        },
        "sequences": sequence_result,
        "relay": remaining,
        "safe_metadata_only": True,
        "blockers": blockers,
    }


def atomic_write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(path)


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "install-source", "remove-source", "status", "run-once"), default="status")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-db-url-env", default=DEFAULT_SOURCE_ENV)
    parser.add_argument("--target-db-url-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument("--source-db-name", default="")
    parser.add_argument("--relay-schema", default=DEFAULT_RELAY_SCHEMA)
    parser.add_argument("--relay-role", default=DEFAULT_RELAY_ROLE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--lock-file", type=Path, default=Path("/run/nutsnews-supabase-standby-relay/relay.lock"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    report: dict[str, Any]
    try:
        if args.mode == "offline":
            report = run_offline(manifest, args.mode)
        elif args.mode in {"install-source", "remove-source"}:
            if not args.source_db_name:
                raise RelayError("missing_source_db_name")
            if args.mode == "install-source":
                report = install_source(manifest, args.source_db_name, args.relay_schema, args.relay_role)
            else:
                report = remove_source(manifest, args.source_db_name, args.relay_schema)
        else:
            source_db_url = os.environ.get(args.source_db_url_env, "").strip()
            target_db_url = os.environ.get(args.target_db_url_env, "").strip()
            if not source_db_url:
                raise RelayError("missing_source_db_url")
            if not target_db_url:
                raise RelayError("missing_target_db_url")
            if args.batch_size < 1 or args.batch_size > 1000:
                raise RelayError("invalid_batch_size")
            args.lock_file.parent.mkdir(parents=True, exist_ok=True)
            with args.lock_file.open("w", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if args.mode == "status":
                    report = run_status(manifest, source_db_url, target_db_url, args.relay_schema)
                else:
                    report = run_once(manifest, source_db_url, target_db_url, args.relay_schema, args.batch_size)
    except BlockingIOError:
        report = {
            "status": "blocked",
            "mode": args.mode,
            "checked_at_utc": utc_now(),
            "safe_metadata_only": True,
            "blockers": ["relay_lock_busy"],
        }
    except RelayError as exc:
        report = {
            "status": "blocked",
            "mode": args.mode,
            "checked_at_utc": utc_now(),
            "safe_metadata_only": True,
            "blockers": [str(exc)],
        }

    if args.output is not None:
        atomic_write_json(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.enforce and report.get("status") not in {"pass", "skipped_with_reason"} else 0


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
