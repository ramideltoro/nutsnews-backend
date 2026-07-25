#!/usr/bin/env python3
"""Validate the no-cost Supabase standby forced-command probe boundary."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "docs" / "supabase-standby-probe-boundary.json"
RUNBOOK = ROOT / "runbooks" / "SUPABASE_STANDBY_PROBE_BOUNDARY.md"
README = ROOT / "README.md"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
RETIRED_RUNNER_ARTIFACTS = [
    ROOT / ".github" / "workflows" / "supabase-standby-ipv6-runner.yml",
    ROOT / "ansible" / "inventories" / "supabase_standby_ipv6_runner",
    ROOT / "ansible" / "playbooks" / "supabase_standby_ipv6_runner.yml",
    ROOT / "ansible" / "roles" / "supabase_standby_ipv6_runner",
    ROOT / "docs" / "supabase-standby-ipv6-runner-boundary.json",
    ROOT / "runbooks" / "SUPABASE_STANDBY_IPV6_RUNNER.md",
    ROOT / "scripts" / "validate_supabase_standby_ipv6_runner.py",
]


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
    boundary_text = json.dumps(boundary, sort_keys=True)
    runbook = read(RUNBOOK)
    readme = read(README)
    backend_checks = read(BACKEND_CHECKS)

    require("forced-command SSH probe" in boundary.get("decision", ""), "Boundary must approve the forced-command SSH probe.", errors)
    require("ubuntu-latest" in boundary_text, "Boundary must keep GitHub jobs on ubuntu-latest.", errors)
    require(boundary.get("cost_policy", {}).get("additional_recurring_cost") == "zero", "Boundary must require zero additional recurring cost.", errors)

    supersedes = boundary.get("supersedes_design", {})
    for key in [
        "digitalocean_disposable_runner",
        "github_self_hosted_runner",
        "runner_admin_pat",
        "supabase_pooler",
        "new_supabase_project",
    ]:
        require(supersedes.get(key) is True, f"Boundary must explicitly supersede {key}.", errors)

    runtime = boundary.get("runtime_model", {})
    jobs = runtime.get("github_jobs", {})
    require(jobs.get("preflight_runs_on") == "ubuntu-latest", "Preflight must stay on ubuntu-latest.", errors)
    require(jobs.get("readiness_runs_on") == "ubuntu-latest", "Readiness must stay on ubuntu-latest.", errors)
    require(jobs.get("workflow_trigger") == "workflow_dispatch only", "Readiness must stay manual-only.", errors)
    require(jobs.get("branch_ref") == "refs/heads/main only", "Readiness must stay main-only.", errors)
    require(jobs.get("environment") == "supabase-standby", "Readiness must use the protected supabase-standby environment.", errors)

    host = runtime.get("backend_probe_host", {})
    require(host.get("address") == "65.75.201.18", "Boundary must record the existing backend host.", errors)
    require("forced-command SSH probe only" in host.get("role", ""), "Backend host role must be probe-only.", errors)
    for forbidden_role in [
        "GitHub runner registration",
        "arbitrary workflow code execution",
        "interactive operator shell",
        "production backend administration",
    ]:
        require(forbidden_role in host.get("not_authorized_for", []), f"Boundary must forbid {forbidden_role}.", errors)

    identity = runtime.get("probe_identity", {})
    require(identity.get("username") == "nutsnews-standby-probe", "Boundary must declare the dedicated probe user.", errors)
    require(identity.get("password") == "locked", "Probe user password must be locked.", errors)
    require("no sudo" in identity.get("groups", ""), "Probe user must have no sudo/admin access.", errors)
    require("command=/usr/local/libexec/nutsnews-standby-supabase-probe plus restrict" == identity.get("authorized_keys"), "Authorized keys must force the fixed command with restrict.", errors)

    contract = boundary.get("probe_program_contract", {})
    require(contract.get("path") == "/usr/local/libexec/nutsnews-standby-supabase-probe", "Probe command path must be fixed.", errors)
    require(contract.get("owner") == "root", "Probe command must be root-owned.", errors)
    require(contract.get("writable_by_probe_user") is False, "Probe user must not be able to modify the command.", errors)

    expected_rejects = {
        "empty input",
        "multiline input",
        "oversized input",
        "non-PostgreSQL protocol",
        "unexpected Supabase project ref",
        "unexpected host",
        "unexpected port",
        "unexpected database",
        "missing credentials",
        "missing sslmode=require",
        "non-empty SSH_ORIGINAL_COMMAND",
    }
    require(expected_rejects.issubset(set(contract.get("input", {}).get("rejects", []))), "Probe contract must reject every unsafe input shape.", errors)

    target = contract.get("validated_target", {})
    require(target.get("protocols") == ["postgres", "postgresql"], "Probe must accept only postgres/postgresql protocols.", errors)
    require(target.get("host_pattern") == "db.<expected-project-ref>.supabase.co", "Probe host pattern must be direct Supabase DB host.", errors)
    require(target.get("port") == 5432, "Probe port must be 5432.", errors)
    require(target.get("database") == "postgres", "Probe database must be postgres.", errors)
    require(target.get("tls") == "sslmode=require", "Probe must require sslmode=require.", errors)

    execution = contract.get("execution", {})
    for key in [
        "connection_timeout",
        "statement_timeout",
        "hard_timeout",
        "flock_serialization",
        "raw_stdout_stderr_discarded",
    ]:
        require(execution.get(key) is True, f"Probe execution must require {key}.", errors)
    for key in ["caller_supplied_sql", "caller_supplied_executable", "caller_supplied_arguments", "psql_uri_in_argv"]:
        require(execution.get(key) is False, f"Probe execution must forbid {key}.", errors)
    require(contract.get("output", {}).get("success") == "READY", "Probe success token must be READY.", errors)
    require(contract.get("persistence", {}).get("database_url_on_backend") is False, "Probe must not persist the DB URL.", errors)

    secret_contract = boundary.get("secret_and_variable_contract", {})
    for required in [
        "NUTSNEWS_STANDBY_PROBE_SSH_PUBLIC_KEY",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_PROJECT_REF",
        "NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_HOST",
    ]:
        require(required in secret_contract.get("backend_production_environment_inputs", []), f"Backend secret contract missing {required}.", errors)
    for required in [
        "NUTSNEWS_STANDBY_PROBE_SSH_PRIVATE_KEY",
        "NUTSNEWS_STANDBY_PROBE_KNOWN_HOSTS",
    ]:
        require(required in secret_contract.get("app_supabase_standby_environment_secrets", []), f"App secret contract missing {required}.", errors)
    for required in [
        "NUTSNEWS_STANDBY_PROBE_HOST",
        "NUTSNEWS_STANDBY_PROBE_USER",
    ]:
        require(required in secret_contract.get("app_fixed_variables", []), f"App variable contract missing {required}.", errors)

    for required in [
        "Use the existing backend host at `65.75.201.18` only as a restricted",
        "Both app readiness jobs stay on GitHub-hosted",
        "must never run a GitHub self-hosted runner",
        "does not approve failover.",
        "No interactive shell.",
        "No PTY.",
        "No agent forwarding.",
        "No TCP forwarding.",
        "No X11 forwarding.",
        "No user-controlled environment.",
        "No production-file access.",
        "reject any non-empty `SSH_ORIGINAL_COMMAND`",
        "GitHub never sends SQL",
        "without placing the database URI or password in argv",
        "captures and discards raw `psql` output",
        "only:",
        "READY",
        "do not trust a fresh",
        "Do not reintroduce a mandatory non-author PR approval rule unless the owner",
    ]:
        require(required in runbook, f"Runbook missing required boundary text: {required}", errors)

    require("Supabase Standby Forced-Command Probe" in readme, "README must point to the new forced-command probe runbook.", errors)
    require("supersedes the disposable IPv6 runner design" in readme, "README must mark the disposable runner design as superseded.", errors)
    for retired_path in RETIRED_RUNNER_ARTIFACTS:
        require(not retired_path.exists(), f"Retired runner artifact must not exist: {retired_path.relative_to(ROOT)}", errors)
    require(
        "validate_supabase_standby_ipv6_runner.py" not in backend_checks,
        "Backend checks must not run the retired standby IPv6 runner validator.",
        errors,
    )
    require(
        "supabase_standby_ipv6_runner.yml" not in backend_checks,
        "Backend checks must not syntax-check the retired standby IPv6 runner playbook.",
        errors,
    )
    require(
        "python3 scripts/validate_supabase_standby_probe_boundary.py" in backend_checks,
        "Backend checks must run the forced-command probe boundary validator.",
        errors,
    )

    leaked_shape = re.compile(
        r"(postgres(?:ql)?://|sb_secret_|sb_publishable_|pgrst_|eyJ|ghp_|github_pat_|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
    )
    for label, text in [("boundary", boundary_text), ("runbook", runbook), ("readme", readme)]:
        require(not leaked_shape.search(text), f"{label} appears to contain a secret-shaped value.", errors)

    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Supabase standby forced-command probe boundary passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
