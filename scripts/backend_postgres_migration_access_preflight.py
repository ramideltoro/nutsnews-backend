#!/usr/bin/env python3
"""Run safe backend PostgreSQL migration access preflight checks."""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_HOST = "65.75.201.18"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ROLE_NAMES = [
    "nutsnews_app",
    "nutsnews_readonly",
    "nutsnews_migration_restore",
    "nutsnews_migration_validation",
    "nutsnews_migration_replication",
    "nutsnews_app_rehearsal",
    "anon",
    "authenticated",
    "service_role",
]
PRIMARY_SHADOW_CONNECT_ROLES = [
    "nutsnews_app",
    "nutsnews_readonly",
    "nutsnews_migration_validation",
    "nutsnews_migration_replication",
    "nutsnews_worker_api",
]
WORKER_UPLIFT_STAGE_SCHEMAS = [
    ("scheduler", "worker_uplift_scheduler"),
    ("fetcher", "worker_uplift_fetcher"),
    ("canonicalizer", "worker_uplift_canonicalizer"),
    ("enrichment", "worker_uplift_enrichment"),
    ("approval", "worker_uplift_approval"),
    ("translation", "worker_uplift_translation"),
    ("persistence", "worker_uplift_persistence"),
    ("publication", "worker_uplift_publication"),
]
WORKER_UPLIFT_SCHEMAS = [schema for _stage, schema in WORKER_UPLIFT_STAGE_SCHEMAS] + [
    "worker_uplift_final",
    "worker_uplift_views",
]
POSTGRES_TRUE_VALUES = {"1", "on", "t", "true", "yes"}
WORKER_API_ROLE = "nutsnews_worker_api"
WORKER_UPLIFT_FINAL_SCHEMA = "worker_uplift_final"


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_ssh(host: str, user: str, key: str, known_hosts: str, command: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [
            "ssh",
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            f"{user}@{host}",
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_postgres_bool(value: str) -> bool:
    return value.strip().lower() in POSTGRES_TRUE_VALUES


def parse_worker_uplift_stage_roles(values: list[str]) -> list[tuple[str, str, str]]:
    if not values:
        return []

    schema_by_stage = dict(WORKER_UPLIFT_STAGE_SCHEMAS)
    role_by_stage: dict[str, str] = {}
    for value in values:
        if ":" in value:
            stage, role_name = value.split(":", 1)
        elif "=" in value:
            stage, role_name = value.split("=", 1)
        else:
            raise SystemExit("--worker-uplift-stage-role values must use stage:role or stage=role")
        stage = stage.strip()
        role_name = role_name.strip()
        if stage not in schema_by_stage:
            raise SystemExit(f"Unsupported worker-uplift stage role: {stage}")
        if stage in role_by_stage:
            raise SystemExit(f"Duplicate worker-uplift stage role: {stage}")
        if not IDENTIFIER_RE.match(role_name):
            raise SystemExit(f"Worker-uplift stage role must be a safe PostgreSQL identifier: {stage}")
        role_by_stage[stage] = role_name

    missing_stages = [stage for stage, _schema in WORKER_UPLIFT_STAGE_SCHEMAS if stage not in role_by_stage]
    if missing_stages:
        raise SystemExit(f"Missing worker-uplift stage roles: {','.join(missing_stages)}")

    return [(stage, role_by_stage[stage], schema) for stage, schema in WORKER_UPLIFT_STAGE_SCHEMAS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default="rami")
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--known-hosts", default="")
    parser.add_argument("--primary-shadow-database", default="nutsnews_primary_shadow")
    parser.add_argument(
        "--worker-uplift-stage-role",
        action="append",
        default=[],
        metavar="STAGE:ROLE",
        help="Configured worker-uplift stage login role. Repeat once for every stage.",
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    checks: list[dict] = []
    blockers: list[str] = []

    if not IDENTIFIER_RE.match(args.primary_shadow_database):
        raise SystemExit("--primary-shadow-database must be a safe PostgreSQL identifier")
    configured_worker_stage_roles = parse_worker_uplift_stage_roles(args.worker_uplift_stage_role)

    try:
        resolved = socket.gethostbyname(args.host)
    except OSError:
        resolved = ""
    checks.append({"name": "dns_resolution", "status": "pass" if resolved else "fail", "address": resolved or None})
    if not resolved:
        blockers.append("dns_resolution_failed")

    public_5432_reachable = tcp_reachable(args.host, 5432)
    checks.append(
        {
            "name": "public_5432_closed",
            "status": "fail" if public_5432_reachable else "pass",
            "public_5432_reachable": public_5432_reachable,
        }
    )
    if public_5432_reachable:
        blockers.append("public_5432_reachable")

    if args.offline:
        checks.append({"name": "ssh_loopback_postgres", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "migration_roles", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "primary_shadow_database", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "primary_shadow_connect_grants", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_uplift_stage_roles", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_uplift_schemas", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_uplift_own_schema_grants", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_uplift_public_write_denied", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_uplift_persistence_final_grant", "status": "skipped_with_reason", "reason": "offline mode"})
        checks.append({"name": "worker_api_final_shadow_grant", "status": "skipped_with_reason", "reason": "offline mode"})
    else:
        for path_arg, label in ((args.ssh_key, "ssh_key"), (args.known_hosts, "known_hosts")):
            if not path_arg or not Path(path_arg).exists():
                blockers.append(f"{label}_missing")
                checks.append({"name": label, "status": "fail"})

        if not any(blocker.endswith("_missing") for blocker in blockers):
            role_csv = ",".join(ROLE_NAMES)
            connect_role_csv = ",".join(PRIMARY_SHADOW_CONNECT_ROLES)
            non_worker_role_csv = ",".join(sorted(set(ROLE_NAMES + PRIMARY_SHADOW_CONNECT_ROLES + ["postgres"])))
            worker_schema_csv = ",".join(WORKER_UPLIFT_SCHEMAS)
            worker_stage_schema_values = ",".join(
                f"('{stage}','{schema}')" for stage, schema in WORKER_UPLIFT_STAGE_SCHEMAS
            )
            if configured_worker_stage_roles:
                configured_stage_role_values = ",".join(
                    f"('{stage}','{role_name}','{schema}')"
                    for stage, role_name, schema in configured_worker_stage_roles
                )
                worker_stage_role_cte = (
                    f"with expected(stage, role_name, schema_name) as (values {configured_stage_role_values}), "
                    "stage_roles as ("
                    "select e.stage, e.schema_name, e.role_name as rolname "
                    "from expected e "
                    "join pg_roles r on r.rolname = e.role_name "
                    "where r.rolcanlogin and not r.rolsuper"
                    ") "
                )
            else:
                worker_stage_role_cte = (
                    f"with expected(stage, schema_name) as (values {worker_stage_schema_values}), "
                    f"non_worker_roles(role_name) as (select unnest(string_to_array('{non_worker_role_csv}', ','))), "
                    "schema_usage as ("
                    "select n.nspname as schema_name, acl.grantee as role_oid "
                    "from pg_namespace n "
                    "cross join lateral aclexplode(coalesce(n.nspacl, '{}'::aclitem[])) acl "
                    "where upper(acl.privilege_type) = 'USAGE' and acl.grantee <> n.nspowner"
                    "), "
                    "table_insert as ("
                    "select n.nspname as schema_name, c.relname as table_name, acl.grantee as role_oid "
                    "from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "cross join lateral aclexplode(coalesce(c.relacl, '{}'::aclitem[])) acl "
                    "where c.relkind in ('r','p') "
                    "and upper(acl.privilege_type) = 'INSERT' "
                    "and acl.grantee <> c.relowner"
                    "), "
                    "stage_roles as ("
                    "select distinct e.stage, e.schema_name, r.rolname "
                    "from expected e "
                    "join schema_usage su on su.schema_name = e.schema_name "
                    "join table_insert inbox on inbox.schema_name = e.schema_name "
                    "and inbox.table_name = 'inbox' "
                    "and inbox.role_oid = su.role_oid "
                    "join table_insert outbox on outbox.schema_name = e.schema_name "
                    "and outbox.table_name = 'outbox' "
                    "and outbox.role_oid = su.role_oid "
                    "join pg_roles r on r.oid = su.role_oid "
                    "where r.rolcanlogin "
                    "and not r.rolsuper "
                    "and not exists (select 1 from non_worker_roles excluded where excluded.role_name = r.rolname)"
                    ") "
                )
            command = (
                "set -eu; "
                "ss -H -ltn sport = :5432 | awk '{print \"listener=\" $4}'; "
                "pg_isready -h 127.0.0.1 -p 5432 >/dev/null; "
                "sudo -n -u postgres psql -At postgres <<'SQL'\n"
                f"select 'role=' || rolname from pg_roles where rolname = any(string_to_array('{role_csv}', ',')) order by rolname;\n"
                f"select 'primary_shadow_database=' || d.datname || '|owner=' || pg_catalog.pg_get_userbyid(d.datdba) from pg_database d where d.datname = '{args.primary_shadow_database}';\n"
                f"select 'primary_shadow_connect=' || r.rolname || ':' || case when exists(select 1 from pg_database where datname = '{args.primary_shadow_database}') then has_database_privilege(r.rolname, '{args.primary_shadow_database}', 'CONNECT')::text else 'f' end from pg_roles r where r.rolname = any(string_to_array('{connect_role_csv}', ',')) order by r.rolname;\n"
                f"\\connect {args.primary_shadow_database}\n"
                f"select 'worker_uplift_schema=' || n.nspname from pg_namespace n where n.nspname = any(string_to_array('{worker_schema_csv}', ',')) order by n.nspname;\n"
                f"{worker_stage_role_cte}select 'worker_uplift_stage_role=' || stage || ':' || schema_name || ':' || rolname from stage_roles order by stage, rolname;\n"
                f"{worker_stage_role_cte}select 'worker_uplift_stage_connect=' || stage || ':' || rolname || ':' || has_database_privilege(rolname, '{args.primary_shadow_database}', 'CONNECT')::text from stage_roles order by stage, rolname;\n"
                f"{worker_stage_role_cte}select 'worker_uplift_own_grant=' || stage || ':' || rolname || ':' || schema_name || ':usage=' || has_schema_privilege(rolname, schema_name, 'USAGE')::text || ':inbox_insert=' || case when to_regclass(schema_name || '.inbox') is null then 'missing_table' else has_table_privilege(rolname, schema_name || '.inbox', 'INSERT')::text end || ':outbox_insert=' || case when to_regclass(schema_name || '.outbox') is null then 'missing_table' else has_table_privilege(rolname, schema_name || '.outbox', 'INSERT')::text end from stage_roles order by stage, rolname;\n"
                f"{worker_stage_role_cte}select 'worker_uplift_public_write=' || rolname || ':insert=' || case when to_regclass('public.articles') is null then 'missing_table' else has_table_privilege(rolname, 'public.articles', 'INSERT')::text end || ':update=' || case when to_regclass('public.articles') is null then 'missing_table' else has_table_privilege(rolname, 'public.articles', 'UPDATE')::text end || ':delete=' || case when to_regclass('public.articles') is null then 'missing_table' else has_table_privilege(rolname, 'public.articles', 'DELETE')::text end from stage_roles order by rolname;\n"
                f"{worker_stage_role_cte}select 'worker_uplift_final_grant=' || stage || ':' || rolname || ':insert=' || case when to_regclass('worker_uplift_final.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(rolname, 'worker_uplift_final.article_shadow_aggregates', 'INSERT')::text end || ':update=' || case when to_regclass('worker_uplift_final.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(rolname, 'worker_uplift_final.article_shadow_aggregates', 'UPDATE')::text end from stage_roles order by stage, rolname;\n"
                f"select 'worker_api_final_grant=' || r.rolname || ':aggregate_select=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates', 'SELECT')::text end || ':aggregate_insert=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates', 'INSERT')::text end || ':aggregate_update=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates', 'UPDATE')::text end || ':receipt_select=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts', 'SELECT')::text end || ':receipt_insert=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts', 'INSERT')::text end || ':receipt_update=' || case when to_regclass('{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.api_command_receipts', 'UPDATE')::text end || ':sequence_usage=' || has_sequence_privilege(r.rolname, '{WORKER_UPLIFT_FINAL_SCHEMA}.article_shadow_aggregates_id_seq', 'USAGE')::text from pg_roles r where r.rolname = '{WORKER_API_ROLE}';\n"
                "SQL"
            )
            code, stdout, _stderr = run_ssh(args.host, args.user, args.ssh_key, args.known_hosts, command)
            if code != 0:
                blockers.append("ssh_loopback_postgres_failed")
                checks.append({"name": "ssh_loopback_postgres", "status": "fail"})
            else:
                listeners = sorted(
                    line.split("=", 1)[1].strip()
                    for line in stdout.splitlines()
                    if line.startswith("listener=")
                )
                non_loopback_listeners = [
                    listener
                    for listener in listeners
                    if not (
                        listener.startswith("127.0.0.1:")
                        or listener.startswith("[::1]:")
                        or listener.startswith("localhost:")
                    )
                ]
                roles = sorted(line.split("=", 1)[1].strip() for line in stdout.splitlines() if line.startswith("role="))
                missing_roles = sorted(set(ROLE_NAMES) - set(roles))
                shadow_database = ""
                shadow_owner = ""
                for line in stdout.splitlines():
                    if line.startswith("primary_shadow_database="):
                        payload = line.split("=", 1)[1]
                        parts = dict(part.split("=", 1) for part in payload.split("|") if "=" in part)
                        shadow_database = parts.get("primary_shadow_database", payload.split("|", 1)[0])
                        shadow_owner = parts.get("owner", "")
                connect_grants: dict[str, bool] = {}
                for line in stdout.splitlines():
                    if line.startswith("primary_shadow_connect="):
                        role_name, allowed = line.split("=", 1)[1].split(":", 1)
                        connect_grants[role_name] = parse_postgres_bool(allowed)
                missing_connect_grants = sorted(role for role in PRIMARY_SHADOW_CONNECT_ROLES if not connect_grants.get(role))
                worker_schemas = sorted(
                    line.split("=", 1)[1].strip()
                    for line in stdout.splitlines()
                    if line.startswith("worker_uplift_schema=")
                )
                missing_worker_schemas = sorted(set(WORKER_UPLIFT_SCHEMAS) - set(worker_schemas))
                stage_roles: dict[str, list[tuple[str, str]]] = {stage: [] for stage, _schema in WORKER_UPLIFT_STAGE_SCHEMAS}
                for line in stdout.splitlines():
                    if not line.startswith("worker_uplift_stage_role="):
                        continue
                    stage, schema_name, role_name = line.split("=", 1)[1].split(":", 2)
                    stage_roles.setdefault(stage, []).append((schema_name, role_name))
                stage_role_failures = [
                    f"{stage}:role_count={len(role_rows)}"
                    for stage, role_rows in sorted(stage_roles.items())
                    if len(role_rows) != 1
                ]
                discovered_stage_roles = sorted({role_name for role_rows in stage_roles.values() for _schema, role_name in role_rows})
                stage_connect_failures: list[str] = []
                for line in stdout.splitlines():
                    if not line.startswith("worker_uplift_stage_connect="):
                        continue
                    stage, role_name, allowed = line.split("=", 1)[1].split(":", 2)
                    if not parse_postgres_bool(allowed):
                        stage_connect_failures.append(f"{stage}:{role_name}")
                own_grant_failures: list[str] = []
                for line in stdout.splitlines():
                    if not line.startswith("worker_uplift_own_grant="):
                        continue
                    parts = line.split("=", 1)[1].split(":")
                    if len(parts) != 6:
                        own_grant_failures.append(line.split("=", 1)[1])
                        continue
                    stage, role_name, schema_name, usage_value, inbox_insert_value, outbox_insert_value = parts
                    usage = usage_value.split("=", 1)[1]
                    inbox_insert = inbox_insert_value.split("=", 1)[1]
                    outbox_insert = outbox_insert_value.split("=", 1)[1]
                    if not all(parse_postgres_bool(value) for value in (usage, inbox_insert, outbox_insert)):
                        own_grant_failures.append(f"{stage}:{role_name}:{schema_name}")
                public_write_grants: list[str] = []
                public_write_missing_table = False
                for line in stdout.splitlines():
                    if not line.startswith("worker_uplift_public_write="):
                        continue
                    payload = line.split("=", 1)[1]
                    role_name, insert_value, update_value, delete_value = payload.split(":")
                    values = [
                        insert_value.split("=", 1)[1],
                        update_value.split("=", 1)[1],
                        delete_value.split("=", 1)[1],
                    ]
                    if any(value == "missing_table" for value in values):
                        public_write_missing_table = True
                    if any(parse_postgres_bool(value) for value in values):
                        public_write_grants.append(role_name)
                final_grant_failures: list[str] = []
                for line in stdout.splitlines():
                    if not line.startswith("worker_uplift_final_grant="):
                        continue
                    payload = line.split("=", 1)[1]
                    stage, role_name, insert_value, update_value = payload.split(":")
                    insert_allowed = insert_value.split("=", 1)[1]
                    update_allowed = update_value.split("=", 1)[1]
                    allowed = parse_postgres_bool(insert_allowed) and parse_postgres_bool(update_allowed)
                    if stage == "persistence":
                        if not allowed:
                            final_grant_failures.append(f"{stage}:{role_name}:missing_persistence_dml")
                    elif allowed:
                        final_grant_failures.append(f"{stage}:{role_name}:unexpected_final_dml")
                worker_api_final_grant_failures: list[str] = []
                for line in stdout.splitlines():
                    if not line.startswith("worker_api_final_grant="):
                        continue
                    payload = line.split("=", 1)[1]
                    parts = payload.split(":")
                    values = [part.split("=", 1)[1] for part in parts[1:]]
                    if not values or any(value == "missing_table" or not parse_postgres_bool(value) for value in values):
                        worker_api_final_grant_failures.append(parts[0])
                checks.append(
                    {
                        "name": "ssh_loopback_postgres",
                        "status": "pass" if listeners and not non_loopback_listeners else "fail",
                        "listener_count": len(listeners),
                        "non_loopback_listener_count": len(non_loopback_listeners),
                    }
                )
                checks.append(
                    {
                        "name": "migration_roles",
                        "status": "pass" if not missing_roles else "fail",
                        "present_count": len(roles),
                        "missing_roles": missing_roles,
                    }
                )
                checks.append(
                    {
                        "name": "primary_shadow_database",
                        "status": "pass"
                        if shadow_database == args.primary_shadow_database and shadow_owner == "nutsnews_migration_restore"
                        else "fail",
                        "database": args.primary_shadow_database,
                        "owner_role": shadow_owner or None,
                    }
                )
                checks.append(
                    {
                        "name": "primary_shadow_connect_grants",
                        "status": "pass" if not missing_connect_grants else "fail",
                        "database": args.primary_shadow_database,
                        "checked_role_count": len(PRIMARY_SHADOW_CONNECT_ROLES),
                        "missing_roles": missing_connect_grants,
                    }
                )
                checks.append(
                    {
                        "name": "worker_uplift_stage_roles",
                        "status": "pass" if not stage_role_failures and not stage_connect_failures else "fail",
                        "present_count": len(discovered_stage_roles),
                        "discovered_role_count": len(discovered_stage_roles),
                        "stage_role_failures": stage_role_failures,
                        "connect_failures": stage_connect_failures,
                    }
                )
                checks.append(
                    {
                        "name": "worker_uplift_schemas",
                        "status": "pass" if not missing_worker_schemas else "fail",
                        "present_count": len(worker_schemas),
                        "missing_schemas": missing_worker_schemas,
                    }
                )
                checks.append(
                    {
                        "name": "worker_uplift_own_schema_grants",
                        "status": "pass" if not own_grant_failures else "fail",
                        "failure_count": len(own_grant_failures),
                        "failures": own_grant_failures,
                    }
                )
                checks.append(
                    {
                        "name": "worker_uplift_public_write_denied",
                        "status": "pass" if not public_write_grants and not public_write_missing_table else "fail",
                        "unexpected_write_roles": sorted(public_write_grants),
                        "public_articles_present": not public_write_missing_table,
                    }
                )
                checks.append(
                    {
                        "name": "worker_uplift_persistence_final_grant",
                        "status": "pass" if not final_grant_failures else "fail",
                        "failures": final_grant_failures,
                    }
                )
                checks.append(
                    {
                        "name": "worker_api_final_shadow_grant",
                        "status": "pass" if not worker_api_final_grant_failures else "fail",
                        "failures": worker_api_final_grant_failures,
                    }
                )
                if not listeners:
                    blockers.append("postgres_listener_missing")
                if non_loopback_listeners:
                    blockers.append("postgres_non_loopback_listener")
                if missing_roles:
                    blockers.append("migration_roles_missing")
                if shadow_database != args.primary_shadow_database:
                    blockers.append("primary_shadow_database_missing")
                if shadow_owner != "nutsnews_migration_restore":
                    blockers.append("primary_shadow_owner_mismatch")
                if missing_connect_grants:
                    blockers.append("primary_shadow_connect_grants_missing")
                if stage_role_failures or stage_connect_failures:
                    blockers.append("worker_uplift_stage_roles_missing")
                if missing_worker_schemas:
                    blockers.append("worker_uplift_schemas_missing")
                if own_grant_failures:
                    blockers.append("worker_uplift_own_schema_grants_invalid")
                if public_write_grants or public_write_missing_table:
                    blockers.append("worker_uplift_public_write_denial_invalid")
                if final_grant_failures:
                    blockers.append("worker_uplift_persistence_final_grant_invalid")
                if worker_api_final_grant_failures:
                    blockers.append("worker_api_final_shadow_grant_invalid")

    status = "pass" if not blockers else "fail"
    report = {
        "status": status,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "host": args.host,
        "checks": checks,
        "blockers": blockers,
        "safe_metadata_only": True,
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
