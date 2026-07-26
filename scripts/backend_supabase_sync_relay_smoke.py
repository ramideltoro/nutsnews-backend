#!/usr/bin/env python3
"""Prove the backend-to-Supabase sync relay catches up synthetic fixture rows.

This script is intended to run on the backend host from the protected
production-backend GitHub Environment. It writes only bounded synthetic rows to
the staging fixture tables on backend PostgreSQL, waits for the installed relay
to copy insert/update/delete state to the existing Supabase target, and emits a
safe metadata report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit


DEFAULT_TARGET_ENV = "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL"
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAMESPACE_RE = re.compile(r"^nutsnews-test-[a-z0-9][a-z0-9-]{5,96}$")
PSQL_TIMEOUT_SECONDS = 30


class SmokeError(Exception):
    """Safe failure marker; messages must not include secrets or row values."""


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


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_db_url(db_url: str) -> ParsedDbUrl:
    try:
        parsed = urlsplit(db_url)
    except ValueError as exc:
        raise SmokeError("target_database_url_invalid") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SmokeError("target_database_url_invalid")
    if not parsed.hostname:
        raise SmokeError("target_database_url_invalid")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise SmokeError("target_database_url_invalid") from exc
    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    sslmode = query.get("sslmode", [None])[0]
    if not database or not username or not password:
        raise SmokeError("target_database_url_invalid")
    if str(port) != "5432":
        raise SmokeError("target_database_not_direct_5432")
    if not parsed.hostname.endswith(".supabase.co"):
        raise SmokeError("target_database_not_supabase_direct_host")
    if "pooler.supabase.com" in parsed.hostname:
        raise SmokeError("target_database_url_is_pooler")
    if sslmode != "require":
        raise SmokeError("target_database_sslmode_required")
    return ParsedDbUrl(
        host=parsed.hostname,
        port=str(port),
        database=database,
        username=username,
        password=password,
        sslmode=sslmode,
    )


def target_psql_env(db_url: str) -> dict[str, str]:
    parsed = parse_db_url(db_url)
    return {
        "PATH": DEFAULT_PATH,
        "PGAPPNAME": "nutsnews-sync-relay-smoke",
        "PGCONNECT_TIMEOUT": "10",
        "PGDATABASE": parsed.database,
        "PGHOST": parsed.host,
        "PGPASSWORD": parsed.password,
        "PGPORT": parsed.port,
        "PGSSLMODE": parsed.sslmode or "require",
        "PGUSER": parsed.username,
    }


def psql_binary() -> str:
    psql = shutil.which("psql", path=DEFAULT_PATH)
    if not psql:
        raise SmokeError("psql_not_installed")
    return psql


def run_target_query(db_url: str, sql: str) -> str:
    try:
        completed = subprocess.run(
            [
                psql_binary(),
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--quiet",
                "--tuples-only",
                "--no-align",
            ],
            input=sql.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=target_psql_env(db_url),
            shell=False,
            timeout=PSQL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeError("target_query_failed") from exc
    if completed.returncode != 0:
        raise SmokeError("target_query_failed")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def run_source_query(database: str, sql: str) -> str:
    if not IDENTIFIER_RE.fullmatch(database):
        raise SmokeError("source_database_invalid")
    try:
        completed = subprocess.run(
            [
                "sudo",
                "-n",
                "-u",
                "postgres",
                psql_binary(),
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
            env={"PATH": DEFAULT_PATH, "PGAPPNAME": "nutsnews-sync-relay-smoke-source"},
            shell=False,
            timeout=PSQL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeError("source_query_failed") from exc
    if completed.returncode != 0:
        raise SmokeError("source_query_failed")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def source_upsert_sql(namespace: str, user_id: str) -> str:
    return f"""
    begin;
    insert into public.staging_fixture_runs(namespace, expires_at)
    values ({sql_literal(namespace)}, now() + interval '2 hours')
    on conflict (namespace) do update
      set expires_at = excluded.expires_at;
    insert into public.staging_fixture_users(namespace, user_id)
    values ({sql_literal(namespace)}, {sql_literal(user_id)}::uuid)
    on conflict (namespace) do update
      set user_id = excluded.user_id;
    commit;
    """


def source_delete_sql(namespace: str) -> str:
    return f"""
    delete from public.staging_fixture_runs
    where namespace = {sql_literal(namespace)};
    """


def target_user_sql(namespace: str) -> str:
    return f"""
    select coalesce((
      select user_id::text
      from public.staging_fixture_users
      where namespace = {sql_literal(namespace)}
    ), '');
    """


def target_count_sql(namespace: str) -> str:
    return f"""
    select (
      (select count(*) from public.staging_fixture_runs where namespace = {sql_literal(namespace)}) +
      (select count(*) from public.staging_fixture_users where namespace = {sql_literal(namespace)})
    )::text;
    """


def backend_public_5432_allowed() -> bool:
    ss = shutil.which("ss", path=DEFAULT_PATH)
    if not ss:
        return False
    try:
        completed = subprocess.run(
            [ss, "-ltn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": DEFAULT_PATH},
            shell=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if ":5432" not in line or "LISTEN" not in line:
            continue
        if "127.0.0.1:5432" in line or "[::1]:5432" in line or "::1:5432" in line:
            continue
        return True
    return False


def wait_until(
    label: str,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        if predicate():
            return {
                "status": "pass",
                "attempts": attempts,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        if time.monotonic() - started >= timeout_seconds:
            raise SmokeError(f"{label}_timeout")
        time.sleep(poll_interval_seconds)


def prove_relay(
    *,
    source_database: str,
    target_db_url: str,
    namespace: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    if not NAMESPACE_RE.fullmatch(namespace):
        raise SmokeError("namespace_invalid")
    first_user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{namespace}:insert"))
    second_user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{namespace}:update"))
    if first_user_id == second_user_id:
        raise SmokeError("fixture_uuid_invalid")

    report: dict[str, Any] = {
        "status": "blocked",
        "issue": "ramideltoro/nutsnews#499",
        "checked_at_utc": utc_now(),
        "source": "backend_postgres_primary_private",
        "target": "existing_production_supabase_standby",
        "safe_metadata_only": True,
        "backend_postgres_public_5432_allowed": backend_public_5432_allowed(),
        "app_worker_writes_to_supabase_before_failover": False,
        "operations": {},
        "blockers": [],
    }

    cleanup_source_needed = False
    try:
        run_source_query(source_database, source_upsert_sql(namespace, first_user_id))
        cleanup_source_needed = True
        report["operations"]["insert"] = wait_until(
            "insert_catchup",
            lambda: run_target_query(target_db_url, target_user_sql(namespace)) == first_user_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

        run_source_query(source_database, source_upsert_sql(namespace, second_user_id))
        report["operations"]["update"] = wait_until(
            "update_catchup",
            lambda: run_target_query(target_db_url, target_user_sql(namespace)) == second_user_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

        run_source_query(source_database, source_delete_sql(namespace))
        cleanup_source_needed = False
        report["operations"]["delete"] = wait_until(
            "delete_catchup",
            lambda: run_target_query(target_db_url, target_count_sql(namespace)) == "0",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    finally:
        if cleanup_source_needed:
            try:
                run_source_query(source_database, source_delete_sql(namespace))
            except SmokeError:
                pass

    report["status"] = "pass"
    report["checked_at_utc"] = utc_now()
    return report


def atomic_write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db-name", required=True)
    parser.add_argument("--target-db-url-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-interval-seconds", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enforce", action="store_true")
    return parser.parse_args(argv)


def main_args(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    blockers: list[str] = []
    target_db_url = os.environ.get(args.target_db_url_env, "")
    if not target_db_url:
        blockers.append("target_database_url_missing")
    if args.timeout_seconds < 30 or args.timeout_seconds > 1200:
        blockers.append("timeout_seconds_invalid")
    if args.poll_interval_seconds < 1 or args.poll_interval_seconds > 60:
        blockers.append("poll_interval_seconds_invalid")

    try:
        if blockers:
            raise SmokeError(blockers[0])
        report = prove_relay(
            source_database=args.source_db_name,
            target_db_url=target_db_url,
            namespace=args.namespace,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    except SmokeError as exc:
        report = {
            "status": "blocked",
            "issue": "ramideltoro/nutsnews#499",
            "checked_at_utc": utc_now(),
            "source": "backend_postgres_primary_private",
            "target": "existing_production_supabase_standby",
            "safe_metadata_only": True,
            "backend_postgres_public_5432_allowed": backend_public_5432_allowed(),
            "app_worker_writes_to_supabase_before_failover": False,
            "operations": {},
            "blockers": [str(exc)],
        }
    except Exception:
        report = {
            "status": "blocked",
            "issue": "ramideltoro/nutsnews#499",
            "checked_at_utc": utc_now(),
            "source": "backend_postgres_primary_private",
            "target": "existing_production_supabase_standby",
            "safe_metadata_only": True,
            "backend_postgres_public_5432_allowed": backend_public_5432_allowed(),
            "app_worker_writes_to_supabase_before_failover": False,
            "operations": {},
            "blockers": ["unexpected_failure"],
        }

    atomic_write_json(args.output, report)
    print(json.dumps({key: report[key] for key in ("status", "issue", "safe_metadata_only", "blockers")}, sort_keys=True))
    return 0 if report["status"] == "pass" or not args.enforce else 1


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
