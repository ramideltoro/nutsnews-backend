#!/usr/bin/env python3
"""Validate the backend abuse-protection decision record."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "backend-abuse-protection-decision.json"
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    decision = load_json(DECISION_PATH)
    baseline = load_json(BASELINE_PATH)
    errors: list[str] = []

    public_ports = {int(entry["port"]) for entry in baseline.get("public_tcp_ports", [])}
    if not public_ports.issubset({22, 80, 443}):
        errors.append(f"service baseline exposes unsupported public ports for the current decision: {sorted(public_ports)}")

    not_deployed = set(baseline.get("not_deployed", []))
    for service in ("backend app", "Docker Engine"):
        if service not in not_deployed:
            errors.append(f"{service} is no longer marked not_deployed; update the abuse-protection decision")

    if decision.get("selected_tool") != "fail2ban":
        errors.append("selected_tool must remain fail2ban until a new reviewed decision replaces it")

    if decision.get("selected_scope") != "ssh-fail2ban-with-http-health-observe-only":
        errors.append("selected_scope must match the SSH fail2ban plus HTTP health observe-only phase")

    if decision.get("crowdsec_status") != "deferred":
        errors.append("crowdsec_status must be deferred unless a CrowdSec implementation PR replaces this record")

    if decision.get("http_enforcement_status") != "deferred_until_backend_app_and_route_logs_exist":
        errors.append("HTTP enforcement must stay deferred until backend app route logs exist")

    enforcement = decision.get("enforcement", {})
    if enforcement.get("protected_apply_required") is not True:
        errors.append("protected apply must be required before live enforcement")
    if enforcement.get("production_blocking_before_approval") is not False:
        errors.append("production blocking before approval must be false")

    ssh_allowlist = set(decision.get("allowlists", {}).get("ssh", []))
    if not {"127.0.0.1/8", "::1"}.issubset(ssh_allowlist):
        errors.append("SSH allowlist must include localhost IPv4 and IPv6")

    routes = set(decision.get("false_positive_sensitive_routes", []))
    for route in ("/health", "/healthz", "/readyz", "auth-provider-route", "admin-redirects"):
        if route not in routes:
            errors.append(f"missing false-positive-sensitive route marker: {route}")

    if decision.get("validation", {}).get("live_verification") != "read-only until a later approved protected apply":
        errors.append("live verification must remain read-only until approved protected apply")

    rollback = decision.get("rollback", {})
    if "fail2ban-client set sshd unbanip" not in rollback.get("ssh_unban", ""):
        errors.append("rollback.ssh_unban must document fail2ban unban")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("abuse protection decision is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
