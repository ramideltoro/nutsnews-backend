#!/usr/bin/env python3
"""Run safe backend PostgreSQL migration access preflight checks."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_HOST = "65.75.201.18"
ROLE_NAMES = [
    "nutsnews_app",
    "nutsnews_readonly",
    "nutsnews_migration_restore",
    "nutsnews_migration_validation",
    "nutsnews_migration_replication",
    "nutsnews_app_rehearsal",
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
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    checks: list[dict] = []
    blockers: list[str] = []

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
    else:
        for path_arg, label in ((args.ssh_key, "ssh_key"), (args.known_hosts, "known_hosts")):
            if not path_arg or not Path(path_arg).exists():
                blockers.append(f"{label}_missing")
                checks.append({"name": label, "status": "fail"})

        if not any(blocker.endswith("_missing") for blocker in blockers):
            role_csv = ",".join(ROLE_NAMES)
            command = (
                "set -eu; "
                "ss -H -ltn sport = :5432 | awk '{print \"listener=\" $4}'; "
                "pg_isready -h 127.0.0.1 -p 5432 >/dev/null; "
                "sudo -n -u postgres psql -At postgres -c "
                "\"select rolname from pg_roles where rolname = any(string_to_array('"
                + role_csv
                + "', ',')) order by rolname\""
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
                roles = sorted(line.strip() for line in stdout.splitlines() if line.strip() and not line.startswith("listener="))
                missing_roles = sorted(set(ROLE_NAMES) - set(roles))
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
                if not listeners:
                    blockers.append("postgres_listener_missing")
                if non_loopback_listeners:
                    blockers.append("postgres_non_loopback_listener")
                if missing_roles:
                    blockers.append("migration_roles_missing")

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
