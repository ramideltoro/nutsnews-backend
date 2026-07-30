#!/usr/bin/env python3
"""Validate read-only and protected worker-runtime workflow boundaries."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = (
    ROOT / ".github" / "workflows" / "backend-worker-runtime-operations.yml"
)
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"

READ_ONLY_ACTIONS = {
    "check",
    "status",
    "logs",
    "queue-inspect",
    "dlq-inspect",
}
PROTECTED_ACTIONS = {
    "deploy",
    "promote",
    "restart",
    "scale",
    "rollback",
    "dlq-replay",
    "drain",
    "reconciliation",
    "smoke",
}
ALL_ACTIONS = READ_ONLY_ACTIONS | PROTECTED_ACTIONS
REQUIRED_REPOSITORY_SECRETS = {
    "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
    "NUTSNEWS_BACKEND_KNOWN_HOSTS",
}


def extract_job(workflow: str, job_name: str) -> str:
    match = re.search(rf"^  {re.escape(job_name)}:\n", workflow, re.MULTILINE)
    if match is None:
        return ""
    next_job = re.search(r"^  [a-z0-9-]+:\n", workflow[match.end() :], re.MULTILINE)
    end = len(workflow) if next_job is None else match.end() + next_job.start()
    return workflow[match.start() : end]


def actions_from_condition(job: str) -> set[str]:
    match = re.search(r"fromJSON\('(\[[^']+\])'\)", job)
    if match is None:
        return set()
    parsed = json.loads(match.group(1))
    return {str(item) for item in parsed}


def input_actions(workflow: str) -> set[str]:
    match = re.search(
        r"(?ms)^      action:\n.*?^        options:\n(?P<options>(?:^          - [^\n]+\n)+)",
        workflow,
    )
    if match is None:
        return set()
    return {
        line.strip().removeprefix("- ").strip()
        for line in match.group("options").splitlines()
    }


def validate_workflow_text(workflow: str) -> list[str]:
    errors: list[str] = []
    validate_job = extract_job(workflow, "validate-dispatch")
    read_only_job = extract_job(workflow, "read-only-runtime")
    protected_job = extract_job(workflow, "protected-runtime")

    if not validate_job:
        errors.append("missing validate-dispatch job")
    if not read_only_job:
        errors.append("missing read-only-runtime job")
    if not protected_job:
        errors.append("missing protected-runtime job")
    if errors:
        return errors

    if input_actions(workflow) != ALL_ACTIONS:
        errors.append("workflow input actions must exactly match the read-only/protected union")

    read_only_condition = actions_from_condition(read_only_job)
    protected_condition = actions_from_condition(protected_job)
    if read_only_condition != READ_ONLY_ACTIONS:
        errors.append("read-only job condition must contain exactly the five read-only actions")
    if protected_condition != PROTECTED_ACTIONS:
        errors.append("protected job condition must contain exactly the nine protected actions")
    if read_only_condition & protected_condition:
        errors.append("read-only and protected action partitions must be disjoint")
    if read_only_condition | protected_condition != ALL_ACTIONS:
        errors.append("job action partitions must cover every workflow action")

    if "environment:" in validate_job:
        errors.append("validate-dispatch must not reference a GitHub environment")
    if "secrets." in validate_job:
        errors.append("validate-dispatch must not access secrets")
    if "environment:" in read_only_job:
        errors.append("read-only job must not reference a GitHub environment")
    if protected_job.count("environment: production-backend") != 1:
        errors.append("protected job must reference production-backend exactly once")
    if workflow.count("environment: production-backend") != 1:
        errors.append("production-backend environment must appear only on the protected job")

    for job_name, job in (
        ("read-only", read_only_job),
        ("protected", protected_job),
    ):
        if "needs: validate-dispatch" not in job:
            errors.append(f"{job_name} job must depend on validate-dispatch")
        if "timeout-minutes: 20" not in job:
            errors.append(f"{job_name} job must retain the 20-minute timeout")
        if "persist-credentials: false" not in job:
            errors.append(f"{job_name} job must disable checkout credential persistence")
        if "BatchMode=yes" not in job or "StrictHostKeyChecking=yes" not in job:
            errors.append(f"{job_name} job must retain strict SSH options")
        if "backend-worker-runtime-report" not in job:
            errors.append(f"{job_name} job must upload the bounded runtime artifact")
        for secret_name in REQUIRED_REPOSITORY_SECRETS:
            if f"secrets.{secret_name}" not in job:
                errors.append(
                    f"{job_name} job must source repository secret {secret_name}"
                )

    if "--confirm-action" in read_only_job:
        errors.append("read-only job must never pass --confirm-action")
    if "remote_args+=(--dry-run)" not in read_only_job:
        errors.append("read-only job must force --dry-run")
    if "Protected action cannot enter the read-only job." not in read_only_job:
        errors.append("read-only job must fail closed if routed a protected action")

    if "CONFIRM_TARGET: ${{ inputs.confirm_target }}" not in protected_job:
        errors.append("protected job must receive the typed confirmation input")
    if '[[ "$CONFIRM_TARGET" != "backend.nutsnews.com" ]]' not in protected_job:
        errors.append("protected job must validate the exact confirmation target")
    if "--confirm-action" not in protected_job:
        errors.append("protected remote operation must always pass --confirm-action")
    if "Read-only action cannot enter the protected job." not in protected_job:
        errors.append("protected job must fail closed if routed a read-only action")

    validation_fragments = (
        "Read-only actions require dry_run=true.",
        "Read-only actions reject confirm_target.",
        "Protected action requires confirm_target to be exactly backend.nutsnews.com.",
        "replicas is accepted only for scale.",
        "tail must be an integer between 0 and 1000.",
        "queue_kind must be main, retry, or dlq.",
    )
    for fragment in validation_fragments:
        if fragment not in validate_job:
            errors.append(f"dispatch validation missing guardrail: {fragment}")

    if "permissions:\n  contents: read" not in workflow:
        errors.append("workflow permissions must remain contents: read")
    if "group: backend-worker-runtime-operations" not in workflow:
        errors.append("workflow must retain serialized runtime concurrency")
    for forbidden in (
        "remote_command",
        "command_input",
        "script_body",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    ):
        if forbidden in workflow:
            errors.append(f"workflow contains forbidden free-form/sensitive input: {forbidden}")

    return errors


def validate_repository() -> list[str]:
    errors = validate_workflow_text(WORKFLOW_PATH.read_text(encoding="utf-8"))
    checks = CHECKS_WORKFLOW_PATH.read_text(encoding="utf-8")
    command = "python3 scripts/validate_backend_worker_runtime_operations_workflow.py"
    if command not in checks:
        errors.append("Backend Checks must run the worker runtime workflow validator")
    return errors


def main() -> int:
    errors = validate_repository()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend worker runtime workflow action and environment boundaries are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
