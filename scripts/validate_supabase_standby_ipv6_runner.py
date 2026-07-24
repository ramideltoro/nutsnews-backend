#!/usr/bin/env python3
"""Validate the Supabase standby IPv6 runner boundary, Ansible, and runbook."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "docs" / "supabase-standby-ipv6-runner-boundary.json"
RUNBOOK = ROOT / "runbooks" / "SUPABASE_STANDBY_IPV6_RUNNER.md"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "supabase_standby_ipv6_runner.yml"
INVENTORY = ROOT / "ansible" / "inventories" / "supabase_standby_ipv6_runner" / "hosts.example.yml"
ROLE = ROOT / "ansible" / "roles" / "supabase_standby_ipv6_runner"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
SERVICE = ROLE / "templates" / "supabase-standby-ipv6-runner.service.j2"
PREFLIGHT = ROLE / "files" / "nutsnews_supabase_standby_ipv6_preflight.py"
WORKFLOW = ROOT / ".github" / "workflows" / "supabase-standby-ipv6-runner.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    errors: list[str] = []

    boundary = json.loads(read(BOUNDARY))
    runbook = read(RUNBOOK)
    playbook = read(PLAYBOOK)
    inventory = read(INVENTORY)
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    service = read(SERVICE)
    preflight = read(PREFLIGHT)
    workflow = read(WORKFLOW)
    backend_checks = read(BACKEND_CHECKS)

    require(boundary.get("decision", "").startswith("Use a disposable"), "Boundary decision must approve disposable VM model.", errors)
    require("65.75.201.18" in json.dumps(boundary), "Boundary must explicitly exclude production backend host.", errors)
    require(boundary.get("runner_model", {}).get("only_label") == "supabase-standby-ipv6", "Boundary must reserve the exact runner label.", errors)
    require(boundary.get("runner_model", {}).get("labels_are_security_boundary") is False, "Boundary must state runner labels are not a security boundary.", errors)
    require(boundary.get("runner_model", {}).get("one_job") is True, "Boundary must require one-job runner behavior.", errors)
    require(boundary.get("vm_profile", {}).get("image") == "Ubuntu 24.04 LTS", "Boundary must record Ubuntu 24.04 LTS VM image.", errors)
    require(boundary.get("vm_profile", {}).get("ipv6") == "enabled at creation", "Boundary must require IPv6 at VM creation.", errors)
    snapshot = boundary.get("github_settings_snapshot_2026_07_24", {})
    require(snapshot.get("allowed_actions") == "selected", "Boundary must record selected-actions enforcement.", errors)
    require(snapshot.get("sha_pinning_required") is True, "Boundary must record repository SHA-pinning enforcement.", errors)
    require(snapshot.get("default_workflow_permissions") == "read", "Boundary must record read-only default workflow token permissions.", errors)
    require(snapshot.get("fork_pr_contributor_approval") == "all_external_contributors", "Boundary must record all-external-contributor approval.", errors)
    require(snapshot.get("main_branch_protection", {}).get("enforce_admins") is True, "Boundary must record main branch protection admin enforcement.", errors)
    require("malicious PR or workflow code" in json.dumps(boundary), "Threat model must cover malicious PR/workflow code.", errors)
    require("secret exposure" in json.dumps(boundary), "Threat model must cover secret exposure.", errors)
    require("runner persistence" in json.dumps(boundary), "Threat model must cover runner persistence.", errors)
    require("network reachability" in json.dumps(boundary), "Threat model must cover network reachability.", errors)
    require("destroy/wipe" in json.dumps(boundary), "Rollback/cleanup must require VM destruction.", errors)

    require("supabase_standby_ipv6_runner:" in inventory, "Inventory must define a separate supabase_standby_ipv6_runner group.", errors)
    require("backend:" not in inventory, "Disposable runner inventory must not include the production backend group.", errors)
    require("hosts: supabase_standby_ipv6_runner" in playbook, "Runner playbook must target only the disposable runner group.", errors)
    require("inventory_hostname not in (groups.get('backend', []))" in playbook, "Runner playbook must refuse backend group hosts.", errors)
    require("name: supabase_standby_ipv6_runner" in playbook, "Runner playbook must include the dedicated role.", errors)

    for required in [
        "supabase_standby_ipv6_runner_label: supabase-standby-ipv6",
        "supabase_standby_ipv6_runner_repo_url: https://github.com/ramideltoro/nutsnews",
        'supabase_standby_ipv6_runner_runner_version: "2.336.0"',
        'supabase_standby_ipv6_runner_runner_sha256: "04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d"',
        "ca-certificates",
        "curl",
        "git",
        "jq",
        "netcat-openbsd",
        "postgresql-client",
        "unattended-upgrades",
        "ufw",
        "fail2ban",
        "docker.io",
        "build-essential",
        "postgresql",
    ]:
        require(required in defaults, f"Role defaults missing {required}.", errors)

    for required in [
        "--ephemeral",
        "--no-default-labels",
        "--labels",
        "{{ supabase_standby_ipv6_runner_label }}",
        "no_log: true",
        "state: absent",
        "groups: \"\"",
        "PasswordAuthentication no",
        "PermitRootLogin no",
        "Set default deny incoming policy",
        "Set default deny outgoing policy",
        "Allow outbound IPv6 TCP 5432 to the redacted Supabase direct DB address",
        "Verify actions/setup-node can populate the runner tool cache without sudo",
    ]:
        require(required in tasks, f"Role tasks missing {required}.", errors)

    for forbidden in [
        "RUNNER_ALLOW_RUNASROOT",
        "passwordless",
        "0.0.0.0",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
    ]:
        require(forbidden not in tasks, f"Role tasks must not contain forbidden fragment {forbidden}.", errors)

    require("NoNewPrivileges=true" in service, "Systemd unit must enforce NoNewPrivileges.", errors)
    require("ProtectSystem=strict" in service, "Systemd unit must protect the host filesystem.", errors)
    require("CapabilityBoundingSet=" in service, "Systemd unit must drop Linux capabilities.", errors)
    require("Restart=no" in service, "Systemd unit must not restart the one-job runner.", errors)
    require("RUNNER_TOOL_CACHE" in service, "Systemd unit must set runner tool cache.", errors)

    require("hostnames_redacted" in preflight, "Preflight probe must report hostname redaction.", errors)
    require("credentials_redacted" in preflight, "Preflight probe must report credential redaction.", errors)
    require("curl" in preflight and "-6" in preflight, "Preflight probe must check outbound IPv6 HTTPS.", errors)
    require("socket.AF_INET6" in preflight, "Preflight probe must require IPv6 DNS resolution.", errors)
    require("nc" in preflight and "5432" in preflight, "Preflight probe must check TCP/5432.", errors)
    require("print(f\"Invalid standby DB URL shape" in preflight, "Preflight probe must reject malformed DB URLs without printing them.", errors)

    for required in [
        "workflow_dispatch:",
        "environment: supabase-standby-runner",
        "permissions:",
        "contents: read",
        "register-one-supabase-standby-ipv6-runner",
        "remove-supabase-standby-ipv6-runner",
        "NUTSNEWS_APP_RUNNER_ADMIN_TOKEN",
        "registration-token",
        "remove-token",
        "::add-mask::",
        "ansible-playbook playbooks/supabase_standby_ipv6_runner.yml --syntax-check",
    ]:
        require(required in workflow, f"Protected workflow missing {required}.", errors)
    require("pull_request:" not in workflow and "push:" not in workflow, "Runner workflow must stay manual-only.", errors)
    require("persist-credentials: false" in workflow, "Runner workflow checkout must not persist credentials.", errors)

    for required in [
        "Do not run a GitHub Actions runner, runner container, or nested runner VM on `65.75.201.18`",
        "Runner labels are routing selectors, not a security boundary.",
        "`supabase-standby-ipv6` appears only in `.github/workflows/supabase-standby-readiness.yml`",
        "preflight used `ubuntu-latest` and readiness used `supabase-standby-ipv6`",
        "lag <= 30 seconds, parity, schema, sequence, writer-pause, and split-brain gates remain separate",
        "destroy/wipe the VM",
        "NUTSNEWS_STANDBY_IPV6_RUNNER_HOSTS_YML",
        "NUTSNEWS_APP_RUNNER_ADMIN_TOKEN",
        "selected-actions enforcement",
        "full-SHA pinning required",
    ]:
        require(required in runbook, f"Runbook missing {required}.", errors)

    leaked_shape = re.compile(r"(postgresql://|postgres://|sb_secret_|sb_publishable_|pgrst_|eyJ|ghp_|github_pat_|-----BEGIN [A-Z ]+PRIVATE KEY-----)")
    for label, text in [
        ("boundary", json.dumps(boundary)),
        ("runbook", runbook),
        ("defaults", defaults),
        ("tasks", tasks),
        ("workflow", workflow),
    ]:
        require(not leaked_shape.search(text), f"{label} appears to contain a secret-shaped value.", errors)

    require(
        "python3 scripts/validate_supabase_standby_ipv6_runner.py" in backend_checks,
        "Backend checks must run the standby IPv6 runner validator.",
        errors,
    )

    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Supabase standby IPv6 runner boundary, automation, and runbook passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
