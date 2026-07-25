#!/usr/bin/env python3
"""Static guardrails for the fixed Supabase standby probe command."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "nutsnews_standby_supabase_probe.py"
TESTS = ROOT / "tests" / "test_supabase_standby_probe.py"
BOUNDARY = ROOT / "docs" / "supabase-standby-probe-boundary.json"
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
    probe = read(PROBE)
    tests = read(TESTS)
    boundary = read(BOUNDARY)
    backend_checks = read(BACKEND_CHECKS)

    for required in [
        "CONFIG_PATH = Path(\"/etc/nutsnews-standby-probe/probe.conf\")",
        "LOCK_PATH = Path(\"/run/lock/nutsnews-standby-probe.lock\")",
        "MAX_INPUT_BYTES = 2048",
        "SUCCESS_TOKEN = \"READY\"",
        "FAILURE_TOKEN = \"FAILED\"",
        "PROJECT_REF_PATTERN = re.compile",
        "BEGIN READ ONLY;",
        "SET LOCAL statement_timeout = '5000ms';",
        "current_database() = 'postgres'",
        "pg_is_in_recovery() IN (true, false)",
        "if args:",
        "SSH_ORIGINAL_COMMAND",
        "stdin.read(MAX_INPUT_BYTES + 1)",
        "b\"\\n\" in raw or b\"\\r\" in raw",
        "parsed.scheme not in {\"postgres\", \"postgresql\"}",
        "parsed.hostname is None or parsed.hostname.lower() != expected.host",
        "port != 5432",
        "database != \"postgres\"",
        "query[\"sslmode\"] != [\"require\"]",
        "shutil.which(\"psql\", path=DEFAULT_PATH)",
        "PGPASSWORD",
        "subprocess.run(",
        "stdout=subprocess.PIPE",
        "stderr=subprocess.PIPE",
        "shell=False",
        "timeout=remaining_timeout",
        "fcntl.flock",
    ]:
        require(required in probe, f"Probe command missing guardrail fragment: {required}", errors)

    for forbidden in [
        "logging.",
        "print(database_url",
        "print(parsed",
        "syslog",
        "shell=True",
        "os.system",
        "PAGER",
    ]:
        require(forbidden not in probe, f"Probe command must not contain {forbidden}.", errors)

    for required in [
        "test_valid_probe_returns_ready_and_keeps_secret_out_of_argv",
        "test_rejects_non_empty_original_command",
        "test_rejects_arguments",
        "test_rejects_multiline_and_oversized_input",
        "test_rejects_unsafe_url_shapes",
        "test_psql_failure_returns_generic_failure_without_leaks",
    ]:
        require(required in tests, f"Probe tests missing {required}.", errors)

    require("source_path" in boundary and "scripts/nutsnews_standby_supabase_probe.py" in boundary, "Boundary must record the probe source path.", errors)
    require(
        "python3 scripts/validate_supabase_standby_probe_command.py" in backend_checks,
        "Backend checks must run the probe command validator.",
        errors,
    )
    require(
        "python3 -m unittest tests.test_supabase_standby_probe" in backend_checks,
        "Backend checks must run focused probe unit tests.",
        errors,
    )

    leaked_shape = re.compile(r"(sb_secret_|sb_publishable_|pgrst_|eyJ|ghp_|github_pat_|-----BEGIN [A-Z ]+PRIVATE KEY-----)")
    for label, text in [("probe", probe), ("tests", tests), ("boundary", boundary)]:
        require(not leaked_shape.search(text), f"{label} appears to contain a secret-shaped value.", errors)

    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Supabase standby probe command guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
