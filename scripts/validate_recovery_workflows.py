#!/usr/bin/env python3
"""Validate fixed-purpose backend recovery workflow guardrails."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from scripts import backend_recovery_workflow as recovery
except ModuleNotFoundError:  # pragma: no cover - script-path execution
    import backend_recovery_workflow as recovery


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-recovery.yml"

FORBIDDEN_WORKFLOW_INPUTS = (
    "remote_command",
    "shell_command",
    "command_input",
    "service_name",
    "unit_name",
    "ansible_tags",
    "script_body",
)
FORBIDDEN_COMMAND_FRAGMENTS = (
    "{command",
    "$INPUT",
    "eval ",
    "bash -c",
    "sh -c",
    "ansible-playbook",
    "docker system prune",
    "docker volume prune",
)


def validate() -> list[str]:
    errors: list[str] = []
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for action in recovery.RECOVERY_ACTIONS:
        if f"- {action}" not in workflow:
            errors.append(f"workflow missing action option: {action}")

    for forbidden in FORBIDDEN_WORKFLOW_INPUTS:
        if forbidden in workflow:
            errors.append(f"workflow contains forbidden free-form input: {forbidden}")

    required_fragments = (
        "type: choice",
        "confirm_target",
        "backend.nutsnews.com",
        "environment: production-backend",
        "scripts/backend_recovery_workflow.py",
    )
    for fragment in required_fragments:
        if fragment not in workflow:
            errors.append(f"workflow missing required guardrail: {fragment}")

    for action, definition in recovery.RECOVERY_ACTIONS.items():
        command_text = definition.get("command") or ""
        for forbidden in FORBIDDEN_COMMAND_FRAGMENTS:
            if forbidden in command_text:
                errors.append(f"{action} command contains forbidden fragment: {forbidden}")
        if definition.get("mutates") and not definition.get("post_requires"):
            errors.append(f"{action} mutates but has no postcheck requirement")

    read_only = {action for action, definition in recovery.RECOVERY_ACTIONS.items() if not definition.get("mutates")}
    if read_only != {"diagnostics", "backup-status"}:
        errors.append(f"unexpected read-only recovery actions: {sorted(read_only)}")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Validated {len(recovery.RECOVERY_ACTIONS)} fixed backend recovery actions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
