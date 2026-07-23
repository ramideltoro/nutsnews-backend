#!/usr/bin/env python3
"""Run fixed-purpose backend recovery checks and approved actions.

The workflow intentionally accepts only a closed action list. It does not accept
arbitrary service names, remote commands, shell snippets, Ansible tags, or user
supplied scripts.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CONFIRM_TARGET = "backend.nutsnews.com"
STATE_PATH = "/var/lib/nutsnews/recovery/last-recovery.json"
VALID_STATUSES = {"healthy", "warning", "critical", "not_configured", "unknown"}

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
    "boot_id": "cat /proc/sys/kernel/random/boot_id",
    "kernel": "uname -r",
    "failed_units": "systemctl --failed --no-legend --no-pager || true",
    "root_disk": "df -PB1 / | tail -n +2",
    "root_inodes": "df -Pi / | tail -n +2",
    "reboot_required": "test -e /var/run/reboot-required && echo yes || echo no",
    "service_states": (
        "for unit in ssh ufw fail2ban caddy postgresql alloy "
        "nutsnews-backup.service nutsnews-backup-verify.service nutsnews-restore-drill.service "
        "nutsnews-metrics-textfile.service nutsnews-ops-dashboard-collect.service; do "
        "load=$(systemctl show -p LoadState --value \"$unit\" 2>/dev/null || true); "
        "state=$(systemctl is-active \"$unit\" 2>/dev/null || true); "
        "printf '%s=%s/%s\\n' \"$unit\" \"${load:-unknown}\" \"${state:-unavailable}\"; "
        "done"
    ),
    "timers": (
        "systemctl list-timers --all --no-legend --no-pager 'nutsnews*' 2>/dev/null || true"
    ),
    "backend_health": (
        "if command -v curl >/dev/null 2>&1 && systemctl is-active caddy >/dev/null 2>&1; then "
        "curl -fsS --connect-timeout 5 "
        "--resolve backend.nutsnews.com:443:127.0.0.1 "
        "https://backend.nutsnews.com/healthz 2>/dev/null || true; "
        "else echo unavailable; fi"
    ),
    "caddy_config": (
        "if command -v caddy >/dev/null 2>&1; then "
        "caddy validate --config /etc/caddy/Caddyfile 2>&1 || "
        "sudo -n caddy validate --config /etc/caddy/Caddyfile 2>&1; "
        "else echo not_configured; fi"
    ),
    "alloy_config": (
        "if command -v alloy >/dev/null 2>&1; then "
        "alloy validate /etc/alloy/config.alloy 2>&1 || "
        "sudo -n alloy validate /etc/alloy/config.alloy 2>&1; "
        "else echo not_configured; fi"
    ),
    "backup_runner": (
        "if test -x /usr/local/sbin/nutsnews-backup; then echo present; "
        "else echo not_configured; fi"
    ),
    "backup_status": (
        "if test -x /usr/local/sbin/nutsnews-backup; then "
        "sudo -n /usr/local/sbin/nutsnews-backup status 2>/dev/null || "
        "/usr/local/sbin/nutsnews-backup status 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "metrics_textfile": (
        "stat -c 'present mtime=%Y size=%s path=%n' /var/lib/nutsnews/metrics/nutsnews.prom "
        "2>/dev/null || echo not_configured"
    ),
    "ops_dashboard_snapshot": (
        "stat -c 'present mtime=%Y size=%s path=%n' /var/www/nutsnews-ops-dashboard/status.json "
        "2>/dev/null || echo not_configured"
    ),
    "recovery_status": (
        f"if test -r {STATE_PATH}; then cat {STATE_PATH}; else echo not_configured; fi"
    ),
}


RECOVERY_ACTIONS: dict[str, dict[str, Any]] = {
    "diagnostics": {
        "mutates": False,
        "description": "Collect fixed read-only backend diagnostics.",
        "command": None,
        "post_requires": (),
    },
    "backup-status": {
        "mutates": False,
        "description": "Read the backend backup status report.",
        "command": None,
        "post_requires": (),
    },
    "trigger-backup": {
        "mutates": True,
        "description": "Start the known service-aware backup one-shot.",
        "command": "sudo -n systemctl start nutsnews-backup.service",
        "pre_requires": ("backup_action_surface",),
        "post_requires": ("backup_freshness",),
        "timeout": 1800,
    },
    "trigger-restore-drill": {
        "mutates": True,
        "description": "Start the known lightweight restore-drill one-shot.",
        "command": "sudo -n systemctl start nutsnews-restore-drill.service",
        "pre_requires": ("backup_action_surface",),
        "post_requires": ("backup_restore_drill",),
        "timeout": 1800,
    },
    "reload-caddy": {
        "mutates": True,
        "description": "Validate and reload the known Caddy service.",
        "command": "sudo -n caddy validate --config /etc/caddy/Caddyfile && sudo -n systemctl reload caddy",
        "pre_requires": ("caddy_config",),
        "post_requires": ("service_caddy", "backend_endpoint_health"),
    },
    "restart-caddy": {
        "mutates": True,
        "description": "Validate and restart the known Caddy service.",
        "command": "sudo -n caddy validate --config /etc/caddy/Caddyfile && sudo -n systemctl restart caddy",
        "pre_requires": ("caddy_config",),
        "post_requires": ("service_caddy", "backend_endpoint_health"),
    },
    "restart-alloy": {
        "mutates": True,
        "description": "Validate and restart the known Alloy collector service.",
        "command": "sudo -n alloy validate /etc/alloy/config.alloy && sudo -n systemctl restart alloy",
        "pre_requires": ("alloy_config",),
        "post_requires": ("service_alloy",),
    },
    "restart-fail2ban": {
        "mutates": True,
        "description": "Restart the known fail2ban service.",
        "command": "sudo -n systemctl restart fail2ban",
        "pre_requires": ("service_ssh",),
        "post_requires": ("service_fail2ban",),
    },
    "refresh-metrics": {
        "mutates": True,
        "description": "Start the known NutsNews metrics textfile one-shot.",
        "command": "sudo -n systemctl start nutsnews-metrics-textfile.service",
        "pre_requires": ("metrics_action_surface",),
        "post_requires": ("metrics_textfile",),
    },
    "refresh-ops-dashboard": {
        "mutates": True,
        "description": "Start the known read-only ops dashboard collector one-shot.",
        "command": "sudo -n systemctl start nutsnews-ops-dashboard-collect.service",
        "pre_requires": ("ops_dashboard_action_surface",),
        "post_requires": ("ops_dashboard_snapshot",),
    },
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
        timeout=timeout + 15,
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


def command_result(evidence: dict[str, Any], name: str) -> dict[str, Any]:
    return evidence.get("commands", {}).get(name, {})


def command_stdout(evidence: dict[str, Any], name: str) -> str:
    return str(command_result(evidence, name).get("stdout", ""))


def parse_df_line(text: str) -> dict[str, Any]:
    line = next((item for item in text.splitlines() if item.strip()), "")
    parts = line.split()
    if len(parts) < 6:
        return {"used_percent": None, "raw": redact(line)}
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


def threshold_status(value: int | float | None, warn: int = 80, crit: int = 90) -> str:
    if value is None:
        return "unknown"
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "healthy"


def parse_service_states(text: str) -> dict[str, dict[str, str]]:
    states: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        unit, value = line.split("=", 1)
        load, _, active = value.partition("/")
        states[unit.strip()] = {"load": load.strip() or "unknown", "active": active.strip() or "unavailable"}
    return states


def service_check(unit: str, states: dict[str, dict[str, str]], *, required: bool) -> dict[str, Any]:
    state = states.get(unit, {"load": "unknown", "active": "unavailable"})
    load = state["load"]
    active = state["active"]
    if load not in {"loaded", "masked"}:
        status = "critical" if required else "not_configured"
    elif active == "active":
        status = "healthy"
    elif required:
        status = "critical" if active == "failed" else "warning"
    else:
        status = "not_configured" if active in {"inactive", "unavailable"} else "critical"
    return {"name": f"service_{unit.removesuffix('.service')}", "status": status, "summary": f"{unit}={load}/{active}"}


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped or stripped == "not_configured":
        return {}
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def status_from_backup_section(section: dict[str, Any]) -> str:
    status = str(section.get("freshness_status") or section.get("status") or "not_configured")
    if status == "ok":
        return "healthy"
    if status in VALID_STATUSES:
        return status
    return "unknown"


def classify_config_check(name: str, result: dict[str, Any]) -> dict[str, Any]:
    stdout = str(result.get("stdout", "")).strip()
    if stdout == "not_configured":
        return {"name": name, "status": "not_configured", "summary": f"{name}=not_configured"}
    if result.get("returncode") == 0:
        return {"name": name, "status": "healthy", "summary": f"{name}=valid"}
    return {"name": name, "status": "critical", "summary": f"{name}=invalid_or_unreadable"}


def classify_stat_check(name: str, text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("present "):
        return {"name": name, "status": "healthy", "summary": stripped}
    if stripped == "not_configured" or not stripped:
        return {"name": name, "status": "not_configured", "summary": f"{name}=not_configured"}
    return {"name": name, "status": "unknown", "summary": f"{name}=unknown"}


def classify_recovery_state(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped == "not_configured" or not stripped:
        return {"name": "recovery_last_run", "status": "not_configured", "summary": "recovery_last_run=not_configured"}
    state = parse_json_object(stripped)
    status = str(state.get("status") or "unknown")
    if status not in {"pass", "fail", "blocked", "unknown"}:
        status = "unknown"
    check_status = "healthy" if status == "pass" else "critical" if status == "fail" else "warning" if status == "blocked" else "unknown"
    return {
        "name": "recovery_last_run",
        "status": check_status,
        "summary": f"last_action={state.get('action', 'unknown')} last_mode={state.get('mode', 'unknown')} last_status={status}",
    }


def classify_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    states = parse_service_states(command_stdout(evidence, "service_states"))

    failed_units = [line for line in command_stdout(evidence, "failed_units").splitlines() if line.strip()]
    checks.append(
        {
            "name": "failed_systemd_units",
            "status": "healthy" if not failed_units else "critical",
            "summary": "no failed systemd units" if not failed_units else f"{len(failed_units)} failed systemd unit(s)",
            "details": failed_units[:10],
        }
    )

    disk = parse_df_line(command_stdout(evidence, "root_disk"))
    checks.append(
        {
            "name": "root_disk_pressure",
            "status": threshold_status(disk.get("used_percent")),
            "summary": f"root_disk_used_percent={disk.get('used_percent', 'unknown')}",
        }
    )
    inodes = parse_df_line(command_stdout(evidence, "root_inodes"))
    checks.append(
        {
            "name": "root_inode_pressure",
            "status": threshold_status(inodes.get("used_percent")),
            "summary": f"root_inode_used_percent={inodes.get('used_percent', 'unknown')}",
        }
    )

    for unit, required in (
        ("ssh", True),
        ("ufw", True),
        ("fail2ban", True),
        ("caddy", True),
        ("postgresql", False),
        ("alloy", True),
        ("nutsnews-backup.service", False),
        ("nutsnews-backup-verify.service", False),
        ("nutsnews-restore-drill.service", False),
        ("nutsnews-metrics-textfile.service", False),
        ("nutsnews-ops-dashboard-collect.service", False),
    ):
        checks.append(service_check(unit, states, required=required))

    backend_health = command_stdout(evidence, "backend_health").strip()
    caddy_state = states.get("caddy", {}).get("active", "unavailable")
    if backend_health == "ok":
        endpoint_status = "healthy"
    elif caddy_state == "active":
        endpoint_status = "critical"
    else:
        endpoint_status = "not_configured"
    checks.append({"name": "backend_endpoint_health", "status": endpoint_status, "summary": f"backend_health={backend_health or 'empty'}"})

    checks.append(classify_config_check("caddy_config", command_result(evidence, "caddy_config")))
    checks.append(classify_config_check("alloy_config", command_result(evidence, "alloy_config")))

    backup_runner = command_stdout(evidence, "backup_runner").strip()
    backup_unit = states.get("nutsnews-backup.service", {})
    backup_surface_status = "healthy" if backup_runner == "present" and backup_unit.get("load") == "loaded" else "not_configured"
    checks.append(
        {
            "name": "backup_action_surface",
            "status": backup_surface_status,
            "summary": f"backup_runner={backup_runner or 'unknown'} backup_service={backup_unit.get('load', 'unknown')}/{backup_unit.get('active', 'unknown')}",
        }
    )

    backup_status = parse_json_object(command_stdout(evidence, "backup_status"))
    backup = backup_status.get("backup", {})
    verification = backup_status.get("verification", {})
    restore_drill = backup_status.get("restore_drill", {})
    rabbitmq_recovery = backup_status.get("rabbitmq_recovery", {})
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

    metrics_unit = states.get("nutsnews-metrics-textfile.service", {})
    metrics_surface_status = "healthy" if metrics_unit.get("load") == "loaded" else "not_configured"
    checks.append(
        {
            "name": "metrics_action_surface",
            "status": metrics_surface_status,
            "summary": f"nutsnews-metrics-textfile.service={metrics_unit.get('load', 'unknown')}/{metrics_unit.get('active', 'unknown')}",
        }
    )
    checks.append(classify_stat_check("metrics_textfile", command_stdout(evidence, "metrics_textfile")))

    ops_unit = states.get("nutsnews-ops-dashboard-collect.service", {})
    ops_surface_status = "healthy" if ops_unit.get("load") == "loaded" else "not_configured"
    checks.append(
        {
            "name": "ops_dashboard_action_surface",
            "status": ops_surface_status,
            "summary": f"nutsnews-ops-dashboard-collect.service={ops_unit.get('load', 'unknown')}/{ops_unit.get('active', 'unknown')}",
        }
    )
    checks.append(classify_stat_check("ops_dashboard_snapshot", command_stdout(evidence, "ops_dashboard_snapshot")))
    checks.append(classify_recovery_state(command_stdout(evidence, "recovery_status")))

    reboot_required = command_stdout(evidence, "reboot_required").strip()
    checks.append(
        {
            "name": "reboot_required",
            "status": "warning" if reboot_required == "yes" else "healthy" if reboot_required == "no" else "unknown",
            "summary": f"reboot_required={reboot_required or 'unknown'}",
        }
    )

    for check in checks:
        if check["status"] not in VALID_STATUSES:
            check["status"] = "unknown"
    return checks


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {status: sum(1 for check in checks if check["status"] == status) for status in sorted(VALID_STATUSES)}


def blockers_for_action(action: str, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    by_name = {check["name"]: check for check in checks}

    ssh_check = by_name.get("service_ssh", {})
    if ssh_check.get("status") != "healthy":
        blockers.append({"check": "service_ssh", "status": str(ssh_check.get("status", "missing"))})

    for name in ("root_disk_pressure", "root_inode_pressure"):
        check = by_name.get(name, {})
        if check.get("status") in {"critical", "unknown"}:
            blockers.append({"check": name, "status": str(check.get("status", "missing"))})

    for name in RECOVERY_ACTIONS[action].get("pre_requires", ()):
        check = by_name.get(name, {})
        if check.get("status") != "healthy":
            blockers.append({"check": name, "status": str(check.get("status", "missing"))})
    return blockers


def postcheck_failures(action: str, postcheck: dict[str, Any]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    by_name = {check["name"]: check for check in postcheck.get("checks", [])}
    for name in RECOVERY_ACTIONS[action].get("post_requires", ()):
        check = by_name.get(name, {})
        if check.get("status") != "healthy":
            failures.append({"check": name, "status": str(check.get("status", "missing"))})
    return failures


def build_check(action: str, target: dict[str, str], evidence: dict[str, Any]) -> dict[str, Any]:
    checks = classify_checks(evidence)
    return {
        "checked_at_utc": utc_now(),
        "action": action,
        "target": target,
        "boot_id": command_stdout(evidence, "boot_id").strip(),
        "kernel": command_stdout(evidence, "kernel").strip(),
        "checks": checks,
        "summary": summarize_checks(checks),
        "mutation_blockers": blockers_for_action(action, checks),
    }


def execute_action(action: str, host: str, user: str, key: Path, known_hosts: Path, default_timeout: int) -> dict[str, Any]:
    command = RECOVERY_ACTIONS[action]["command"]
    if command is None:
        return {"returncode": 0, "stdout": "", "stderr": ""}
    timeout = int(RECOVERY_ACTIONS[action].get("timeout", default_timeout))
    return run_ssh_command(host, user, key, known_hosts, command, timeout)


def write_last_run_state(
    host: str,
    user: str,
    key: Path,
    known_hosts: Path,
    timeout: int,
    report: dict[str, Any],
) -> dict[str, Any]:
    state = {
        "schema_version": 1,
        "action": report["action"],
        "mode": report["mode"],
        "status": report["status"],
        "actor": report.get("actor") or "unknown",
        "run_url": report.get("run_url") or "",
        "started_at_utc": report["started_at_utc"],
        "finished_at_utc": report["finished_at_utc"],
        "error": report.get("error"),
    }
    encoded = base64.b64encode(json.dumps(state, indent=2, sort_keys=True).encode("utf-8")).decode("ascii")
    command = (
        "sudo -n install -d -m 0755 /var/lib/nutsnews/recovery && "
        f"printf %s {shlex.quote(encoded)} | base64 -d | sudo -n tee {STATE_PATH} >/dev/null && "
        f"sudo -n chmod 0644 {STATE_PATH}"
    )
    return run_ssh_command(host, user, key, known_hosts, command, timeout)


def write_summary(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Backend Recovery Workflow",
        "",
        f"- Action: `{report['action']}`",
        f"- Mode: `{report['mode']}`",
        f"- Status: `{report['status']}`",
        f"- Target: `{report['target']['user']}@{report['target']['host']}`",
        f"- Started: `{report['started_at_utc']}`",
        f"- Finished: `{report['finished_at_utc']}`",
        f"- Mutating action: `{report['action_definition']['mutates']}`",
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
    if report.get("action_result"):
        lines.extend(
            [
                "",
                "## Action Result",
                "",
                f"- Status: `{report['action_result'].get('status')}`",
                f"- Return code: `{report['action_result'].get('returncode')}`",
            ]
        )
    if report.get("postcheck"):
        lines.extend(["", "## Postchecks", "", "| Check | Status | Summary |", "| --- | --- | --- |"])
        for check in report["postcheck"]["checks"]:
            lines.append(f"| `{check['name']}` | `{check['status']}` | {check['summary']} |")
        if report.get("postcheck_failures"):
            lines.extend(["", "## Postcheck Failures", ""])
            for failure in report["postcheck_failures"]:
                lines.append(f"- `{failure['check']}` is `{failure['status']}`")
    if report.get("last_run_state_write"):
        write_result = report["last_run_state_write"]
        lines.extend(["", "## Last-Run State", "", f"- Write return code: `{write_result.get('returncode')}`"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=tuple(sorted(RECOVERY_ACTIONS)), required=True)
    parser.add_argument("--mode", choices=("check", "apply"), required=True)
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--action-timeout", type=int, default=120)
    parser.add_argument("--postcheck-delay-seconds", type=int, default=3)
    parser.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR", "unknown"))
    parser.add_argument("--run-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    started_at = utc_now()
    target = {"host": args.ssh_host, "user": args.ssh_user}
    action_definition = RECOVERY_ACTIONS[args.action]

    report: dict[str, Any] = {
        "version": 1,
        "action": args.action,
        "mode": args.mode,
        "target": target,
        "actor": args.actor,
        "run_url": args.run_url,
        "action_definition": {
            "description": action_definition["description"],
            "mutates": action_definition["mutates"],
            "fixed_command": action_definition["command"] or "read-only",
            "pre_requires": list(action_definition.get("pre_requires", ())),
            "post_requires": list(action_definition.get("post_requires", ())),
        },
        "started_at_utc": started_at,
        "finished_at_utc": None,
        "status": "unknown",
        "precheck": {},
        "postcheck": None,
        "postcheck_failures": [],
        "action_result": None,
        "last_run_state_write": None,
        "error": None,
        "secret_redaction": "fixed commands only; stdout/stderr redacted before reporting",
    }

    pre_evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    precheck = build_check(args.action, target, pre_evidence)
    report["precheck"] = precheck

    exit_code = 0
    if args.mode == "check":
        report["status"] = "blocked" if precheck["mutation_blockers"] else "pass"
        report["action_result"] = {"status": "not_run", "detail": "check mode does not mutate the backend host"}
        exit_code = 1 if precheck["mutation_blockers"] else 0
    elif action_definition["mutates"] and args.confirm_target != CONFIRM_TARGET:
        report["status"] = "blocked"
        report["action_result"] = {"status": "not_run", "detail": f"confirm-target must be {CONFIRM_TARGET}"}
        report["error"] = report["action_result"]["detail"]
        exit_code = 2
    elif precheck["mutation_blockers"]:
        report["status"] = "blocked"
        report["action_result"] = {"status": "not_run", "detail": "precheck blockers present"}
        report["error"] = report["action_result"]["detail"]
        exit_code = 1
    else:
        result = execute_action(args.action, args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.action_timeout)
        report["action_result"] = {
            "status": "run" if action_definition["mutates"] else "read_only",
            "returncode": result["returncode"],
            "stdout": str(result["stdout"])[-4000:],
            "stderr": str(result["stderr"])[-4000:],
        }
        if args.postcheck_delay_seconds > 0:
            time.sleep(args.postcheck_delay_seconds)
        post_evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
        postcheck = build_check(args.action, target, post_evidence)
        report["postcheck"] = postcheck
        report["postcheck_failures"] = postcheck_failures(args.action, postcheck)
        if result["returncode"] != 0:
            report["status"] = "fail"
            report["error"] = "fixed recovery action returned non-zero"
            exit_code = 1
        elif report["postcheck_failures"]:
            report["status"] = "fail"
            report["error"] = "postcheck failures present"
            exit_code = 1
        else:
            report["status"] = "pass"
            exit_code = 0

    report["finished_at_utc"] = utc_now()
    if args.mode == "apply" and action_definition["mutates"]:
        report["last_run_state_write"] = write_last_run_state(
            args.ssh_host,
            args.ssh_user,
            args.ssh_key,
            args.known_hosts,
            args.timeout,
            report,
        )
        if report["last_run_state_write"].get("returncode") != 0 and report["status"] == "pass":
            report["status"] = "fail"
            report["error"] = "recovery action passed but last-run state write failed"
            exit_code = 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, report)

    print(json.dumps({"status": report["status"], "action": args.action, "mode": args.mode}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
