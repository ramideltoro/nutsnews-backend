#!/usr/bin/env python3
"""Validate the backend service baseline inventory."""

from __future__ import annotations

import json
import sys
from pathlib import Path


BASELINE_PATH = Path("docs/backend-service-baseline.json")
ALLOWED_PUBLIC_TCP_PORTS = {22, 80, 443}
REQUIRED_KEYS = {
    "observed_at_utc",
    "host",
    "ipv4",
    "public_tcp_ports",
    "private_listeners",
    "not_deployed",
    "failed_systemd_units",
    "represented_in_repo",
    "requires_protected_apply",
}


def main() -> int:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - set(data)
    if missing:
        print(f"Missing required baseline keys: {sorted(missing)}", file=sys.stderr)
        return 1

    public_ports = data["public_tcp_ports"]
    if not isinstance(public_ports, list) or not public_ports:
        print("public_tcp_ports must be a non-empty list.", file=sys.stderr)
        return 1

    unexpected = []
    for entry in public_ports:
        port = entry.get("port")
        if port not in ALLOWED_PUBLIC_TCP_PORTS:
            unexpected.append(entry)

    if unexpected:
        print(f"Unexpected public TCP ports: {unexpected}", file=sys.stderr)
        return 1

    if data["failed_systemd_units"] != 0:
        print("failed_systemd_units must be 0 for the attested baseline.", file=sys.stderr)
        return 1

    print("Backend service baseline inventory is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
