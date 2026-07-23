#!/usr/bin/env python3
"""Run fixed-purpose backend maintenance prechecks and approved actions.

This script intentionally accepts only a closed set of maintenance actions. It
does not accept arbitrary remote command input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CONFIRM_TARGET = "backend.nutsnews.com"
VALID_STATUSES = {"healthy", "warning", "critical", "not_configured", "unknown"}
RABBITMQ_PROBE_PATH = "/usr/local/sbin/nutsnews-rabbitmq-probe"
RABBITMQ_ENV_PATH = "/etc/nutsnews-rabbitmq/rabbitmq.env"
RABBITMQ_HOST_RESTART_STATE_PATH = "/var/lib/nutsnews/rabbitmq-probes/host-restart-probe.json"
RABBITMQ_HOST_RESTART_QUEUE = "worker.uplift.probe.host-restart"
RABBITMQ_PROBE_TIMEOUT_SECONDS = 180

TOKEN_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|"
    r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
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
    "boot_id": "cat /proc/sys/kernel/random/boot_id",
    "latest_installed_kernel": (
        "latest=$(find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\\n' 2>/dev/null "
        "| sed 's/^vmlinuz-//' | sort -V | tail -n 1); printf '%s\\n' \"${latest:-unknown}\""
    ),
    "failed_units": "systemctl --failed --no-legend --no-pager || true",
    "ssh_state": "systemctl is-active ssh 2>/dev/null || true",
    "ufw_state": "systemctl is-active ufw 2>/dev/null || true",
    "fail2ban_state": "systemctl is-active fail2ban 2>/dev/null || true",
    "docker_state": "systemctl is-active docker 2>/dev/null || true",
    "rabbitmq_state": "systemctl is-active nutsnews-rabbitmq 2>/dev/null || true",
    "rabbitmq_health": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        f"&& test -x {RABBITMQ_PROBE_PATH}; then "
        f"sudo -n {RABBITMQ_PROBE_PATH} health --env {RABBITMQ_ENV_PATH} 2>/dev/null || echo critical; "
        "else echo not_configured; fi"
    ),
    "caddy_state": "systemctl is-active caddy 2>/dev/null || true",
    "backend_units": "systemctl list-units --type=service --all --no-legend 'nutsnews*' 2>/dev/null || true",
    "backend_endpoint": (
        "if command -v curl >/dev/null 2>&1 && systemctl is-active caddy >/dev/null 2>&1; then "
        "curl -fsS --connect-timeout 5 "
        "--resolve backend.nutsnews.com:443:127.0.0.1 "
        "https://backend.nutsnews.com/healthz 2>/dev/null || true; "
        "else echo unavailable; fi"
    ),
    "root_disk": "df -PB1 / | tail -n +2",
    "root_inodes": "df -Pi / | tail -n +2",
    "memory": "free -b",
    "reboot_required": "test -e /var/run/reboot-required && echo yes || echo no",
    "upgradable_count": "apt list --upgradable 2>/dev/null | tail -n +2 | wc -l",
    "unattended_upgrade": "command -v unattended-upgrade >/dev/null 2>&1 && echo present || echo missing",
    "unattended_upgrades_enabled": "systemctl is-enabled unattended-upgrades 2>/dev/null || true",
    "backup_state": (
        "if test -r /var/lib/nutsnews/backups/last-backup.json; then "
        "cat /var/lib/nutsnews/backups/last-backup.json; "
        "elif command -v resticprofile >/dev/null 2>&1; then "
        "echo resticprofile_present; systemctl is-active resticprofile.timer 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "active_alerts": (
        "if test -d /var/lib/nutsnews/alerts; then "
        "find /var/lib/nutsnews/alerts -maxdepth 1 -type f -name '*.active' -printf '%f\\n' | head -20; "
        "else echo not_configured; fi"
    ),
}

MAINTENANCE_COMMANDS: dict[str, str] = {
    "security-upgrade": "sudo -n env DEBIAN_FRONTEND=noninteractive unattended-upgrade -v",
    "reboot": "sudo -n systemctl reboot",
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    redacted = TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = URL_SECRET_RE.sub(r"\1<redacted>\3", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def ssh_command(host: str, user: str, key: Path, known_hosts: Path, command: str, timeout: int) -> list[str]:
    return [
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


def run_ssh_command(host: str, user: str, key: Path, known_hosts: Path, command: str, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        ssh_command(host, user, key, known_hosts, command, timeout),
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


def collect_live(host: str, user: str, key: Path, known_hosts: Path, timeout: int) -> dict[str, Any]:
    commands = {}
    for name, command in REMOTE_COMMANDS.items():
        commands[name] = run_ssh_command(host, user, key, known_hosts, command, timeout)
    return {"commands": commands}


def command_stdout(evidence: dict[str, Any], name: str) -> str:
    return evidence.get("commands", {}).get(name, {}).get("stdout", "")


def parse_df_line(text: str) -> dict[str, Any]:
    line = next((item for item in text.splitlines() if item.strip()), "")
    parts = line.split()
    if len(parts) < 6:
        return {"status": "unknown", "used_percent": None, "raw": redact(line)}
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
        return int(stripped.splitlines()[-1])
    except ValueError:
        return None


def threshold_status(value: int | float | None, warn: int = 80, crit: int = 90) -> str:
    if value is None:
        return "unknown"
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "healthy"


def service_check(name: str, state: str, *, required: bool) -> dict[str, Any]:
    normalized = state.strip() or "unavailable"
    if normalized == "active":
        status = "healthy"
    elif required:
        status = "critical" if normalized == "failed" else "warning"
    else:
        status = "not_configured" if normalized in {"inactive", "unavailable", "failed"} else "unknown"
    return {"name": name, "status": status, "summary": f"{name}={normalized}"}


def classify_backup_state(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if stripped == "not_configured":
        return "not_configured", "not_configured"
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return "unknown", "backup status json invalid"
        status = str(data.get("freshness_status") or data.get("status") or "unknown")
        if status == "ok":
            status = "healthy"
        if status not in VALID_STATUSES:
            status = "unknown"
        snapshot = data.get("snapshot_id") or "none"
        return status, f"snapshot={snapshot}"
    if "active" in stripped or "waiting" in stripped:
        return "healthy", stripped
    if stripped:
        return "warning", stripped
    return "unknown", "unknown"


def classify_rabbitmq_health(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if stripped == "not_configured":
        return "not_configured", "rabbitmq=not_configured"
    if stripped == "critical":
        return "critical", "rabbitmq=critical"
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return "unknown", "rabbitmq health json invalid"
        status = str(data.get("status") or "unknown")
        if status not in VALID_STATUSES:
            status = "unknown"
        version = data.get("rabbitmq_version") or "unknown"
        return status, f"rabbitmq={status} version={version}"
    if stripped:
        return "unknown", redact(stripped.splitlines()[-1])
    return "unknown", "rabbitmq=unknown"


def classify_prechecks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    failed_units = [line for line in command_stdout(evidence, "failed_units").splitlines() if line.strip()]
    checks.append(
        {
            "name": "failed_systemd_units",
            "status": "healthy" if not failed_units else "critical",
            "summary": "no failed systemd units" if not failed_units else f"{len(failed_units)} failed systemd unit(s)",
            "details": failed_units[:10],
        }
    )

    kernel = command_stdout(evidence, "kernel").strip()
    latest_kernel = command_stdout(evidence, "latest_installed_kernel").strip()
    if not latest_kernel or latest_kernel == "unknown":
        kernel_status = "unknown"
    elif kernel == latest_kernel:
        kernel_status = "healthy"
    else:
        kernel_status = "warning"
    checks.append(
        {
            "name": "kernel_alignment",
            "status": kernel_status,
            "summary": f"running={kernel or 'unknown'} latest_installed={latest_kernel or 'unknown'}",
        }
    )

    root_disk = parse_df_line(command_stdout(evidence, "root_disk"))
    checks.append(
        {
            "name": "root_disk_pressure",
            "status": threshold_status(root_disk.get("used_percent")),
            "summary": f"root_disk_used_percent={root_disk.get('used_percent', 'unknown')}",
        }
    )

    root_inodes = parse_df_line(command_stdout(evidence, "root_inodes"))
    checks.append(
        {
            "name": "root_inode_pressure",
            "status": threshold_status(root_inodes.get("used_percent")),
            "summary": f"root_inode_used_percent={root_inodes.get('used_percent', 'unknown')}",
        }
    )

    checks.extend(
        [
            service_check("service_ssh", command_stdout(evidence, "ssh_state"), required=True),
            service_check("service_ufw", command_stdout(evidence, "ufw_state"), required=True),
            service_check("service_fail2ban", command_stdout(evidence, "fail2ban_state"), required=True),
            service_check("service_docker", command_stdout(evidence, "docker_state"), required=False),
            service_check("service_rabbitmq", command_stdout(evidence, "rabbitmq_state"), required=False),
            service_check("service_caddy", command_stdout(evidence, "caddy_state"), required=True),
        ]
    )

    rabbitmq_status, rabbitmq_summary = classify_rabbitmq_health(command_stdout(evidence, "rabbitmq_health"))
    checks.append({"name": "rabbitmq_health", "status": rabbitmq_status, "summary": rabbitmq_summary})

    endpoint = command_stdout(evidence, "backend_endpoint").strip()
    checks.append(
        {
            "name": "reverse_proxy_health",
            "status": "healthy" if endpoint == "ok" else "critical",
            "summary": f"backend.nutsnews.com local health={endpoint or 'empty'}",
        }
    )

    backend_units = [
        line
        for line in command_stdout(evidence, "backend_units").splitlines()
        if line.strip() and "nutsnews-ops-dashboard-collect" not in line
    ]
    checks.append(
        {
            "name": "backend_app_health",
            "status": "not_configured" if not backend_units else "healthy" if endpoint == "ok" else "critical",
            "summary": "backend app not deployed" if not backend_units else f"backend_units={len(backend_units)}",
        }
    )

    backup_status, backup_summary = classify_backup_state(command_stdout(evidence, "backup_state"))
    checks.append({"name": "backup_freshness", "status": backup_status, "summary": backup_summary})

    active_alerts = command_stdout(evidence, "active_alerts").strip()
    if active_alerts == "not_configured":
        alerts_status = "not_configured"
        alerts_summary = "alert state not configured"
    elif active_alerts:
        alerts_status = "critical"
        alerts_summary = "active alerts present"
    else:
        alerts_status = "healthy"
        alerts_summary = "no active alerts"
    checks.append({"name": "active_alerts", "status": alerts_status, "summary": alerts_summary})

    reboot_required = command_stdout(evidence, "reboot_required").strip()
    checks.append(
        {
            "name": "reboot_required",
            "status": "warning" if reboot_required == "yes" else "healthy" if reboot_required == "no" else "unknown",
            "summary": f"reboot_required={reboot_required or 'unknown'}",
        }
    )

    upgradable_count = parse_upgradable_count(command_stdout(evidence, "upgradable_count"))
    checks.append(
        {
            "name": "package_updates_visible",
            "status": "warning" if (upgradable_count or 0) > 0 else "healthy" if upgradable_count == 0 else "unknown",
            "summary": f"upgradable_packages={upgradable_count if upgradable_count is not None else 'unknown'}",
        }
    )

    unattended = command_stdout(evidence, "unattended_upgrade").strip()
    unattended_enabled = command_stdout(evidence, "unattended_upgrades_enabled").strip()
    checks.append(
        {
            "name": "unattended_security_updates",
            "status": "healthy" if unattended == "present" else "warning",
            "summary": f"unattended-upgrade={unattended or 'unknown'} enabled={unattended_enabled or 'unknown'}",
        }
    )

    for check in checks:
        if check["status"] not in VALID_STATUSES:
            check["status"] = "unknown"
    return checks


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {status: sum(1 for check in checks if check["status"] == status) for status in sorted(VALID_STATUSES)}


def mutation_blockers(action: str, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    by_name = {check["name"]: check for check in checks}

    required_healthy_for_all = ("failed_systemd_units", "root_disk_pressure", "root_inode_pressure", "service_ssh")
    for name in required_healthy_for_all:
        check = by_name.get(name, {})
        if check.get("status") in {"critical", "unknown"}:
            blockers.append({"check": name, "status": str(check.get("status", "missing"))})

    if action == "security-upgrade":
        check = by_name.get("unattended_security_updates", {})
        if check.get("status") != "healthy":
            blockers.append({"check": "unattended_security_updates", "status": str(check.get("status", "missing"))})

    if action == "reboot":
        for name in (
            "service_ufw",
            "service_fail2ban",
            "service_caddy",
            "reverse_proxy_health",
            "backup_freshness",
            "active_alerts",
        ):
            check = by_name.get(name, {})
            if check.get("status") != "healthy":
                blockers.append({"check": name, "status": str(check.get("status", "missing"))})

    return blockers


def report_state(action: str, target: dict[str, str], evidence: dict[str, Any]) -> dict[str, Any]:
    checks = classify_prechecks(evidence)
    return {
        "checked_at_utc": utc_now(),
        "action": action,
        "target": target,
        "boot_id": command_stdout(evidence, "boot_id").strip(),
        "kernel": command_stdout(evidence, "kernel").strip(),
        "latest_installed_kernel": command_stdout(evidence, "latest_installed_kernel").strip(),
        "reboot_required": command_stdout(evidence, "reboot_required").strip() or "unknown",
        "checks": checks,
        "summary": summarize_checks(checks),
        "mutation_blockers": mutation_blockers(action, checks),
    }


def write_summary(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Backend Controlled Maintenance",
        "",
        f"- Action: `{report['action']}`",
        f"- Status: `{report['status']}`",
        f"- Target: `{report['target']['user']}@{report['target']['host']}`",
        f"- Started: `{report['started_at_utc']}`",
        f"- Finished: `{report['finished_at_utc']}`",
        "",
        "## Prechecks",
        "",
        "| Check | Status | Summary |",
        "| --- | --- | --- |",
    ]
    for check in report["precheck"]["checks"]:
        lines.append(f"| `{check['name']}` | `{check['status']}` | {check['summary']} |")
    if report["precheck"]["mutation_blockers"]:
        lines.extend(["", "## Mutation Blockers", ""])
        for blocker in report["precheck"]["mutation_blockers"]:
            lines.append(f"- `{blocker['check']}` is `{blocker['status']}`")
    if report.get("postcheck"):
        lines.extend(["", "## Postchecks", "", "| Check | Status | Summary |", "| --- | --- | --- |"])
        for check in report["postcheck"]["checks"]:
            lines.append(f"| `{check['name']}` | `{check['status']}` | {check['summary']} |")
        lines.append(f"- Boot ID changed: `{report['postcheck'].get('boot_id_changed')}`")
        lines.append(f"- Kernel changed: `{report['postcheck'].get('kernel_changed')}`")
    if report.get("rabbitmq_host_reboot_probe"):
        probe = report["rabbitmq_host_reboot_probe"]
        lines.extend(["", "## RabbitMQ Host-Reboot Probe", ""])
        lines.append(f"- Required: `{probe.get('required')}`")
        if probe.get("reason"):
            lines.append(f"- Reason: `{probe.get('reason')}`")
        if probe.get("publish"):
            lines.append(f"- Publish: `{probe['publish'].get('status')}`")
        if probe.get("verify"):
            lines.append(f"- Verify: `{probe['verify'].get('status')}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def execute_fixed_action(
    action: str,
    host: str,
    user: str,
    key: Path,
    known_hosts: Path,
    timeout: int,
) -> dict[str, Any]:
    command = MAINTENANCE_COMMANDS[action]
    return run_ssh_command(host, user, key, known_hosts, command, timeout)


def rabbitmq_probe_command(action: str) -> str:
    if action not in {"publish", "verify"}:
        raise ValueError(f"unsupported RabbitMQ probe action: {action}")
    args = (
        f"{RABBITMQ_PROBE_PATH} {action} "
        f"--env {RABBITMQ_ENV_PATH} "
        f"--queue {RABBITMQ_HOST_RESTART_QUEUE} "
        f"--state {RABBITMQ_HOST_RESTART_STATE_PATH} "
        f"--timeout-seconds {RABBITMQ_PROBE_TIMEOUT_SECONDS}"
    )
    if action == "verify":
        args = f"{args} --delete-queue"
    return (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        f"&& test -x {RABBITMQ_PROBE_PATH}; then "
        f"sudo -n {args}; "
        "else echo not_configured; fi"
    )


def run_rabbitmq_probe_action(
    action: str,
    host: str,
    user: str,
    key: Path,
    known_hosts: Path,
    timeout: int,
) -> dict[str, Any]:
    probe_timeout = max(timeout, RABBITMQ_PROBE_TIMEOUT_SECONDS + 20)
    result = run_ssh_command(host, user, key, known_hosts, rabbitmq_probe_command(action), probe_timeout)
    status = "pass" if result["returncode"] == 0 and result["stdout"].strip() != "not_configured" else "fail"
    return {
        "action": action,
        "returncode": result["returncode"],
        "stdout": result["stdout"][-4000:],
        "stderr": result["stderr"][-4000:],
        "status": status,
    }


def wait_for_ssh(host: str, user: str, key: Path, known_hosts: Path, timeout: int, wait_seconds: int) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            result = run_ssh_command(host, user, key, known_hosts, "hostname", timeout)
            if result["returncode"] == 0:
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("precheck", "security-upgrade", "reboot"), required=True)
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--reboot-wait-seconds", type=int, default=600)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    started_at = utc_now()
    target = {"host": args.ssh_host, "user": args.ssh_user}

    report: dict[str, Any] = {
        "version": 1,
        "action": args.action,
        "target": target,
        "started_at_utc": started_at,
        "finished_at_utc": None,
        "status": "unknown",
        "precheck": {},
        "postcheck": None,
        "rabbitmq_host_reboot_probe": None,
        "action_result": None,
        "secret_redaction": "fixed commands only; stdout/stderr redacted before summary reporting",
    }

    pre_evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    precheck = report_state(args.action, target, pre_evidence)
    report["precheck"] = precheck

    exit_code = 0
    if args.action == "precheck":
        report["status"] = "pass"
    else:
        if args.confirm_target != CONFIRM_TARGET:
            report["status"] = "blocked"
            report["action_result"] = {"status": "not_run", "detail": f"confirm-target must be {CONFIRM_TARGET}"}
            exit_code = 2
        elif precheck["mutation_blockers"]:
            report["status"] = "blocked"
            report["action_result"] = {"status": "not_run", "detail": "precheck blockers present"}
            exit_code = 1
        else:
            rabbitmq_probe_required = False
            if args.action == "reboot":
                prechecks_by_name = {check["name"]: check for check in precheck["checks"]}
                rabbitmq_probe_required = prechecks_by_name.get("rabbitmq_health", {}).get("status") == "healthy"
                if rabbitmq_probe_required:
                    publish_probe = run_rabbitmq_probe_action(
                        "publish",
                        args.ssh_host,
                        args.ssh_user,
                        args.ssh_key,
                        args.known_hosts,
                        args.timeout,
                    )
                    report["rabbitmq_host_reboot_probe"] = {"required": True, "publish": publish_probe, "verify": None}
                    if publish_probe["status"] != "pass":
                        report["status"] = "fail"
                        report["action_result"] = {"status": "not_run", "detail": "RabbitMQ host-restart probe publish failed"}
                        report["finished_at_utc"] = utc_now()
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                        if args.summary:
                            write_summary(args.summary, report)
                        print(json.dumps({"status": report["status"], "precheck_summary": precheck["summary"]}, indent=2))
                        return 1
                else:
                    report["rabbitmq_host_reboot_probe"] = {"required": False, "reason": "rabbitmq_health_not_healthy_or_not_configured"}
            result = execute_fixed_action(args.action, args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
            report["action_result"] = {
                "status": "run",
                "returncode": result["returncode"],
                "stdout": result["stdout"][-4000:],
                "stderr": result["stderr"][-4000:],
            }
            if args.action == "reboot":
                if not wait_for_ssh(
                    args.ssh_host,
                    args.ssh_user,
                    args.ssh_key,
                    args.known_hosts,
                    args.timeout,
                    args.reboot_wait_seconds,
                ):
                    report["status"] = "fail"
                    report["action_result"]["status"] = "ssh_reconnect_timeout"
                    exit_code = 1
                else:
                    verify_probe = None
                    if rabbitmq_probe_required:
                        verify_probe = run_rabbitmq_probe_action(
                            "verify",
                            args.ssh_host,
                            args.ssh_user,
                            args.ssh_key,
                            args.known_hosts,
                            args.timeout,
                        )
                        if isinstance(report["rabbitmq_host_reboot_probe"], dict):
                            report["rabbitmq_host_reboot_probe"]["verify"] = verify_probe
                    post_evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
                    postcheck = report_state(args.action, target, post_evidence)
                    postcheck["boot_id_changed"] = postcheck["boot_id"] != precheck["boot_id"]
                    postcheck["kernel_changed"] = postcheck["kernel"] != precheck["kernel"]
                    report["postcheck"] = postcheck
                    rabbitmq_probe_passed = not rabbitmq_probe_required or (verify_probe is not None and verify_probe["status"] == "pass")
                    report["status"] = "pass" if postcheck["boot_id_changed"] and rabbitmq_probe_passed else "fail"
                    exit_code = 0 if report["status"] == "pass" else 1
            else:
                post_evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
                report["postcheck"] = report_state(args.action, target, post_evidence)
                report["status"] = "pass" if result["returncode"] == 0 else "fail"
                exit_code = 0 if result["returncode"] == 0 else 1

    report["finished_at_utc"] = utc_now()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, report)
    print(json.dumps({"status": report["status"], "precheck_summary": precheck["summary"]}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
