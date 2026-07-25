#!/usr/bin/env python3
"""Validate the protected Supabase standby probe workflow contract."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "supabase-standby-probe.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK = ROOT / "runbooks" / "SUPABASE_STANDBY_PROBE_BOUNDARY.md"
ANSIBLE_README = ROOT / "ansible" / "README.md"
BOUNDARY = ROOT / "docs" / "supabase-standby-probe-boundary.json"


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
    workflow = read(WORKFLOW)
    backend_checks = read(BACKEND_CHECKS)
    runbook = read(RUNBOOK)
    ansible_readme = read(ANSIBLE_README)
    boundary = read(BOUNDARY)

    for required in [
        "name: Supabase Standby Probe Boundary",
        "workflow_dispatch:",
        "run_mode:",
        "probe_state:",
        "confirm_apply:",
        "default: check",
        "default: present",
        "permissions:\n  contents: read",
        "concurrency:",
        "group: supabase-standby-probe-production-backend",
        "cancel-in-progress: false",
        "runs-on: ubuntu-latest",
        "timeout-minutes: 20",
        "environment: production-backend",
        "uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "persist-credentials: false",
        "refs/heads/main",
        "backend.nutsnews.com",
        "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
        "NUTSNEWS_BACKEND_KNOWN_HOSTS",
        "NUTSNEWS_STANDBY_PROBE_SSH_PUBLIC_KEY",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_PROJECT_REF",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_HOST",
        "ssh-keygen -y -f",
        "ssh-keygen -F \"$NUTSNEWS_BACKEND_HOST\" -f \"$RUNNER_TEMP/backend-ssh/known_hosts\" > /dev/null",
        "-o BatchMode=yes",
        "-o IdentitiesOnly=yes",
        "-o StrictHostKeyChecking=yes",
        "-o UserKnownHostsFile={known_hosts}",
        "-o ClearAllForwardings=yes",
        "-o RequestTTY=no",
        "-o ConnectTimeout=10",
        "-o ConnectionAttempts=2",
        "-o ServerAliveInterval=10",
        "-o ServerAliveCountMax=2",
        "playbooks/supabase_standby_probe.yml",
        "inventories/production/hosts.yml",
        "--private-key \"$RUNNER_TEMP/backend-ssh/nutsnews_backend\"",
        "--extra-vars \"@$RUNNER_TEMP/supabase-standby-probe-extra-vars.json\"",
        "args+=(--check)",
        "ansible-playbook \"${args[@]}\"",
        "no credentials, database URLs, Supabase host/project metadata, PostgreSQL errors, or row data",
    ]:
        require(required in workflow, f"Workflow missing required guardrail: {required}", errors)

    for option in ["- check", "- apply", "- present", "- absent"]:
        require(option in workflow, f"Workflow missing dispatch option: {option}", errors)

    for forbidden in [
        "runs-on: self-hosted",
        "self-hosted",
        "supabase-standby-ipv6",
        "DIGITALOCEAN",
        "RUNNER_TOKEN",
        "actions/checkout@v",
        "--diff",
        "ssh-keyscan",
        "psql ",
        "supabase pooler",
        "pooler.supabase.com",
    ]:
        require(forbidden not in workflow, f"Workflow must not contain obsolete or unsafe marker: {forbidden}", errors)

    for required in [
        "python3 scripts/validate_supabase_standby_probe_workflow.py",
        "Supabase standby forced-command probe workflow",
    ]:
        require(required in backend_checks, f"Backend checks must run workflow validator marker: {required}", errors)

    for required in [
        ".github/workflows/supabase-standby-probe.yml",
        "run_mode",
        "probe_state",
        "NUTSNEWS_STANDBY_PROBE_SSH_PUBLIC_KEY",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_PROJECT_REF",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_HOST",
    ]:
        require(required in runbook, f"Runbook missing workflow contract marker: {required}", errors)

    require(
        ".github/workflows/supabase-standby-probe.yml" in ansible_readme,
        "Ansible README must mention the dedicated probe workflow.",
        errors,
    )
    require(
        ".github/workflows/supabase-standby-probe.yml" in boundary,
        "Boundary manifest must record the dedicated probe workflow.",
        errors,
    )

    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Supabase standby probe protected workflow guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
