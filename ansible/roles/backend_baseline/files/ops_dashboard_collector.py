#!/usr/bin/env python3
"""Collect a sanitized read-only backend ops dashboard snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


SERVICES = ("ssh", "ufw", "fail2ban", "caddy", "docker", "postgresql", "alloy", "nutsnews-backup.timer")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    redacted = TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = URL_SECRET_RE.sub(r"\1<redacted>\3", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def run(command: list[str], timeout: int = 8) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - defensive for host edge cases
        return {"returncode": 255, "stdout": "", "stderr": redact(str(exc))}
    return {
        "returncode": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def parse_free(text: str) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"status": "unknown"}
    headers = [item.lower() for item in lines[0].split()]
    rows: dict[str, dict[str, int]] = {}
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        name = parts[0].rstrip(":").lower()
        values: dict[str, int] = {}
        for header, raw in zip(headers, parts[1:]):
            try:
                values[header] = int(raw)
            except ValueError:
                continue
        rows[name] = values
    mem = rows.get("mem", {})
    swap = rows.get("swap", {})
    return {
        "memory": percent_summary(mem),
        "swap": percent_summary(swap),
    }


def percent_summary(values: dict[str, int]) -> dict[str, Any]:
    total = values.get("total", 0)
    used = values.get("used", 0)
    available = values.get("available")
    percent = round((used / total) * 100, 1) if total else None
    return {"total": total, "used": used, "available": available, "used_percent": percent}


def parse_df(text: str) -> dict[str, Any]:
    line = next((item for item in text.splitlines() if item.strip()), "")
    parts = line.split()
    if len(parts) < 6:
        return {"status": "unknown", "raw": redact(line)}
    try:
        used_percent = int(parts[4].rstrip("%"))
    except ValueError:
        used_percent = None
    return {
        "filesystem": parts[0],
        "size": int(parts[1]) if parts[1].isdigit() else None,
        "used": int(parts[2]) if parts[2].isdigit() else None,
        "available": int(parts[3]) if parts[3].isdigit() else None,
        "used_percent": used_percent,
        "mount": parts[5],
    }


def threshold(value: float | int | None, warn: int = 80, crit: int = 90) -> str:
    if value is None:
        return "unknown"
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "healthy"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_configured"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
    return data if isinstance(data, dict) else {"status": "unknown"}


def service_state(service: str) -> str:
    result = run(["systemctl", "is-active", service], timeout=5)
    state = result["stdout"].strip()
    return state or "unavailable"


def classify_service(service: str, state: str) -> str:
    if state == "active":
        return "healthy"
    if service in {"docker", "postgresql", "alloy", "nutsnews-backup.timer"} and state in {"inactive", "unavailable", "failed"}:
        return "not_configured"
    if service == "fail2ban" and state in {"inactive", "unavailable"}:
        return "warning"
    if state in {"inactive", "unavailable"}:
        return "warning"
    return "critical"


def public_listeners(text: str) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "tcp" or parts[1] != "LISTEN":
            continue
        address = parts[4]
        if ":" not in address:
            continue
        host, raw_port = address.rsplit(":", 1)
        if host.startswith("127.") or host in {"::1", "localhost"} or "%lo" in host:
            continue
        if not raw_port.isdigit():
            continue
        listeners.append({"address": host, "port": int(raw_port)})
    return listeners


def collect() -> dict[str, Any]:
    free_data = parse_free(run(["free", "-b"])["stdout"])
    root_disk_raw = run(["df", "-PB1", "/"])["stdout"]
    root_inode_raw = run(["df", "-Pi", "/"])["stdout"]
    root_disk = parse_df(root_disk_raw.splitlines()[-1] if root_disk_raw else "")
    root_inodes = parse_df(root_inode_raw.splitlines()[-1] if root_inode_raw else "")
    loadavg = run(["cat", "/proc/loadavg"])["stdout"].strip().split()
    cpu_count_raw = run(["nproc"])["stdout"].strip()
    try:
        cpu_count = int(cpu_count_raw)
    except ValueError:
        cpu_count = None

    services = [
        {"name": service, "state": state, "status": classify_service(service, state)}
        for service in SERVICES
        for state in [service_state(service)]
    ]

    failed_units = run(["systemctl", "--failed", "--no-legend", "--no-pager"])["stdout"].strip()
    endpoint = run(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--resolve",
            "backend.nutsnews.com:443:127.0.0.1",
            "https://backend.nutsnews.com/healthz",
        ],
        timeout=8,
    )
    endpoint_body = endpoint["stdout"].strip()

    timers = run(
        [
            "systemctl",
            "list-timers",
            "--all",
            "--no-legend",
            "--no-pager",
            "apt*",
            "logrotate*",
            "fstrim*",
            "dpkg-db-backup*",
            "nutsnews*",
        ]
    )["stdout"]
    listeners = public_listeners(run(["ss", "-H", "-tuln"])["stdout"])
    upgradable_raw = run(["sh", "-lc", "apt list --upgradable 2>/dev/null | tail -n +2 | wc -l"])["stdout"].strip()
    try:
        upgradable_count = int(upgradable_raw)
    except ValueError:
        upgradable_count = None
    latest_kernel = run(
        [
            "sh",
            "-lc",
            "find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\\n' 2>/dev/null | sed 's/^vmlinuz-//' | sort -V | tail -n 1",
        ]
    )["stdout"].strip()
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = "unknown"

    memory = free_data.get("memory", {})
    swap = free_data.get("swap", {})
    root_disk["status"] = threshold(root_disk.get("used_percent"))
    root_inodes["status"] = threshold(root_inodes.get("used_percent"))
    memory["status"] = threshold(memory.get("used_percent"))
    swap["status"] = "not_configured" if not swap.get("total") else threshold(swap.get("used_percent"), warn=60, crit=85)
    backup_dir = Path("/var/lib/nutsnews/backups")
    backup = {
        "backup": read_json(backup_dir / "last-backup.json"),
        "verification": read_json(backup_dir / "last-verification.json"),
        "restore_drill": read_json(backup_dir / "last-restore-verification.json"),
    }

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "access_boundary": "loopback_only_ssh_tunnel",
        "host": {
            "hostname": socket.gethostname(),
            "kernel": run(["uname", "-r"])["stdout"].strip(),
            "latest_installed_kernel": latest_kernel or "unknown",
            "boot_id": boot_id,
            "os": run(["sh", "-lc", ". /etc/os-release && printf '%s %s' \"$NAME\" \"$VERSION_ID\""])["stdout"].strip(),
            "uptime": run(["uptime", "-p"])["stdout"].strip(),
            "boot_time": run(["uptime", "-s"])["stdout"].strip(),
            "cpu_count": cpu_count,
            "load_average": loadavg[:3],
            "reboot_required": Path("/var/run/reboot-required").exists(),
            "upgradable_packages": upgradable_count,
        },
        "resources": {
            "memory": memory,
            "swap": swap,
            "root_disk": root_disk,
            "root_inodes": root_inodes,
        },
        "endpoint": {
            "name": "backend_health",
            "url": "https://backend.nutsnews.com/healthz",
            "expected": "ok",
            "status": "healthy" if endpoint_body == "ok" else "critical",
            "response": endpoint_body or "empty",
        },
        "services": services,
        "systemd": {
            "failed_units_status": "healthy" if not failed_units else "critical",
            "failed_units": failed_units.splitlines()[:10],
            "timers": [line for line in timers.splitlines() if line.strip()][:20],
        },
        "backup": backup,
        "network": {
            "public_tcp_listeners": listeners,
            "expected_public_tcp_ports": [22, 80, 443],
        },
    }


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    write_atomic(Path(args.output), collect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
