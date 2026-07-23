#!/usr/bin/env python3
"""Validate worker-uplift RabbitMQ protected operations guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_probe.py"
TASKS = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "tasks" / "main.yml"
DEFAULTS = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "defaults" / "main.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-smoke.yml"
DRIFT_WORKFLOW = ROOT / ".github" / "workflows" / "backend-drift-check.yml"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
DRIFT_SCRIPT = ROOT / "scripts" / "backend_drift_check.py"
SAFETY_SCRIPT = ROOT / "scripts" / "backend_deployment_safety.py"
HEALTH_REPORT = ROOT / "scripts" / "backend_health_report.py"
CREDENTIAL_INVENTORY = ROOT / "docs" / "backend-credential-inventory.json"
SERVICE_BASELINE = ROOT / "docs" / "backend-service-baseline.json"
PROVISIONING_RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md"
DEPLOYMENT_RUNBOOK = ROOT / "runbooks" / "DEPLOYMENT_SAFETY_GATES.md"
DRIFT_RUNBOOK = ROOT / "runbooks" / "DRIFT_CHECK.md"
HEALTH_RUNBOOK = ROOT / "runbooks" / "BACKEND_HEALTH_REPORT.md"


RABBITMQ_SECRET_NAMES = {
    "RABBITMQ_ERLANG_COOKIE",
    "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
    "RABBITMQ_MONITORING_PASSWORD",
    "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD",
    "RABBITMQ_FETCHER_CONSUMER_PASSWORD",
    "RABBITMQ_FETCHER_PUBLISHER_PASSWORD",
    "RABBITMQ_CANONICALIZER_CONSUMER_PASSWORD",
    "RABBITMQ_CANONICALIZER_PUBLISHER_PASSWORD",
    "RABBITMQ_ENRICHMENT_CONSUMER_PASSWORD",
    "RABBITMQ_ENRICHMENT_PUBLISHER_PASSWORD",
    "RABBITMQ_APPROVAL_CONSUMER_PASSWORD",
    "RABBITMQ_APPROVAL_PUBLISHER_PASSWORD",
    "RABBITMQ_TRANSLATION_CONSUMER_PASSWORD",
    "RABBITMQ_TRANSLATION_PUBLISHER_PASSWORD",
    "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD",
    "RABBITMQ_PERSISTENCE_PUBLISHER_PASSWORD",
    "RABBITMQ_PUBLICATION_CONSUMER_PASSWORD",
}


def require_fragments(label: str, text: str, fragments: tuple[str, ...], errors: list[str]) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} missing required fragment: {fragment}")


def validate() -> list[str]:
    errors: list[str] = []
    probe = PROBE.read_text(encoding="utf-8")
    tasks = TASKS.read_text(encoding="utf-8")
    defaults = DEFAULTS.read_text(encoding="utf-8")
    protected_apply = PROTECTED_APPLY.read_text(encoding="utf-8")
    smoke_workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
    drift_workflow = DRIFT_WORKFLOW.read_text(encoding="utf-8")
    checks_workflow = CHECKS_WORKFLOW.read_text(encoding="utf-8")
    drift_script = DRIFT_SCRIPT.read_text(encoding="utf-8")
    safety_script = SAFETY_SCRIPT.read_text(encoding="utf-8")
    health_report = HEALTH_REPORT.read_text(encoding="utf-8")

    require_fragments(
        "RabbitMQ probe helper",
        probe,
        (
            '"smoke"',
            '"drift"',
            "worker.uplift.probe.smoke.",
            "publish_confirm",
            "consume_manual_ack",
            "retry",
            "dlq",
            "restart_persistence",
            "permission_denial",
            "rabbitmq_image_digest",
            '["systemctl", "restart", args.restart_service]',
            "ignored_statuses=(401, 403)",
            "secret_redaction",
        ),
        errors,
    )
    for forbidden in ("remote_command", "shell_command", "eval(", "subprocess.run(command_text"):
        if forbidden in probe:
            errors.append(f"RabbitMQ probe helper contains forbidden free-form pattern: {forbidden}")

    require_fragments(
        "RabbitMQ role defaults",
        defaults,
        (
            "backend_rabbitmq_apply_metadata_path",
            "backend_rabbitmq_smoke_report_path",
        ),
        errors,
    )
    require_fragments(
        "RabbitMQ role tasks",
        tasks,
        (
            "Write RabbitMQ protected apply metadata",
            "Capture RabbitMQ read-only drift summary",
            "drift_summary",
            "git_revert_then_protected_check_then_protected_apply",
            "do_not_delete_data_dir_without_review",
            "backend_rabbitmq_smoke_report_path",
        ),
        errors,
    )

    require_fragments(
        "Protected backend apply workflow",
        protected_apply,
        (
            "Check RabbitMQ credential readiness",
            "check_backend_credential_readiness.py --group rabbitmq --json",
            "backend-rabbitmq-credential-readiness.json",
            "confirm_apply",
            "backend.nutsnews.com",
            "environment: production-backend",
        ),
        errors,
    )
    for secret_name in RABBITMQ_SECRET_NAMES:
        if secret_name not in protected_apply:
            errors.append(f"protected apply workflow missing RabbitMQ readiness secret mapping: {secret_name}")

    require_fragments(
        "RabbitMQ smoke workflow",
        smoke_workflow,
        (
            "type: choice",
            "- status",
            "- smoke",
            "confirm_target",
            "backend.nutsnews.com",
            "environment: production-backend",
            "/usr/local/sbin/nutsnews-rabbitmq-probe smoke",
            "backend-rabbitmq-smoke-report.json",
            "backend-rabbitmq-smoke-report",
        ),
        errors,
    )
    for forbidden in ("remote_command", "shell_command", "command_input", "service_name", "ansible_tags", "script_body"):
        if forbidden in smoke_workflow:
            errors.append(f"RabbitMQ smoke workflow contains forbidden free-form input/pattern: {forbidden}")

    require_fragments(
        "Backend drift workflow/script",
        drift_workflow + drift_script,
        (
            "rabbitmq_drift",
            "nutsnews-rabbitmq-probe drift",
            "/var/lib/nutsnews/rabbitmq-probes/apply-metadata.json",
            "RabbitMQ drift check present after broker provisioning",
        ),
        errors,
    )
    require_fragments(
        "Deployment safety script",
        safety_script,
        (
            "rabbitmq_drift",
            "nutsnews-rabbitmq-probe drift",
            '"rabbitmq_drift"',
            "high_priority_unexpected=none",
        ),
        errors,
    )
    require_fragments(
        "Backend health report",
        health_report,
        (
            "rabbitmq_drift",
            "rabbitmq_smoke_status",
            "rabbitmq_smoke_last_run",
            "nutsnews-rabbitmq-probe drift",
        ),
        errors,
    )

    inventory = json.loads(CREDENTIAL_INVENTORY.read_text(encoding="utf-8"))
    rabbitmq_group = next((group for group in inventory.get("secret_groups", []) if group.get("id") == "rabbitmq"), None)
    if not rabbitmq_group:
        errors.append("credential inventory missing rabbitmq group")
    else:
        inventory_names = {secret.get("name") for secret in rabbitmq_group.get("secrets", [])}
        missing = sorted(RABBITMQ_SECRET_NAMES - inventory_names)
        if missing:
            errors.append(f"credential inventory rabbitmq group missing names: {', '.join(missing)}")

    baseline = json.loads(SERVICE_BASELINE.read_text(encoding="utf-8"))
    not_deployed = set(baseline.get("not_deployed", []))
    for forbidden in ("Docker Engine", "Docker Compose", "RabbitMQ broker"):
        if forbidden in not_deployed:
            errors.append(f"service baseline still marks provisioned RabbitMQ dependency not_deployed: {forbidden}")
    private_ports = {int(item.get("port", 0)) for item in baseline.get("private_listeners", [])}
    for port in (5672, 15672, 15692):
        if port not in private_ports:
            errors.append(f"service baseline missing RabbitMQ private listener port: {port}")

    require_fragments(
        "runbooks",
        "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PROVISIONING_RUNBOOK, DEPLOYMENT_RUNBOOK, DRIFT_RUNBOOK, HEALTH_RUNBOOK)
        ),
        (
            "#84",
            "Backend RabbitMQ Smoke",
            "rabbitmq_drift",
            "last-smoke.json",
            "apply-metadata.json",
        ),
        errors,
    )

    if "python3 scripts/validate_worker_uplift_rabbitmq_operations.py" not in checks_workflow:
        errors.append("Backend Checks must run RabbitMQ operations validator")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift RabbitMQ protected operations are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
