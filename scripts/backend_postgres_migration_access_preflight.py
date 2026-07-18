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
]


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default="rami")
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--known-hosts", default="")
    parser.add_argument("--primary-shadow-database", default="nutsnews_primary_shadow")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    checks: list[dict] = []
    blockers: list[str] = []

    if not IDENTIFIER_RE.match(args.primary_shadow_database):
        raise SystemExit("--primary-shadow-database must be a safe PostgreSQL identifier")

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
    else:
        for path_arg, label in ((args.ssh_key, "ssh_key"), (args.known_hosts, "known_hosts")):
            if not path_arg or not Path(path_arg).exists():
                blockers.append(f"{label}_missing")
                checks.append({"name": label, "status": "fail"})

        if not any(blocker.endswith("_missing") for blocker in blockers):
            role_csv = ",".join(ROLE_NAMES)
            connect_role_csv = ",".join(PRIMARY_SHADOW_CONNECT_ROLES)
            command = (
                "set -eu; "
                "ss -H -ltn sport = :5432 | awk '{print \"listener=\" $4}'; "
                "pg_isready -h 127.0.0.1 -p 5432 >/dev/null; "
                "sudo -n -u postgres psql -At postgres <<'SQL'\n"
                f"select 'role=' || rolname from pg_roles where rolname = any(string_to_array('{role_csv}', ',')) order by rolname;\n"
                f"select 'primary_shadow_database=' || d.datname || '|owner=' || pg_catalog.pg_get_userbyid(d.datdba) from pg_database d where d.datname = '{args.primary_shadow_database}';\n"
                f"select 'primary_shadow_connect=' || r.rolname || ':' || case when exists(select 1 from pg_database where datname = '{args.primary_shadow_database}') then has_database_privilege(r.rolname, '{args.primary_shadow_database}', 'CONNECT')::text else 'f' end from pg_roles r where r.rolname = any(string_to_array('{connect_role_csv}', ',')) order by r.rolname;\n"
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
                        connect_grants[role_name] = allowed == "t"
                missing_connect_grants = sorted(role for role in PRIMARY_SHADOW_CONNECT_ROLES if not connect_grants.get(role))
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
