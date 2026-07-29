#!/usr/bin/env python3
"""Validate immutable action references and safe shell expression handling."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_DIR = ROOT / ".github" / "workflows"
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<reference>\S+)")
PINNED_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.\-/]+@[0-9a-f]{40}$")
PINNED_CONTAINER_RE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
RUN_BLOCK_RE = re.compile(r"^(?P<indent>\s*)(?:-\s*)?run:\s*[|>][-+]?\s*(?:#.*)?$")
UNTRUSTED_SHELL_EXPRESSION_RE = re.compile(
    r"\$\{\{\s*(?:inputs\.|vars\.|github\.event\.|github\.head_ref\b|github\.base_ref\b)"
)


def workflow_files(workflow_dir: Path) -> list[Path]:
    return sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])


def validate_action_reference(reference: str) -> str | None:
    if reference.startswith("./"):
        return None
    if reference.startswith("docker://"):
        if PINNED_CONTAINER_RE.fullmatch(reference):
            return None
        return "container action must use an immutable sha256 digest"
    if PINNED_ACTION_RE.fullmatch(reference):
        return None
    return "external action must use an immutable 40-character commit SHA"


def validate_workflow(path: Path) -> list[str]:
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_run_block = False
    run_indent = -1

    for line_number, line in enumerate(lines, start=1):
        uses_match = USES_RE.match(line)
        if uses_match:
            reference = uses_match.group("reference")
            error = validate_action_reference(reference)
            if error:
                errors.append(f"{path.name}:{line_number}: {error}: {reference}")

        run_match = RUN_BLOCK_RE.match(line)
        if run_match:
            in_run_block = True
            run_indent = len(run_match.group("indent"))
            continue

        if in_run_block:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= run_indent:
                in_run_block = False
            elif UNTRUSTED_SHELL_EXPRESSION_RE.search(line):
                errors.append(
                    f"{path.name}:{line_number}: dispatch, repository, or event data must enter shell through step env"
                )

    return errors


def validate_workflows(workflow_dir: Path) -> list[str]:
    files = workflow_files(workflow_dir)
    if not files:
        return [f"no workflow files found under {workflow_dir}"]
    return [error for path in files for error in validate_workflow(path)]


def main_args(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-dir", type=Path, default=DEFAULT_WORKFLOW_DIR)
    args = parser.parse_args(argv)

    errors = validate_workflows(args.workflow_dir)
    if errors:
        print("GitHub Actions security validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("GitHub Actions use immutable references and safe shell expression indirection.")
    return 0


def main() -> int:
    return main_args(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
