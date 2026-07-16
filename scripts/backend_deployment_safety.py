#!/usr/bin/env python3
"""Run fixed deployment-safety gates for backend-changing workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from scripts import backend_controlled_maintenance as maintenance
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    import backend_controlled_maintenance as maintenance  # type: ignore[no-redef]


REMOTE_SAFETY_COMMANDS: dict[str, str] = {
    "caddy_config": (
        "if command -v caddy >/dev/null 2>&1 && test -f /etc/caddy/Caddyfile; then "
        "(sudo -n caddy validate --config /etc/caddy/Caddyfile 2>&1 || caddy validate --config /etc/caddy/Caddyfile 2>&1); "
        "else echo not_configured; fi"
    ),
    "docker_health": (
        "if systemctl is-active docker >/dev/null 2>&1; then "
        "sudo -n docker info --format '{{.ServerVersion}}' 2>/dev/null || docker info --format '{{.ServerVersion}}' 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "restore_verification": (
        "test -s /var/lib/nutsnews/backups/last-restore-verification.json && echo present || echo not_configured"
    ),
}

PROFILES = {"baseline_apply", "cloudflare_dns_apply", "cloudflare_dns_rollback"}
CRITICAL_IF_NOT_HEALTHY: dict[str, set[str]] = {
    "baseline_apply": {
        "failed_systemd_units",
        "kernel_alignment",
        "root_disk_pressure",
        "root_inode_pressure",
        "service_ssh",
        "service_ufw",
        "service_caddy",
        "reverse_proxy_health",
        "public_endpoint_health",
        "caddy_config",
        "reboot_required",
        "secret_presence",
    },
    "cloudflare_dns_apply": {
        "failed_systemd_units",
        "root_disk_pressure",
        "root_inode_pressure",
        "service_caddy",
        "reverse_proxy_health",
        "public_endpoint_health",
        "caddy_config",
        "secret_presence",
    },
    "cloudflare_dns_rollback": {
        "failed_systemd_units",
        "root_disk_pressure",
        "root_inode_pressure",
        "secret_presence",
    },
}


def bool_arg(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def public_endpoint_health(url: str, expected_body: str, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = maintenance.redact(response.read(256).decode("utf-8", errors="replace")).strip()
            status_code = response.status
    except urllib.error.HTTPError as exc:
        body = maintenance.redact(exc.read(256).decode("utf-8", errors="replace")).strip()
        return {
            "name": "public_endpoint_health",
            "status": "critical",
            "summary": f"http_status={exc.code} body={body or 'empty'}",
        }
    except Exception as exc:
        return {
            "name": "public_endpoint_health",
            "status": "critical",
            "summary": maintenance.redact(str(exc)),
        }

    healthy = status_code == 200 and body == expected_body
    return {
        "name": "public_endpoint_health",
        "status": "healthy" if healthy else "critical",
        "summary": f"http_status={status_code} body={body or 'empty'}",
    }


def collect_evidence(args: argparse.Namespace) -> dict[str, Any]:
    evidence = maintenance.collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    for name, command in REMOTE_SAFETY_COMMANDS.items():
        evidence["commands"][name] = maintenance.run_ssh_command(
            args.ssh_host,
            args.ssh_user,
            args.ssh_key,
            args.known_hosts,
            command,
            args.timeout,
        )
    return evidence


def secret_presence_checks(required_names: list[str]) -> list[dict[str, Any]]:
    missing = [name for name in required_names if not os.environ.get(name, "").strip()]
    return [
        {
            "name": "secret_presence",
            "status": "healthy" if not missing else "critical",
            "summary": "all required secret names present" if not missing else "missing required secret names",
            "checked_names": sorted(required_names),
            "missing_names": sorted(missing),
        }
    ]


def safety_checks(args: argparse.Namespace, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks = maintenance.classify_prechecks(evidence)

    caddy_output = maintenance.command_stdout(evidence, "caddy_config").strip()
    if caddy_output == "not_configured":
        caddy_status = "not_configured"
    elif "Valid configuration" in caddy_output or "configuration is valid" in caddy_output.lower():
        caddy_status = "healthy"
    elif caddy_output:
        caddy_status = "critical"
    else:
        caddy_status = "unknown"
    checks.append({"name": "caddy_config", "status": caddy_status, "summary": caddy_output.splitlines()[-1] if caddy_output else "unknown"})

    docker_output = maintenance.command_stdout(evidence, "docker_health").strip()
    if docker_output == "not_configured":
        docker_status = "not_configured"
    elif docker_output:
        docker_status = "healthy"
    else:
        docker_status = "unknown"
    checks.append({"name": "docker_health", "status": docker_status, "summary": docker_output or "unknown"})

    restore_output = maintenance.command_stdout(evidence, "restore_verification").strip()
    checks.append(
        {
            "name": "restore_verification",
            "status": "healthy" if restore_output == "present" else "not_configured" if restore_output == "not_configured" else "unknown",
            "summary": restore_output or "unknown",
        }
    )

    checks.append(public_endpoint_health(args.public_health_url, args.expected_public_health_body, args.timeout))
    checks.extend(secret_presence_checks(args.required_secret))
    return checks


def blockers(profile: str, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    critical_names = CRITICAL_IF_NOT_HEALTHY[profile]
    result: list[dict[str, str]] = []
    for check in checks:
        name = check["name"]
        status = check["status"]
        if name not in critical_names:
            continue
        if status != "healthy":
            result.append({"check": name, "status": status})
    missing = sorted(critical_names - {check["name"] for check in checks})
    result.extend({"check": name, "status": "missing"} for name in missing)
    return result


def report(args: argparse.Namespace, evidence: dict[str, Any]) -> dict[str, Any]:
    checks = safety_checks(args, evidence)
    gate_blockers = blockers(args.profile, checks)
    enforced = args.enforce
    return {
        "version": 1,
        "generated_at_utc": maintenance.utc_now(),
        "profile": args.profile,
        "phase": args.phase,
        "enforced": enforced,
        "status": "fail" if enforced and gate_blockers else "pass",
        "change_description": args.change_description,
        "target": {"host": args.ssh_host, "user": args.ssh_user},
        "summary": maintenance.summarize_checks(checks),
        "blockers": gate_blockers,
        "checks": checks,
        "secret_handling": "presence and shape by name only; secret values are never emitted",
        "remediation": "use the fixed workflow rollback/recovery path documented for this profile",
    }


def write_summary(path: Path, gate_report: dict[str, Any]) -> None:
    lines = [
        "# Backend Deployment Safety Gate",
        "",
        f"- Profile: `{gate_report['profile']}`",
        f"- Phase: `{gate_report['phase']}`",
        f"- Enforced: `{gate_report['enforced']}`",
        f"- Status: `{gate_report['status']}`",
        f"- Change: `{gate_report['change_description']}`",
        "",
        "## Blockers",
        "",
    ]
    if gate_report["blockers"]:
        for blocker in gate_report["blockers"]:
            lines.append(f"- `{blocker['check']}` is `{blocker['status']}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Checks", "", "| Check | Status | Summary |", "| --- | --- | --- |"])
    for check in gate_report["checks"]:
        lines.append(f"| `{check['name']}` | `{check['status']}` | {check['summary']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--phase", choices=("pre", "post", "dry-run"), required=True)
    parser.add_argument("--enforce", type=bool_arg, default=True)
    parser.add_argument("--change-description", required=True)
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--public-health-url", default="https://backend.nutsnews.com/healthz")
    parser.add_argument("--expected-public-health-body", default="ok")
    parser.add_argument("--required-secret", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    gate_report = report(args, collect_evidence(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(gate_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, gate_report)
    print(json.dumps({"status": gate_report["status"], "blockers": gate_report["blockers"]}, indent=2))
    return 1 if gate_report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
