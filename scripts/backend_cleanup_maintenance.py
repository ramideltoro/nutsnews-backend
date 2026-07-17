#!/usr/bin/env python3
"""Run fixed-purpose backend cleanup reporting, dry-run, and approved apply.

The command set is closed and allowlist-based. It never accepts arbitrary remote
commands from workflow inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIRM_TARGET = "backend.nutsnews.com"
VALID_ACTIONS = {"report", "dry-run", "apply"}
TMP_CLEANUP_ROOTS = ("/tmp", "/var/tmp")
STATE_PATH = "/var/lib/nutsnews/cleanup/last-cleanup.json"
PROTECTED_PATHS = {
    "/",
    "/boot",
    "/etc",
    "/home",
    "/opt/nutsnews",
    "/root",
    "/var/lib/caddy",
    "/var/lib/docker/volumes",
    "/var/lib/nutsnews/backups",
    "/var/lib/postgresql",
    "/var/www/nutsnews-ops-dashboard",
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
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


REMOTE_COMMANDS: dict[str, str] = {
    "hostname": "hostname",
    "root_disk": "df -PB1 / | tail -n +2",
    "root_inodes": "df -Pi / | tail -n +2",
    "docker_state": "systemctl is-active docker 2>/dev/null || true",
    "docker_system_df": "docker system df 2>&1 || true",
    "apt_cache_bytes": "du -sB1 /var/cache/apt/archives 2>/dev/null | awk '{print $1}' || echo 0",
    "old_tmp_files": (
        "find /tmp /var/tmp -xdev -type f -mtime +7 "
        "-printf '%s\\n' 2>/dev/null | awk '{count += 1; bytes += $1} END {printf \"count=%d bytes=%d\\n\", count, bytes}'"
    ),
    "cleanup_path_sizes": (
        "for path in /tmp /var/tmp /var/log/nutsnews /var/log/caddy /var/lib/docker "
        "/var/lib/caddy /var/lib/nutsnews/backups /var/cache/apt/archives; do "
        "if [ -e \"$path\" ]; then du -sB1 \"$path\" 2>/dev/null || true; else printf 'missing %s\\n' \"$path\"; fi; "
        "done"
    ),
    "cleanup_status": f"if test -r {STATE_PATH}; then cat {STATE_PATH}; else echo not_configured; fi",
    "sudo_ready": "sudo -n true >/dev/null 2>&1 && echo yes || echo no",
}


@dataclass(frozen=True)
class CleanupCommand:
    name: str
    description: str
    dry_run_command: str
    apply_command: str


CLEANUP_COMMANDS: tuple[CleanupCommand, ...] = (
    CleanupCommand(
        name="stale_temp_files",
        description="Remove files older than 7 days from /tmp and /var/tmp only.",
        dry_run_command=(
            "find /tmp /var/tmp -xdev -type f -mtime +7 "
            "-printf '%p\\t%s\\n' 2>/dev/null | sed -n '1,100p'"
        ),
        apply_command="sudo -n find /tmp /var/tmp -xdev -type f -mtime +7 -delete",
    ),
    CleanupCommand(
        name="apt_package_cache",
        description="Clear downloaded apt package archives.",
        dry_run_command="du -sh /var/cache/apt/archives 2>/dev/null || true",
        apply_command="sudo -n apt-get clean",
    ),
    CleanupCommand(
        name="docker_dangling_images",
        description="Prune dangling Docker images only; named volumes are never pruned.",
        dry_run_command=(
            "if command -v docker >/dev/null 2>&1; then "
            "docker images --filter dangling=true --format '{{.ID}} {{.Repository}} {{.Tag}} {{.Size}}'; "
            "else echo docker_not_installed; fi"
        ),
        apply_command=(
            "if command -v docker >/dev/null 2>&1; then "
            "sudo -n docker image prune -f --filter dangling=true; else echo docker_not_installed; fi"
        ),
    ),
    CleanupCommand(
        name="docker_build_cache",
        description="Prune Docker build cache older than 7 days only.",
        dry_run_command=(
            "if command -v docker >/dev/null 2>&1; then "
            "docker builder du 2>/dev/null || true; else echo docker_not_installed; fi"
        ),
        apply_command=(
            "if command -v docker >/dev/null 2>&1; then "
            "sudo -n docker builder prune -f --filter until=168h; else echo docker_not_installed; fi"
        ),
    ),
)


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


def run_ssh_command(
    host: str,
    user: str,
    key: Path,
    known_hosts: Path,
    command: str,
    timeout: int,
    stdin: str | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        ssh_command(host, user, key, known_hosts, command, timeout),
        check=False,
        input=stdin,
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
    return str(evidence.get("commands", {}).get(name, {}).get("stdout", ""))


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


def parse_key_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for item in text.split():
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        try:
            values[key] = int(raw_value)
        except ValueError:
            values[key] = 0
    return values


def threshold_status(value: int | float | None, warn: int = 80, crit: int = 90) -> str:
    if value is None:
        return "unknown"
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "healthy"


def bytes_status(value: int, warn: int, crit: int) -> str:
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "healthy"


def safe_cleanup_path(path: str) -> bool:
    normalized = "/" + path.strip().strip("/")
    if normalized == "//":
        normalized = "/"
    if normalized in PROTECTED_PATHS:
        return False
    for protected in PROTECTED_PATHS:
        if protected == "/":
            continue
        if normalized.startswith(protected.rstrip("/") + "/"):
            return False
    return (
        normalized in TMP_CLEANUP_ROOTS
        or any(normalized.startswith(root + "/") for root in TMP_CLEANUP_ROOTS)
        or normalized == "/var/cache/apt/archives"
    )


def cleanup_plan() -> list[dict[str, str]]:
    return [
        {
            "name": item.name,
            "description": item.description,
            "dry_run_command": item.dry_run_command,
            "apply_command": item.apply_command,
        }
        for item in CLEANUP_COMMANDS
    ]


def assert_command_safety() -> None:
    joined = "\n".join(item.apply_command for item in CLEANUP_COMMANDS)
    forbidden_fragments = [
        "docker volume prune",
        "docker system prune --volumes",
        "/var/lib/caddy",
        "/var/lib/docker/volumes",
        "/var/lib/nutsnews/backups",
        "/var/lib/postgresql",
        "rm -rf /",
    ]
    for fragment in forbidden_fragments:
        if fragment in joined:
            raise RuntimeError(f"cleanup command contains forbidden fragment: {fragment}")
    for path in TMP_CLEANUP_ROOTS:
        if not safe_cleanup_path(path):
            raise RuntimeError(f"expected safe cleanup path rejected: {path}")
    for path in PROTECTED_PATHS:
        if safe_cleanup_path(path):
            raise RuntimeError(f"protected path is cleanup-allowed: {path}")


def classify(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    checks: list[dict[str, Any]] = []
    root_disk = parse_df_line(command_stdout(evidence, "root_disk"))
    root_inodes = parse_df_line(command_stdout(evidence, "root_inodes"))
    checks.append(
        {
            "name": "root_disk_pressure",
            "status": threshold_status(root_disk.get("used_percent")),
            "summary": f"root_disk_used_percent={root_disk.get('used_percent', 'unknown')}",
        }
    )
    checks.append(
        {
            "name": "root_inode_pressure",
            "status": threshold_status(root_inodes.get("used_percent")),
            "summary": f"root_inode_used_percent={root_inodes.get('used_percent', 'unknown')}",
        }
    )

    docker_state = command_stdout(evidence, "docker_state").strip() or "unavailable"
    docker_status = "healthy" if docker_state == "active" else "not_configured" if docker_state in {"inactive", "unavailable"} else "warning"
    checks.append({"name": "docker_cleanup_surface", "status": docker_status, "summary": f"docker={docker_state}"})

    old_tmp = parse_key_values(command_stdout(evidence, "old_tmp_files"))
    old_tmp_count = old_tmp.get("count", 0)
    old_tmp_bytes = old_tmp.get("bytes", 0)
    checks.append(
        {
            "name": "stale_temp_file_candidates",
            "status": "warning" if old_tmp_count > 0 else "healthy",
            "summary": f"old_tmp_files={old_tmp_count} old_tmp_bytes={old_tmp_bytes}",
        }
    )

    try:
        apt_cache_bytes = int(command_stdout(evidence, "apt_cache_bytes").splitlines()[-1].strip() or "0")
    except (IndexError, ValueError):
        apt_cache_bytes = 0
    checks.append(
        {
            "name": "apt_package_cache_size",
            "status": bytes_status(apt_cache_bytes, warn=512 * 1024 * 1024, crit=1024 * 1024 * 1024),
            "summary": f"apt_cache_bytes={apt_cache_bytes}",
        }
    )

    cleanup_status = command_stdout(evidence, "cleanup_status").strip()
    if cleanup_status == "not_configured" or not cleanup_status:
        checks.append({"name": "cleanup_last_run", "status": "not_configured", "summary": "cleanup_last_run=not_configured"})
    else:
        try:
            state = json.loads(cleanup_status)
            status = str(state.get("status", "unknown"))
            if status not in {"healthy", "warning", "critical", "unknown"}:
                status = "unknown"
            checks.append(
                {
                    "name": "cleanup_last_run",
                    "status": status,
                    "summary": f"last_action={state.get('action', 'unknown')} last_run={state.get('generated_at_utc', 'unknown')}",
                }
            )
        except json.JSONDecodeError:
            checks.append({"name": "cleanup_last_run", "status": "unknown", "summary": "cleanup_last_run_json=invalid"})

    summary = {
        "critical": sum(1 for item in checks if item["status"] == "critical"),
        "warning": sum(1 for item in checks if item["status"] == "warning"),
        "unknown": sum(1 for item in checks if item["status"] == "unknown"),
        "not_configured": sum(1 for item in checks if item["status"] == "not_configured"),
        "healthy": sum(1 for item in checks if item["status"] == "healthy"),
    }
    return checks, summary


def run_cleanup_commands(args: argparse.Namespace, action: str) -> list[dict[str, Any]]:
    results = []
    for item in CLEANUP_COMMANDS:
        command = item.dry_run_command if action == "dry-run" else item.apply_command
        results.append(
            {
                "name": item.name,
                "action": action,
                "description": item.description,
                "result": run_ssh_command(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, command, args.timeout),
            }
        )
    return results


def write_remote_state(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    command = (
        "sudo -n install -d -m 0755 /var/lib/nutsnews/cleanup && "
        f"sudo -n tee {STATE_PATH} >/dev/null"
    )
    return run_ssh_command(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, command, args.timeout, stdin=payload)


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "NutsNews backend cleanup maintenance",
        f"Run: {report['generated_at_utc']}",
        f"Action: {report['action']}",
        f"Target: {report['target']['user']}@{report['target']['host']}",
        "",
        "Summary:",
    ]
    for key in ("critical", "warning", "unknown", "not_configured", "healthy"):
        lines.append(f"- {key}: {report['summary'][key]}")
    lines.extend(["", "Checks:"])
    for check in report["checks"]:
        lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")
    lines.extend(["", "Cleanup plan:"])
    for item in report["cleanup_plan"]:
        lines.append(f"- {item['name']}: {item['description']}")
    if report.get("cleanup_results"):
        lines.extend(["", "Cleanup results:"])
        for item in report["cleanup_results"]:
            result = item["result"]
            lines.append(f"- {item['name']}: rc={result['returncode']}")
    if report.get("state_write"):
        lines.extend(["", f"State write: rc={report['state_write']['returncode']}"])
    return "\n".join(lines) + "\n"


def write_summary(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Backend Cleanup Maintenance",
        "",
        f"- Action: `{report['action']}`",
        f"- Target: `{report['target']['user']}@{report['target']['host']}`",
        f"- Status: `{report['status']}`",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for key in ("critical", "warning", "unknown", "not_configured", "healthy"):
        lines.append(f"| `{key}` | {report['summary'][key]} |")
    lines.extend(["", "## Checks", "", "| Status | Check |", "| --- | --- |"])
    for check in report["checks"]:
        lines.append(f"| `{check['status']}` | {check['summary']} |")
    lines.extend(["", "## Cleanup Plan", "", "| Target | Description |", "| --- | --- |"])
    for item in report["cleanup_plan"]:
        lines.append(f"| `{item['name']}` | {item['description']} |")
    if report.get("cleanup_results"):
        lines.extend(["", "## Cleanup Results", "", "| Target | Return Code |", "| --- | ---: |"])
        for item in report["cleanup_results"]:
            lines.append(f"| `{item['name']}` | `{item['result']['returncode']}` |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    assert_command_safety()
    generated_at = utc_now()
    evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    checks, summary = classify(evidence)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "action": args.action,
        "status": "healthy" if summary["critical"] == 0 else "critical",
        "target": {"host": args.ssh_host, "user": args.ssh_user},
        "checks": checks,
        "summary": summary,
        "cleanup_plan": cleanup_plan(),
        "cleanup_results": [],
        "protected_paths": sorted(PROTECTED_PATHS),
        "ssh": evidence,
    }

    if args.action == "apply":
        if args.confirm_target != CONFIRM_TARGET:
            report["status"] = "critical"
            report["cleanup_results"] = []
            report["error"] = f"apply requires confirm target {CONFIRM_TARGET}"
            return report
        if command_stdout(evidence, "sudo_ready").strip() != "yes":
            report["status"] = "critical"
            report["error"] = "sudo -n is not available for protected cleanup apply"
            return report

    if args.action in {"dry-run", "apply"}:
        report["cleanup_results"] = run_cleanup_commands(args, args.action)
        failed = [item for item in report["cleanup_results"] if item["result"]["returncode"] != 0]
        if failed:
            report["status"] = "critical" if args.action == "apply" else "warning"
            report["error"] = f"{len(failed)} cleanup command(s) returned non-zero"
        if args.action == "apply" and not failed:
            state = {
                "schema_version": 1,
                "generated_at_utc": generated_at,
                "action": args.action,
                "status": report["status"],
                "summary": summary,
            }
            report["state_write"] = write_remote_state(args, state)
            if report["state_write"]["returncode"] != 0:
                report["status"] = "critical"
                report["error"] = "cleanup completed but last-run state write failed"

    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=sorted(VALID_ACTIONS), required=True)
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--ssh-host", default="65.75.201.18")
    parser.add_argument("--ssh-user", default="rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(report, args.summary)
    print(render_text(report))
    return 1 if report["status"] == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
