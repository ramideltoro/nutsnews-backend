#!/usr/bin/env python3
"""Run a fixed-purpose, read-only backend drift check."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"

ALLOWED_NUTSNEWS_NON_APP_UNITS = {
    "nutsnews-backup.service",
    "nutsnews-backup.timer",
    "nutsnews-backup-verify.service",
    "nutsnews-backup-verify.timer",
    "nutsnews-metrics-textfile.service",
    "nutsnews-metrics-textfile.timer",
    "nutsnews-ops-dashboard-collect.service",
    "nutsnews-ops-dashboard-collect.timer",
    "nutsnews-rabbitmq.service",
    "nutsnews-restore-drill.service",
    "nutsnews-restore-drill.timer",
}

TOKEN_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|"
    r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)


REMOTE_COMMANDS: dict[str, str] = {
    "hostname": "hostname",
    "kernel": "uname -r",
    "failed_units": "systemctl --failed --no-legend --no-pager || true",
    "listeners": "ss -H -tuln || true",
    "sudo_nopasswd": "sudo -n true >/dev/null 2>&1 && echo yes || echo no",
    "sshd_effective": "/usr/sbin/sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) ' || true",
    "docker_active": "systemctl is-active docker 2>/dev/null || true",
    "docker_present": "command -v docker >/dev/null 2>&1 && echo yes || echo no",
    "caddy_active": "systemctl is-active caddy 2>/dev/null || true",
    "caddy_present": "command -v caddy >/dev/null 2>&1 && echo yes || echo no",
    "backend_units": "systemctl list-units --type=service --type=timer --all --no-legend 'nutsnews*' 2>/dev/null || true",
    "postgres_active": "systemctl is-active postgresql 2>/dev/null || true",
    "redis_active": "systemctl is-active redis-server 2>/dev/null || systemctl is-active valkey 2>/dev/null || true",
    "swap": "swapon --show=NAME --noheadings 2>/dev/null || true",
    "reboot_required": "test -e /var/run/reboot-required && echo yes || echo no",
    "ufw_status": "ufw status verbose 2>&1 || true",
    "managed_files": "for path in /etc/ssh/sshd_config.d/00-nutsnews-hardening.conf /etc/fail2ban/jail.d/nutsnews-sshd.local /etc/sysctl.d/99-nutsnews-backend-swap.conf /etc/systemd/journald.conf.d/99-nutsnews-backend.conf /etc/logrotate.d/nutsnews-backend /usr/local/sbin/nutsnews-backend-smoke /usr/local/sbin/nutsnews-backup /usr/local/bin/nutsnews-metrics-textfile /etc/nutsnews-backup/service-matrix.json /etc/nutsnews-backup/restic.env /etc/alloy/config.alloy /etc/alloy/nutsnews-prometheus.env /etc/systemd/system/alloy.service.d/10-nutsnews-prometheus.conf /etc/systemd/system/nutsnews-backup.service /etc/systemd/system/nutsnews-backup.timer /etc/systemd/system/nutsnews-backup-verify.service /etc/systemd/system/nutsnews-backup-verify.timer /etc/systemd/system/nutsnews-restore-drill.service /etc/systemd/system/nutsnews-restore-drill.timer /etc/systemd/system/nutsnews-metrics-textfile.service /etc/systemd/system/nutsnews-metrics-textfile.timer /etc/systemd/system/nutsnews-rabbitmq.service /opt/nutsnews-rabbitmq/compose.yml /etc/nutsnews-rabbitmq/rabbitmq.conf /etc/nutsnews-rabbitmq/enabled_plugins /etc/nutsnews-rabbitmq/worker-uplift-topology.json /etc/nutsnews-rabbitmq/rabbitmq.env /etc/nutsnews-rabbitmq/topology.env /usr/local/sbin/nutsnews-rabbitmq-probe /usr/local/sbin/nutsnews-rabbitmq-topology /usr/local/sbin/nutsnews-rabbitmq-network-check /usr/local/sbin/nutsnews-rabbitmq-recovery /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json /usr/local/sbin/nutsnews-worker-runtime /etc/nutsnews-worker-uplift/services.json /opt/nutsnews-worker-uplift/compose.yml; do stat -c '%a %U %G %n' \"$path\" 2>/dev/null || { sudo -n test -e \"$path\" 2>/dev/null && echo \"present_root_only $path\" || echo \"missing $path\"; }; done",
    "rabbitmq_drift": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        "&& test -x /usr/local/sbin/nutsnews-rabbitmq-probe; then "
        "sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe drift "
        "--env /etc/nutsnews-rabbitmq/rabbitmq.env "
        "--credentials-env /etc/nutsnews-rabbitmq/topology.env "
        "--definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json "
        "--metadata /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json; "
        "else echo not_configured; fi"
    ),
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    value = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    value = TOKEN_RE.sub("<redacted-token>", value)
    value = URL_SECRET_RE.sub(r"\1<redacted>\3", value)
    return value


def run_ssh_command(host: str, user: str, key: Path, known_hosts: Path, command: str, timeout: int) -> dict[str, Any]:
    ssh_command = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"ConnectTimeout={timeout}",
        f"{user}@{host}",
        command,
    ]
    completed = subprocess.run(
        ssh_command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout + 10,
    )
    return {
        "rc": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def collect_live(host: str, user: str, key: Path, known_hosts: Path, timeout: int) -> dict[str, Any]:
    commands = {}
    for name, command in REMOTE_COMMANDS.items():
        commands[name] = run_ssh_command(host, user, key, known_hosts, command, timeout)
    return {"commands": commands}


def load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def command_stdout(evidence: dict[str, Any], name: str) -> str:
    return evidence.get("commands", {}).get(name, {}).get("stdout", "")


def parse_public_tcp_ports(listeners: str) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    for line in listeners.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "tcp":
            continue
        local = parts[4]
        host, port = split_host_port(local)
        if port is None:
            continue
        if is_public_bind(host):
            ports.append({"address": host, "port": port})
    return sorted(ports, key=lambda item: (item["port"], item["address"]))


def split_host_port(local: str) -> tuple[str, int | None]:
    local = local.strip()
    if local.startswith("[") and "]:" in local:
        host, port_text = local.rsplit("]:", 1)
        return host[1:], parse_port(port_text)
    if ":" not in local:
        return local, None
    host, port_text = local.rsplit(":", 1)
    return host, parse_port(port_text)


def parse_port(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def is_public_bind(host: str) -> bool:
    private_prefixes = ("127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "192.168.")
    if host in {"::1", "localhost"}:
        return False
    if host.startswith(private_prefixes):
        return False
    return True


def classify(evidence: dict[str, Any], baseline: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_public_ports = sorted({int(item["port"]) for item in baseline["public_tcp_ports"]})
    observed_public = parse_public_tcp_ports(command_stdout(evidence, "listeners"))
    observed_public_ports = sorted({item["port"] for item in observed_public})
    unexpected_ports = [port for port in observed_public_ports if port not in expected_public_ports]
    missing_ports = [port for port in expected_public_ports if port not in observed_public_ports]

    if unexpected_ports:
        status = "unexpected"
    elif missing_ports:
        status = "missing"
    else:
        status = "expected"
    checks.append(
        {
            "surface": "public_tcp_ports",
            "status": status,
            "severity": "high" if status == "unexpected" else "info",
            "expected": expected_public_ports,
            "observed": observed_public,
            "details": {
                "unexpected_ports": unexpected_ports,
                "missing_ports": missing_ports,
            },
        }
    )

    failed_units_output = command_stdout(evidence, "failed_units").strip()
    failed_units = [line for line in failed_units_output.splitlines() if line.strip()]
    checks.append(
        {
            "surface": "failed_systemd_units",
            "status": "unexpected" if failed_units else "expected",
            "severity": "high" if failed_units else "info",
            "expected": 0,
            "observed": len(failed_units),
            "details": failed_units,
        }
    )

    host = command_stdout(evidence, "hostname").strip()
    checks.append(
        {
            "surface": "hostname",
            "status": "expected" if host == baseline["host"] else "unexpected",
            "severity": "medium" if host != baseline["host"] else "info",
            "expected": baseline["host"],
            "observed": host,
        }
    )

    sudo_nopasswd = command_stdout(evidence, "sudo_nopasswd").strip()
    checks.append(
        {
            "surface": "sudo_nopasswd",
            "status": "missing" if sudo_nopasswd == "no" else "expected",
            "severity": "medium" if sudo_nopasswd == "no" else "info",
            "expected": "yes for protected apply without become password",
            "observed": sudo_nopasswd or "unknown",
            "acceptable_until": "NUTSNEWS_BACKEND_BECOME_PASSWORD is provided or #10 provisions passwordless sudo",
        }
    )

    checks.extend(classify_not_deployed(evidence, baseline))
    checks.extend(classify_managed_files(evidence))
    checks.append(classify_rabbitmq_drift(evidence))

    summary = {
        "total": len(checks),
        "expected": sum(1 for item in checks if item["status"] == "expected"),
        "missing": sum(1 for item in checks if item["status"] == "missing"),
        "unexpected": sum(1 for item in checks if item["status"] == "unexpected"),
        "unknown": sum(1 for item in checks if item["status"] == "unknown"),
        "high_priority_unexpected": [
            item["surface"]
            for item in checks
            if item["status"] == "unexpected" and item.get("severity") == "high"
        ],
    }
    return checks, summary


def classify_not_deployed(evidence: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    not_deployed = set(baseline.get("not_deployed", []))
    service_map = {
        "Docker Engine": ("docker_present", "docker_active"),
        "Docker Compose": ("docker_present", "docker_active"),
        "Caddy": ("caddy_present", "caddy_active"),
        "PostgreSQL": (None, "postgres_active"),
        "RabbitMQ broker": (None, "rabbitmq_drift"),
        "Redis or Valkey": (None, "redis_active"),
    }
    for label, (present_command, active_command) in service_map.items():
        if label not in not_deployed:
            continue
        present = command_stdout(evidence, present_command).strip() if present_command else "unknown"
        active = command_stdout(evidence, active_command).strip()
        deployed = present == "yes" or active == "active"
        checks.append(
            {
                "surface": f"not_deployed:{label}",
                "status": "unexpected" if deployed else "expected",
                "severity": "medium" if deployed else "info",
                "expected": "not_deployed",
                "observed": {"present": present, "active": active or "inactive_or_missing"},
            }
        )

    backend_unit_lines = [
        line.strip()
        for line in command_stdout(evidence, "backend_units").splitlines()
        if line.strip()
    ]
    allowed_backend_unit_lines = [
        line
        for line in backend_unit_lines
        if any(unit in line for unit in ALLOWED_NUTSNEWS_NON_APP_UNITS)
    ]
    unexpected_backend_unit_lines = [
        line
        for line in backend_unit_lines
        if line not in allowed_backend_unit_lines
    ]
    if "backend app" in not_deployed:
        checks.append(
            {
                "surface": "not_deployed:backend app",
                "status": "unexpected" if unexpected_backend_unit_lines else "expected",
                "severity": "medium" if unexpected_backend_unit_lines else "info",
                "expected": "no nutsnews backend service",
                "observed": unexpected_backend_unit_lines,
                "allowed_observed": allowed_backend_unit_lines,
            }
        )
    return checks


def classify_rabbitmq_drift(evidence: dict[str, Any]) -> dict[str, Any]:
    output = command_stdout(evidence, "rabbitmq_drift").strip()
    if output == "not_configured":
        return {
            "surface": "rabbitmq_drift",
            "status": "missing",
            "severity": "low",
            "expected": "RabbitMQ drift check present after broker provisioning",
            "observed": "not_configured",
        }
    data = parse_json_object(output)
    if data is None:
        return {
            "surface": "rabbitmq_drift",
            "status": "unknown",
            "severity": "medium",
            "expected": "valid RabbitMQ drift JSON",
            "observed": output.splitlines()[-1] if output else "empty",
        }
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    blockers = summary.get("high_priority_unexpected") if isinstance(summary, dict) else []
    status = "expected" if data.get("status") == "pass" else "unexpected"
    return {
        "surface": "rabbitmq_drift",
        "status": status,
        "severity": "high" if status == "unexpected" else "info",
        "expected": "pass",
        "observed": data.get("status", "unknown"),
        "details": {
            "high_priority_unexpected": blockers if isinstance(blockers, list) else [],
            "total": summary.get("total") if isinstance(summary, dict) else None,
        },
    }


def parse_json_object(value: str) -> dict[str, Any] | None:
    value = value.strip()
    if not value.startswith("{"):
        return None
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def classify_managed_files(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    managed_files = command_stdout(evidence, "managed_files").splitlines()
    for line in managed_files:
        if not line.strip():
            continue
        if line.startswith("missing "):
            checks.append(
                {
                    "surface": f"managed_file:{line.removeprefix('missing ')}",
                    "status": "missing",
                    "severity": "low",
                    "expected": "present after protected apply",
                    "observed": "missing",
                    "acceptable_until": "#10 protected apply succeeds",
                }
            )
        elif line.startswith("present_root_only "):
            checks.append(
                {
                    "surface": f"managed_file:{line.removeprefix('present_root_only ')}",
                    "status": "expected",
                    "severity": "info",
                    "expected": "present",
                    "observed": "present_root_only",
                }
            )
        else:
            checks.append(
                {
                    "surface": f"managed_file:{line.split()[-1]}",
                    "status": "expected",
                    "severity": "info",
                    "expected": "present",
                    "observed": line,
                }
            )
    return checks


def write_summary(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Backend Drift Check",
        "",
        f"- Status: `{report['status']}`",
        f"- Checked at: `{report['checked_at_utc']}`",
        f"- Host: `{report['target']['user']}@{report['target']['host']}`",
        f"- High-priority unexpected surfaces: `{len(report['summary']['high_priority_unexpected'])}`",
        "",
        "| Surface | Status | Severity |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        lines.append(f"| `{check['surface']}` | `{check['status']}` | `{check.get('severity', 'info')}` |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_SSH_USER", "rami"))
    parser.add_argument("--ssh-key", type=Path, default=Path(os.environ.get("NUTSNEWS_BACKEND_SSH_KEY", "")))
    parser.add_argument("--known-hosts", type=Path, default=Path(os.environ.get("NUTSNEWS_BACKEND_KNOWN_HOSTS_FILE", "")))
    parser.add_argument("--fixture", type=Path, help="Use fixture evidence instead of live SSH.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, help="Write a Markdown summary.")
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline = load_baseline()
    if args.fixture:
        evidence = json.loads(args.fixture.read_text(encoding="utf-8"))
    else:
        if not args.ssh_key or not args.known_hosts:
            print("--ssh-key and --known-hosts are required for live checks", file=sys.stderr)
            return 2
        evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)

    checks, summary = classify(evidence, baseline)
    status = "fail" if summary["high_priority_unexpected"] else "pass"
    report = {
        "version": 1,
        "checked_at_utc": utc_now(),
        "target": {"host": args.ssh_host, "user": args.ssh_user},
        "status": status,
        "summary": summary,
        "checks": checks,
        "secret_redaction": "fixed commands only; stdout/stderr redacted before classification",
        "remediation": "none; this workflow is read-only",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, report)
    print(json.dumps({"status": status, "summary": summary}, indent=2))
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
