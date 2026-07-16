#!/usr/bin/env python3
"""Block common secret-bearing file names from being tracked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BLOCKED_EXACT = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.prod",
    ".env.staging",
    ".env.development",
}

BLOCKED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".tfstate",
    ".tfvars",
    ".sqlite",
    ".db",
    ".dump",
}

SENSITIVE_PARTS = {
    "id_rsa",
    "id_ed25519",
    "private_key",
    "secret",
    "secrets",
    "credential",
    "credentials",
}

ALLOWLIST = {
    ".github/workflows/backend-credential-readiness.yml",
    ".github/workflows/protected-backend-ansible-apply.yml",
    "docs/backend-credential-inventory.json",
    "runbooks/CREDENTIAL_BOOTSTRAP.md",
    "scripts/check_backend_credential_readiness.py",
    "scripts/validate_backend_credential_inventory.py",
    "scripts/validate_no_secret_files.py",
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def is_blocked(path_text: str) -> str | None:
    path = Path(path_text)
    name = path.name.lower()
    lowered = path_text.lower()

    if path_text in ALLOWLIST:
        return None

    if name in BLOCKED_EXACT:
        return "secret environment file"

    if any(lowered.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return "blocked secret or state file suffix"

    if any(part in name for part in SENSITIVE_PARTS):
        return "sensitive file name"

    return None


def main() -> int:
    failures = []
    for path in tracked_files():
        reason = is_blocked(path)
        if reason:
            failures.append(f"{path}: {reason}")

    if failures:
        print("Blocked tracked files:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("No blocked secret-bearing file names are tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
