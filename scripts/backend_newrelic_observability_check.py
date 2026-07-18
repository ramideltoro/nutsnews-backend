#!/usr/bin/env python3
"""Check backend New Relic reporting without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENDPOINTS = {
    "us": "https://api.newrelic.com/graphql",
    "eu": "https://api.eu.newrelic.com/graphql",
    "jp": "https://api.jp.newrelic.com/graphql",
}
LOG_PATHS = (
    Path("/var/log/newrelic/newrelic-daemon.log"),
    Path("/var/log/newrelic-infra/newrelic-infra.log"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(text: str) -> str:
    redacted = text
    for name, value in os.environ.items():
        if any(term in name.upper() for term in ("KEY", "PASSWORD", "TOKEN", "SECRET")) and len(value) >= 8:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def run_command(command: list[str], *, timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", "command_not_found"
    except subprocess.TimeoutExpired:
        return 124, "", "command_timeout"
    return proc.returncode, redact(proc.stdout), redact(proc.stderr)


def check_php_extension() -> dict[str, Any]:
    code, stdout, stderr = run_command(["php", "-m"])
    if code != 0:
        return {"name": "php_extension", "status": "fail", "summary": stderr or "php -m failed"}
    loaded = any(line.strip().lower() == "newrelic" for line in stdout.splitlines())
    return {"name": "php_extension", "status": "pass" if loaded else "fail", "summary": "newrelic extension loaded" if loaded else "newrelic extension missing"}


def check_php_configuration(expected_app_name: str) -> dict[str, Any]:
    code, stdout, stderr = run_command(["php", "-i"])
    if code != 0:
        return {"name": "php_configuration", "status": "fail", "summary": stderr or "php -i failed"}
    lower = stdout.lower()
    app_configured = expected_app_name.lower() in lower
    license_configured = "newrelic.license" in lower and "no value" not in lower
    status = "pass" if app_configured and license_configured else "fail"
    return {
        "name": "php_configuration",
        "status": status,
        "summary": "app name and license setting present" if status == "pass" else "app name or license setting missing",
        "app_name_present": app_configured,
        "license_setting_present": license_configured,
    }


def check_systemd_unit(unit: str) -> dict[str, Any]:
    code, stdout, _stderr = run_command(["systemctl", "is-active", unit])
    active = code == 0 and stdout.strip() == "active"
    return {"name": f"systemd_{unit}", "status": "pass" if active else "fail", "summary": stdout.strip() or "inactive"}


def check_logs() -> dict[str, Any]:
    existing = [path for path in LOG_PATHS if path.exists()]
    if not existing:
        return {"name": "agent_logs", "status": "warn", "summary": "New Relic log files not found"}
    summaries = []
    healthy = False
    for path in existing:
        try:
            tail = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:])
        except OSError:
            summaries.append(f"{path}: unreadable")
            continue
        lowered = tail.lower()
        if any(marker in lowered for marker in ("connect", "report", "harvest", "collector")):
            healthy = True
        summaries.append(f"{path}: present")
    return {"name": "agent_logs", "status": "pass" if healthy else "warn", "summary": "; ".join(summaries)}


def check_nerdgraph() -> dict[str, Any]:
    user_key = os.environ.get("NEW_RELIC_USER_KEY", "").strip()
    account_id = os.environ.get("NEW_RELIC_ACCOUNT_ID", "").strip()
    if not user_key or not account_id:
        return {"name": "nerdgraph_account", "status": "skipped_with_reason", "reason": "missing_new_relic_user_key_or_account_id"}
    region = os.environ.get("NEW_RELIC_REGION", "us").strip().lower() or "us"
    endpoint = ENDPOINTS.get(region)
    if endpoint is None:
        return {"name": "nerdgraph_account", "status": "fail", "summary": "unsupported NEW_RELIC_REGION"}
    query = "query($id: Int!) { actor { account(id: $id) { id name } } }"
    payload = json.dumps({"query": query, "variables": {"id": int(account_id)}}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json", "Api-Key": user_key}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (ValueError, urllib.error.URLError, TimeoutError):
        return {"name": "nerdgraph_account", "status": "fail", "summary": "NerdGraph account query failed"}
    if data.get("errors"):
        return {"name": "nerdgraph_account", "status": "fail", "summary": "NerdGraph returned errors"}
    account = data.get("data", {}).get("actor", {}).get("account") or {}
    return {"name": "nerdgraph_account", "status": "pass" if account.get("id") else "fail", "summary": "account query returned"}


def rollup(checks: list[dict[str, Any]]) -> str:
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if "skipped_with_reason" in statuses:
        return "warn"
    return "pass"


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    app_name = os.environ.get("NEW_RELIC_APP_NAME", "nutsnews-backend-production")
    if args.offline:
        checks = [
            {"name": "php_extension", "status": "skipped_with_reason", "reason": "offline mode"},
            {"name": "php_configuration", "status": "skipped_with_reason", "reason": "offline mode"},
            {"name": "systemd_newrelic-infra", "status": "skipped_with_reason", "reason": "offline mode"},
            {"name": "systemd_newrelic-daemon", "status": "skipped_with_reason", "reason": "offline mode"},
            {"name": "agent_logs", "status": "skipped_with_reason", "reason": "offline mode"},
            {"name": "nerdgraph_account", "status": "skipped_with_reason", "reason": "offline mode"},
        ]
    else:
        checks = [
            check_php_extension(),
            check_php_configuration(app_name),
            check_systemd_unit("newrelic-infra"),
            check_systemd_unit("newrelic-daemon"),
            check_logs(),
            check_nerdgraph(),
        ]
    report = {
        "status": rollup(checks),
        "checked_at_utc": utc_now(),
        "app_name": app_name,
        "checks": checks,
        "safe_metadata_only": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.enforce and report["status"] == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_args())
