#!/usr/bin/env python3
"""Generate a fixed-purpose backend health report.

The reporter intentionally runs a closed set of read-only SSH commands. It does
not accept remote commands from workflow inputs or issue text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
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


REMOTE_COMMANDS: dict[str, str] = {
    "hostname": "hostname",
    "kernel": "uname -r",
    "os_release": ". /etc/os-release 2>/dev/null && printf '%s %s\\n' \"${NAME:-unknown}\" \"${VERSION_ID:-unknown}\" || true",
    "uptime_pretty": "uptime -p || true",
    "uptime_since": "uptime -s || true",
    "loadavg": "cat /proc/loadavg",
    "cpu_count": "nproc",
    "memory": "free -b",
    "root_disk": "df -PB1 / | tail -n +2",
    "root_inodes": "df -Pi / | tail -n +2",
    "swap": "swapon --show --bytes --noheadings || true",
    "reboot_required": "test -e /var/run/reboot-required && echo yes || echo no",
    "upgradable_count": "apt list --upgradable 2>/dev/null | tail -n +2 | wc -l",
    "failed_units": "systemctl --failed --no-legend --no-pager || true",
    "service_states": (
        "for unit in ssh ufw fail2ban docker caddy postgresql alloy sysstat; do "
        "state=$(systemctl is-active \"$unit\" 2>/dev/null || true); "
        "if [ -z \"$state\" ]; then state=unavailable; fi; "
        "printf '%s=%s\\n' \"$unit\" \"$state\"; "
        "done"
    ),
    "timers": "systemctl list-timers --all --no-legend --no-pager 'apt*' 'logrotate*' 'fstrim*' 'dpkg-db-backup*' 'nutsnews*' 2>/dev/null || true",
    "listeners": "ss -H -tuln || true",
    "backend_health": (
        "if command -v curl >/dev/null 2>&1 && systemctl is-active caddy >/dev/null 2>&1; then "
        "curl -fsS --connect-timeout 5 "
        "--resolve backend.nutsnews.com:443:127.0.0.1 "
        "https://backend.nutsnews.com/healthz 2>/dev/null || true; "
        "else echo unavailable; fi"
    ),
    "backend_units": "systemctl list-units --type=service --type=timer --all --no-legend 'nutsnews*' 2>/dev/null || true",
    "backup_tools": "for tool in restic rclone pg_dump docker caddy alloy; do command -v \"$tool\" >/dev/null 2>&1 && echo \"$tool=present\" || echo \"$tool=missing\"; done",
    "recent_errors": "journalctl -p err..alert -n 25 --no-pager 2>/dev/null || true",
    "sudo_nopasswd": "sudo -n true >/dev/null 2>&1 && echo yes || echo no",
}


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    starttls: bool
    username: str
    password: str
    sender: str
    recipients: list[str]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_after(hours: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    redacted = TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = URL_SECRET_RE.sub(r"\1<redacted>\3", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def parse_free_bytes(text: str) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return rows

    headers = lines[0].split()
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        name = parts[0].rstrip(":").lower()
        values: dict[str, int] = {}
        for header, raw in zip(headers, parts[1:]):
            try:
                values[header.lower()] = int(raw)
            except ValueError:
                continue
        rows[name] = values
    return rows


def parse_df_line(text: str) -> dict[str, Any]:
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


def parse_upgradable_count(text: str) -> int | None:
    stripped = text.strip()
    if not stripped:
        return 0
    try:
        return int(stripped.splitlines()[-1].strip())
    except ValueError:
        return None


def run_ssh_command(host: str, user: str, key: str, known_hosts: str, command: str, timeout: int) -> dict[str, Any]:
    ssh_command = [
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
        "returncode": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def collect_ssh(host: str, user: str, key: str, known_hosts: str, timeout: int) -> dict[str, Any]:
    commands: dict[str, Any] = {}
    for name, command in REMOTE_COMMANDS.items():
        try:
            commands[name] = run_ssh_command(host, user, key, known_hosts, command, timeout)
        except Exception as exc:  # pragma: no cover - defensive for subprocess edge cases
            commands[name] = {"returncode": 255, "stdout": "", "stderr": redact(str(exc))}
    return {"host": host, "user": user, "commands": commands}


def command_stdout(report: dict[str, Any], name: str) -> str:
    return str(report.get("ssh", {}).get("commands", {}).get(name, {}).get("stdout", ""))


def classify(report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    checks: list[dict[str, Any]] = []

    memory = parse_free_bytes(command_stdout(report, "memory"))
    mem = memory.get("mem", {})
    mem_total = mem.get("total", 0)
    mem_used = mem.get("used", 0)
    mem_used_percent = round((mem_used / mem_total) * 100, 1) if mem_total else None
    checks.append(threshold_check("memory_used_percent", mem_used_percent, warn=80, crit=90, unit="%"))

    disk = parse_df_line(command_stdout(report, "root_disk"))
    checks.append(threshold_check("root_disk_used_percent", disk.get("used_percent"), warn=80, crit=90, unit="%"))

    inodes = parse_df_line(command_stdout(report, "root_inodes"))
    checks.append(threshold_check("root_inode_used_percent", inodes.get("used_percent"), warn=80, crit=90, unit="%"))

    failed_units = command_stdout(report, "failed_units").strip()
    checks.append(
        {
            "name": "failed_systemd_units",
            "status": "healthy" if not failed_units else "critical",
            "summary": "no failed systemd units" if not failed_units else "failed systemd units present",
        }
    )

    reboot_required = command_stdout(report, "reboot_required").strip()
    checks.append(
        {
            "name": "reboot_required",
            "status": "warning" if reboot_required == "yes" else "healthy" if reboot_required == "no" else "unknown",
            "summary": f"reboot_required={reboot_required or 'unknown'}",
        }
    )

    upgradable_count = parse_upgradable_count(command_stdout(report, "upgradable_count"))
    checks.append(
        {
            "name": "package_updates",
            "status": "warning" if (upgradable_count or 0) > 0 else "healthy" if upgradable_count == 0 else "unknown",
            "summary": f"upgradable_packages={upgradable_count if upgradable_count is not None else 'unknown'}",
        }
    )

    services = parse_key_values(command_stdout(report, "service_states"))
    for service in ("ssh", "ufw", "fail2ban", "docker", "caddy", "postgresql", "alloy", "sysstat"):
        state = services.get(service, "unavailable")
        expected_missing = service in {"docker", "postgresql", "alloy"}
        if state == "active":
            status = "healthy"
        elif expected_missing and state in {"inactive", "unavailable", "failed"}:
            status = "not_configured"
        elif service == "fail2ban" and state in {"inactive", "unavailable", "failed"}:
            status = "warning"
        else:
            status = "warning" if state in {"inactive", "unavailable"} else "critical"
        checks.append({"name": f"service_{service}", "status": status, "summary": f"{service}={state}"})

    endpoint_health = command_stdout(report, "backend_health").strip()
    caddy_state = services.get("caddy", "unavailable")
    if endpoint_health == "ok":
        endpoint_status = "healthy"
    elif caddy_state == "active":
        endpoint_status = "critical"
    else:
        endpoint_status = "not_configured"
    checks.append(
        {
            "name": "backend_endpoint_health",
            "status": endpoint_status,
            "summary": f"backend_endpoint_health={endpoint_health or 'empty'}",
        }
    )

    backup_tools = parse_key_values(command_stdout(report, "backup_tools"))
    restic_state = backup_tools.get("restic", "missing")
    checks.append(
        {
            "name": "backup_tooling",
            "status": "not_configured" if restic_state == "missing" else "healthy",
            "summary": f"restic={restic_state}",
        }
    )

    sudo_state = command_stdout(report, "sudo_nopasswd").strip()
    checks.append(
        {
            "name": "sudo_nopasswd",
            "status": "warning" if sudo_state == "no" else "healthy" if sudo_state == "yes" else "unknown",
            "summary": f"sudo_nopasswd={sudo_state or 'unknown'}",
        }
    )

    summary = {
        "critical": sum(1 for item in checks if item["status"] == "critical"),
        "warning": sum(1 for item in checks if item["status"] == "warning"),
        "unknown": sum(1 for item in checks if item["status"] == "unknown"),
        "not_configured": sum(1 for item in checks if item["status"] == "not_configured"),
        "healthy": sum(1 for item in checks if item["status"] == "healthy"),
    }
    return checks, summary


def threshold_check(name: str, value: float | int | None, warn: int, crit: int, unit: str) -> dict[str, Any]:
    if value is None:
        return {"name": name, "status": "unknown", "value": None, "summary": f"{name}=unknown"}
    if value >= crit:
        status = "critical"
    elif value >= warn:
        status = "warning"
    else:
        status = "healthy"
    return {"name": name, "status": status, "value": value, "summary": f"{name}={value}{unit}"}


def smtp_config_from_env() -> tuple[SmtpConfig | None, list[str], list[str]]:
    required = [
        "NUTSNEWS_REPORT_SMTP_HOST",
        "NUTSNEWS_REPORT_SMTP_USERNAME",
        "NUTSNEWS_REPORT_SMTP_PASSWORD",
        "NUTSNEWS_REPORT_EMAIL_FROM",
        "NUTSNEWS_REPORT_EMAIL_TO",
    ]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        return None, missing, []

    shape_errors: list[str] = []
    port_raw = os.environ.get("NUTSNEWS_REPORT_SMTP_PORT", "587").strip() or "587"
    try:
        port = int(port_raw)
    except ValueError:
        shape_errors.append("NUTSNEWS_REPORT_SMTP_PORT must be an integer")
        port = 587

    sender = os.environ["NUTSNEWS_REPORT_EMAIL_FROM"].strip()
    recipients = [item.strip() for item in os.environ["NUTSNEWS_REPORT_EMAIL_TO"].split(",") if item.strip()]
    if not EMAIL_RE.fullmatch(sender):
        shape_errors.append("NUTSNEWS_REPORT_EMAIL_FROM must be an email address")
    if not recipients or any(not EMAIL_RE.fullmatch(item) for item in recipients):
        shape_errors.append("NUTSNEWS_REPORT_EMAIL_TO must be one or more comma-separated email addresses")

    if shape_errors:
        return None, missing, shape_errors

    starttls = os.environ.get("NUTSNEWS_REPORT_SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "no"}
    return (
        SmtpConfig(
            host=os.environ["NUTSNEWS_REPORT_SMTP_HOST"].strip(),
            port=port,
            starttls=starttls,
            username=os.environ["NUTSNEWS_REPORT_SMTP_USERNAME"].strip(),
            password=os.environ["NUTSNEWS_REPORT_SMTP_PASSWORD"],
            sender=sender,
            recipients=recipients,
        ),
        [],
        [],
    )


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "NutsNews backend health report",
        f"Run: {report['last_report_run_at']}",
        f"Next expected run: {report['next_report_run_at']}",
        f"Host: {report['target']['user']}@{report['target']['host']}",
        "",
        "Summary:",
    ]
    for key in ("critical", "warning", "unknown", "not_configured", "healthy"):
        lines.append(f"- {key}: {report['summary'][key]}")

    lines.extend(["", "Checks:"])
    for check in report["checks"]:
        lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")

    lines.extend(
        [
            "",
            "Delivery:",
            f"- status: {report['delivery']['status']}",
            f"- detail: {report['delivery'].get('detail', '')}",
            "",
            "This report is generated by a fixed-purpose GitHub Actions workflow using read-only SSH commands.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_summary(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Backend Health Report",
        "",
        f"- Run: `{report['last_report_run_at']}`",
        f"- Next expected run: `{report['next_report_run_at']}`",
        f"- Target: `{report['target']['user']}@{report['target']['host']}`",
        f"- Delivery: `{report['delivery']['status']}`",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for key in ("critical", "warning", "unknown", "not_configured", "healthy"):
        lines.append(f"| `{key}` | {report['summary'][key]} |")
    lines.extend(["", "## Checks", "", "| Status | Check |", "| --- | --- |"])
    for check in report["checks"]:
        lines.append(f"| `{check['status']}` | {check['summary']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def send_email(config: SmtpConfig, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(body)

    with smtplib.SMTP(config.host, config.port, timeout=20) as client:
        if config.starttls:
            client.starttls()
        client.login(config.username, config.password)
        client.send_message(message)


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    run_at = utc_now()
    report: dict[str, Any] = {
        "version": 1,
        "last_report_run_at": run_at,
        "next_report_run_at": iso_after(args.next_run_interval_hours),
        "last_report_success_at": None,
        "last_error": None,
        "target": {"host": args.ssh_host, "user": args.ssh_user},
        "delivery": {"status": "not_attempted", "detail": "email disabled"},
        "ssh": {},
        "checks": [],
        "summary": {},
    }

    if args.ssh_key and args.known_hosts and Path(args.ssh_key).exists() and Path(args.known_hosts).exists():
        report["ssh"] = collect_ssh(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    else:
        report["last_error"] = "missing SSH key or known_hosts file"
        report["ssh"] = {"host": args.ssh_host, "user": args.ssh_user, "commands": {}}

    checks, summary = classify(report)
    report["checks"] = checks
    report["summary"] = summary

    if args.send_email:
        config, missing, shape_errors = smtp_config_from_env()
        if missing:
            report["delivery"] = {"status": "not_configured", "detail": f"missing: {', '.join(sorted(missing))}"}
        elif shape_errors:
            report["delivery"] = {"status": "error", "detail": "; ".join(shape_errors)}
            report["last_error"] = report["delivery"]["detail"]
        elif config is not None:
            try:
                subject_prefix = os.environ.get("NUTSNEWS_REPORT_SUBJECT_PREFIX", "[NutsNews backend]").strip()
                subject = f"{subject_prefix} health report: {summary['critical']} critical, {summary['warning']} warning"
                send_email(config, subject, render_text(report))
                report["delivery"] = {"status": "sent", "detail": f"sent_to_count={len(config.recipients)}"}
            except Exception as exc:  # pragma: no cover - network/provider dependent
                report["delivery"] = {"status": "error", "detail": redact(str(exc))}
                report["last_error"] = report["delivery"]["detail"]
    else:
        report["delivery"] = {"status": "skipped", "detail": "send_email=false"}

    if report["last_error"] is None and report["delivery"]["status"] != "error":
        report["last_report_success_at"] = run_at

    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--known-hosts", default="")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--next-run-interval-hours", type=int, default=24)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--send-email", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    report = build_report(args)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.summary:
        write_summary(report, Path(args.summary))

    print(render_text(report))
    return 0 if report["delivery"]["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
