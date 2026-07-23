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

try:
    from scripts import backend_alert_state
except ModuleNotFoundError:  # pragma: no cover - script-path execution
    import backend_alert_state


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
    "latest_installed_kernel": (
        "latest=$(find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\\n' 2>/dev/null "
        "| sed 's/^vmlinuz-//' | sort -V | tail -n 1); printf '%s\\n' \"${latest:-unknown}\""
    ),
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
        "for unit in ssh ufw fail2ban docker caddy postgresql alloy sysstat nutsnews-backup.timer nutsnews-rabbitmq nutsnews-rabbitmq-canary.timer; do "
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
    "backup_status": (
        "if test -x /usr/local/sbin/nutsnews-backup; then "
        "/usr/local/sbin/nutsnews-backup status 2>/dev/null || sudo -n /usr/local/sbin/nutsnews-backup status 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_drift": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        "&& test -x /usr/local/sbin/nutsnews-rabbitmq-probe; then "
        "sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe drift "
        "--env /etc/nutsnews-rabbitmq/rabbitmq.env "
        "--credentials-env /etc/nutsnews-rabbitmq/topology.env "
        "--definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json "
        "--metadata /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_smoke_status": (
        "if test -r /var/lib/nutsnews/rabbitmq-probes/last-smoke.json; then "
        "cat /var/lib/nutsnews/rabbitmq-probes/last-smoke.json; "
        "elif sudo -n test -r /var/lib/nutsnews/rabbitmq-probes/last-smoke.json 2>/dev/null; then "
        "sudo -n cat /var/lib/nutsnews/rabbitmq-probes/last-smoke.json; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_canary_status": (
        "if test -r /var/lib/nutsnews/rabbitmq-probes/last-canary.json; then "
        "cat /var/lib/nutsnews/rabbitmq-probes/last-canary.json; "
        "elif sudo -n test -r /var/lib/nutsnews/rabbitmq-probes/last-canary.json 2>/dev/null; then "
        "sudo -n cat /var/lib/nutsnews/rabbitmq-probes/last-canary.json; "
        "else echo not_configured; fi"
    ),
    "cleanup_status": (
        "if test -r /var/lib/nutsnews/cleanup/last-cleanup.json; then "
        "cat /var/lib/nutsnews/cleanup/last-cleanup.json; "
        "else echo not_configured; fi"
    ),
    "recovery_status": (
        "if test -r /var/lib/nutsnews/recovery/last-recovery.json; then "
        "cat /var/lib/nutsnews/recovery/last-recovery.json; "
        "else echo not_configured; fi"
    ),
    "postgres_status": (
        "if test -r /var/lib/nutsnews/postgres/status.json; then "
        "cat /var/lib/nutsnews/postgres/status.json; "
        "else echo not_configured; fi"
    ),
    "postgres_replication_health": (
        "if test -r /var/lib/nutsnews/postgres/replication-health.json; then "
        "cat /var/lib/nutsnews/postgres/replication-health.json; "
        "else echo not_configured; fi"
    ),
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


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped or stripped == "not_configured":
        return {}
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_previous_report(path: str) -> dict[str, Any]:
    if not path:
        return {}
    previous_path = Path(path)
    if not previous_path.exists():
        return {}
    try:
        data = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def status_from_backup_section(section: dict[str, Any]) -> str:
    status = str(section.get("freshness_status") or section.get("status") or "not_configured")
    if status == "ok":
        return "healthy"
    if status in {"healthy", "warning", "critical", "not_configured", "unknown"}:
        return status
    return "unknown"


def status_from_replication(replication: dict[str, Any]) -> str:
    if not replication:
        return "not_configured"
    explicit = str(replication.get("status") or "").strip()
    if explicit == "healthy":
        return "healthy"
    if explicit in {"fail", "critical"}:
        return "critical"
    if explicit in {"blocked", "warning"}:
        return "warning"
    lag_status = str(replication.get("lag_status") or "not_configured")
    slot_status = str(replication.get("slot_status") or "not_configured")
    validation_status = str(replication.get("validation_status") or "not_configured")
    blockers = replication.get("blockers", [])
    if blockers or lag_status in {"lagging", "inactive"}:
        return "critical"
    if lag_status in {"unknown"} or slot_status == "unknown" or validation_status == "stale":
        return "warning"
    if lag_status in {"healthy"}:
        return "healthy"
    return "not_configured"


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

    kernel = command_stdout(report, "kernel").strip()
    latest_kernel = command_stdout(report, "latest_installed_kernel").strip()
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

    services = parse_key_values(command_stdout(report, "service_states"))
    for service in ("ssh", "ufw", "fail2ban", "docker", "caddy", "postgresql", "alloy", "sysstat", "nutsnews-backup.timer", "nutsnews-rabbitmq-canary.timer"):
        state = services.get(service, "unavailable")
        expected_missing = service in {"docker", "postgresql", "alloy", "nutsnews-backup.timer"}
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

    backup_status = parse_json_object(command_stdout(report, "backup_status"))
    backup = backup_status.get("backup", {})
    verification = backup_status.get("verification", {})
    restore_drill = backup_status.get("restore_drill", {})
    rabbitmq_recovery = backup_status.get("rabbitmq_recovery", {})
    quota = backup.get("quota", {}) if isinstance(backup, dict) else {}

    checks.extend(
        [
            {
                "name": "backup_freshness",
                "status": status_from_backup_section(backup if isinstance(backup, dict) else {}),
                "summary": f"snapshot={backup.get('snapshot_id') if isinstance(backup, dict) else None}",
            },
            {
                "name": "backup_verification",
                "status": status_from_backup_section(verification if isinstance(verification, dict) else {}),
                "summary": f"snapshot={verification.get('snapshot_id') if isinstance(verification, dict) else None}",
            },
            {
                "name": "backup_restore_drill",
                "status": status_from_backup_section(restore_drill if isinstance(restore_drill, dict) else {}),
                "summary": f"snapshot={restore_drill.get('snapshot_id') if isinstance(restore_drill, dict) else None}",
            },
            {
                "name": "backup_storage_quota",
                "status": status_from_backup_section(quota if isinstance(quota, dict) else {}),
                "summary": f"quota_status={quota.get('status', 'not_configured') if isinstance(quota, dict) else 'not_configured'}",
            },
        ]
    )
    if isinstance(rabbitmq_recovery, dict):
        for check_name, section_name in (
            ("rabbitmq_definition_export", "definition_export"),
            ("rabbitmq_clean_rebuild_drill", "clean_rebuild_drill"),
            ("rabbitmq_stopped_volume_restore_drill", "stopped_volume_restore_drill"),
        ):
            section = rabbitmq_recovery.get(section_name, {})
            checks.append(
                {
                    "name": check_name,
                    "status": status_from_backup_section(section if isinstance(section, dict) else {}),
                    "summary": f"finished_at={section.get('finished_at_utc') if isinstance(section, dict) else None}",
                }
            )

    rabbitmq_drift_raw = command_stdout(report, "rabbitmq_drift").strip()
    if rabbitmq_drift_raw == "not_configured" or not rabbitmq_drift_raw:
        checks.append({"name": "rabbitmq_drift", "status": "not_configured", "summary": "rabbitmq_drift=not_configured"})
    else:
        rabbitmq_drift = parse_json_object(rabbitmq_drift_raw)
        if rabbitmq_drift.get("status") == "pass":
            checks.append({"name": "rabbitmq_drift", "status": "healthy", "summary": "high_priority_unexpected=none"})
        elif rabbitmq_drift.get("status") == "fail":
            summary = rabbitmq_drift.get("summary") if isinstance(rabbitmq_drift.get("summary"), dict) else {}
            blockers = summary.get("high_priority_unexpected") if isinstance(summary, dict) else []
            checks.append(
                {
                    "name": "rabbitmq_drift",
                    "status": "critical",
                    "summary": f"high_priority_unexpected={','.join(str(item) for item in blockers) or 'unknown'}",
                }
            )
        else:
            checks.append({"name": "rabbitmq_drift", "status": "unknown", "summary": "rabbitmq_drift=invalid_json"})

    rabbitmq_smoke_raw = command_stdout(report, "rabbitmq_smoke_status").strip()
    if rabbitmq_smoke_raw == "not_configured" or not rabbitmq_smoke_raw:
        checks.append({"name": "rabbitmq_smoke_last_run", "status": "not_configured", "summary": "rabbitmq_smoke_last_run=not_configured"})
    else:
        rabbitmq_smoke = parse_json_object(rabbitmq_smoke_raw)
        smoke_status = str(rabbitmq_smoke.get("status") or "unknown")
        checks.append(
            {
                "name": "rabbitmq_smoke_last_run",
                "status": "healthy" if smoke_status == "pass" else "critical" if smoke_status == "fail" else "unknown",
                "summary": f"finished_at={rabbitmq_smoke.get('finished_at_utc', 'unknown')} status={smoke_status}",
            }
        )

    rabbitmq_canary_raw = command_stdout(report, "rabbitmq_canary_status").strip()
    if rabbitmq_canary_raw == "not_configured" or not rabbitmq_canary_raw:
        checks.append({"name": "rabbitmq_canary_last_run", "status": "not_configured", "summary": "rabbitmq_canary_last_run=not_configured"})
    else:
        rabbitmq_canary = parse_json_object(rabbitmq_canary_raw)
        canary_status = str(rabbitmq_canary.get("status") or "unknown")
        checks.append(
            {
                "name": "rabbitmq_canary_last_run",
                "status": "healthy" if canary_status == "pass" else "warning" if canary_status == "expected_failure" else "critical" if canary_status == "fail" else "unknown",
                "summary": (
                    f"finished_at={rabbitmq_canary.get('finished_at_utc', 'unknown')} "
                    f"status={canary_status} failure_class={rabbitmq_canary.get('failure_class', 'none')}"
                ),
            }
        )

    cleanup_status_raw = command_stdout(report, "cleanup_status").strip()
    if cleanup_status_raw == "not_configured" or not cleanup_status_raw:
        checks.append(
            {
                "name": "cleanup_last_run",
                "status": "not_configured",
                "summary": "cleanup_last_run=not_configured",
            }
        )
    else:
        cleanup_status = parse_json_object(cleanup_status_raw)
        status = str(cleanup_status.get("status") or "unknown")
        if status not in {"healthy", "warning", "critical", "unknown"}:
            status = "unknown"
        checks.append(
            {
                "name": "cleanup_last_run",
                "status": status,
                "summary": (
                    f"last_action={cleanup_status.get('action', 'unknown')} "
                    f"last_run={cleanup_status.get('generated_at_utc', 'unknown')}"
                ),
            }
        )

    recovery_status_raw = command_stdout(report, "recovery_status").strip()
    if recovery_status_raw == "not_configured" or not recovery_status_raw:
        checks.append(
            {
                "name": "recovery_last_run",
                "status": "not_configured",
                "summary": "recovery_last_run=not_configured",
            }
        )
    else:
        recovery_status = parse_json_object(recovery_status_raw)
        last_status = str(recovery_status.get("status") or "unknown")
        if last_status == "pass":
            status = "healthy"
        elif last_status == "fail":
            status = "critical"
        elif last_status == "blocked":
            status = "warning"
        else:
            status = "unknown"
        checks.append(
            {
                "name": "recovery_last_run",
                "status": status,
                "summary": (
                    f"last_action={recovery_status.get('action', 'unknown')} "
                    f"last_mode={recovery_status.get('mode', 'unknown')} "
                    f"last_status={last_status}"
                ),
            }
        )

    postgres_status_raw = command_stdout(report, "postgres_status").strip()
    replication_health = parse_json_object(command_stdout(report, "postgres_replication_health"))
    replication_from_health = replication_health.get("replication", {}) if isinstance(replication_health.get("replication"), dict) else {}
    if postgres_status_raw == "not_configured" or not postgres_status_raw:
        checks.append(
            {
                "name": "postgres_restore_readiness",
                "status": "not_configured",
                "summary": "postgres_restore_readiness=not_configured",
            }
        )
        replication_for_check = replication_from_health
    else:
        postgres_status = parse_json_object(postgres_status_raw)
        last_restore = postgres_status.get("last_restore_drill", {})
        restore_status = str(last_restore.get("status") or "unknown") if isinstance(last_restore, dict) else "unknown"
        if restore_status not in {"healthy", "warning", "critical", "unknown", "not_configured"}:
            restore_status = "unknown"
        replication = replication_from_health or postgres_status.get("replication", {})
        lag_status = str(replication.get("lag_status") or "not_configured") if isinstance(replication, dict) else "not_configured"
        replication_for_check = replication if isinstance(replication, dict) else {}
        checks.append(
            {
                "name": "postgres_restore_readiness",
                "status": restore_status,
                "summary": (
                    f"database={postgres_status.get('database', 'unknown')} "
                    f"restore_drill={restore_status} "
                    f"replication_lag={lag_status}"
                ),
            }
        )
    checks.append(
        {
            "name": "postgres_replication_health",
            "status": status_from_replication(replication_for_check),
            "summary": (
                f"mode={replication_for_check.get('mode', 'not_configured')} "
                f"lag={replication_for_check.get('lag_status', 'not_configured')} "
                f"max_lag_seconds={replication_for_check.get('max_lag_seconds', 'unknown')} "
                f"slot={replication_for_check.get('slot_status', 'not_configured')}"
            ),
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

    alerting = report.get("alerting", {})
    if alerting:
        summary = alerting.get("summary", {})
        lines.extend(
            [
                "",
                "Alerting:",
                f"- active_alert_count: {summary.get('active_alert_count', 0)}",
                f"- notification_count: {summary.get('notification_count', 0)}",
                f"- suppressed_count: {summary.get('suppressed_count', 0)}",
                f"- recovered_count: {summary.get('recovered_count', 0)}",
                f"- last_sent_at: {summary.get('last_sent_at') or 'none'}",
                f"- last_error: {summary.get('last_error') or report.get('last_error') or 'none'}",
            ]
        )

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
    alerting = report.get("alerting", {})
    if alerting:
        summary = alerting.get("summary", {})
        lines.extend(
            [
                "",
                "## Alerting",
                "",
                f"- Active alerts: `{summary.get('active_alert_count', 0)}`",
                f"- Notifications: `{summary.get('notification_count', 0)}`",
                f"- Suppressed: `{summary.get('suppressed_count', 0)}`",
                f"- Recovered: `{summary.get('recovered_count', 0)}`",
                f"- Last sent: `{summary.get('last_sent_at') or 'none'}`",
                f"- Last error: `{summary.get('last_error') or report.get('last_error') or 'none'}`",
            ]
        )
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


def failure_class_for_check(check: dict[str, Any]) -> str:
    name = str(check.get("name", ""))
    if check.get("status") == "unknown":
        return "missing_data"
    if name.startswith("backup_"):
        return name
    if name == "postgres_replication_health":
        return "replication_health"
    if "disk" in name or "inode" in name:
        return "disk_pressure"
    if name.startswith("service_"):
        return "service_down"
    if name == "backend_endpoint_health":
        return "backend_health"
    if name in {"reboot_required", "package_updates", "kernel_alignment", "sudo_nopasswd"}:
        return "maintenance"
    return name or "unknown"


def current_alerts_from_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts = []
    for check in checks:
        status = str(check.get("status", "unknown"))
        if status in {"healthy", "not_configured"}:
            continue
        alerts.append(
            {
                "source": "health_report",
                "service": check.get("name"),
                "severity": status,
                "failure_class": failure_class_for_check(check),
                "message": check.get("summary", check.get("name", "")),
            }
        )
    return alerts


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
    alerting = backend_alert_state.evaluate_alerts(
        load_previous_report(args.previous_state),
        current_alerts_from_checks(checks),
        run_at,
        cooldown_seconds=args.alert_cooldown_hours * 60 * 60,
    )
    report["alerting"] = {"summary": alerting["summary"], "notifications": alerting["notifications"], "suppressed": alerting["suppressed"]}
    report["alert_state"] = alerting["state"]

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
                notification_count = report["alerting"]["summary"]["notification_count"]
                if notification_count > 0:
                    subject = f"{subject_prefix} health alert: {notification_count} notification(s)"
                    send_email(config, subject, render_text(report))
                    report["delivery"] = {"status": "sent", "detail": f"sent_to_count={len(config.recipients)} notifications={notification_count}"}
                else:
                    report["delivery"] = {
                        "status": "skipped",
                        "detail": f"no unsuppressed notifications; suppressed={report['alerting']['summary']['suppressed_count']}",
                    }
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
    parser.add_argument("--previous-state", default="")
    parser.add_argument("--alert-cooldown-hours", type=int, default=24)
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
