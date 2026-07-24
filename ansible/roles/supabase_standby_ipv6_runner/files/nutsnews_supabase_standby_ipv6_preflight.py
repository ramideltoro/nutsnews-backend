#!/usr/bin/env python3
"""Run redacted IPv6 readiness probes for the one-job standby runner VM."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse


def run_probe(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def parse_db_host(value: str) -> tuple[str, int]:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("standby DB URL must use a postgres scheme")
    if not parsed.hostname:
        raise ValueError("standby DB URL must include a host")
    if "sslmode=require" not in parsed.query:
        raise ValueError("standby DB URL must require TLS")
    return parsed.hostname, parsed.port or 5432


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Validate redaction shape without network probes.")
    args = parser.parse_args()

    report: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "safe_output": True,
        "hostnames_redacted": True,
        "credentials_redacted": True,
    }

    if args.offline:
        report.update(
            {
                "offline": True,
                "ipv6_https": "not-run",
                "dns_aaaa": "not-run",
                "tcp_5432": "not-run",
            }
        )
        print(json.dumps(report, sort_keys=True))
        return 0

    db_url = os.environ.get("NUTSNEWS_STANDBY_SUPABASE_DB_URL", "")
    if not db_url:
        print("NUTSNEWS_STANDBY_SUPABASE_DB_URL is required for live probes.", file=sys.stderr)
        return 2

    try:
        host, port = parse_db_host(db_url)
    except ValueError as exc:
        print(f"Invalid standby DB URL shape: {exc}", file=sys.stderr)
        return 2

    curl = run_probe(["curl", "-6", "--fail", "--silent", "--show-error", "--max-time", "15", "https://ifconfig.co/ip"])
    ipv6_text = curl.stdout.strip()
    report["ipv6_https"] = curl.returncode == 0 and ":" in ipv6_text

    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror:
        addresses = []
    report["dns_aaaa"] = len(addresses) > 0

    nc = run_probe(["nc", "-6", "-vz", "-w", "15", host, str(port)])
    report["tcp_5432"] = nc.returncode == 0

    if not (report["ipv6_https"] and report["dns_aaaa"] and report["tcp_5432"]):
        print(json.dumps(report, sort_keys=True))
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
