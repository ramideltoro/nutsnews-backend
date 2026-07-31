#!/usr/bin/env python3
"""Validate worker-uplift RabbitMQ backup and recovery guardrails."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "backend_rabbitmq"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
RECOVERY_HELPER = ROLE / "files" / "nutsnews_rabbitmq_recovery.py"
BACKUP_RUNNER = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "nutsnews_backup.py"
METRICS = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "nutsnews_metrics_textfile.py"
OPS_COLLECTOR = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "ops_dashboard_collector.py"
HEALTH_REPORT = ROOT / "scripts" / "backend_health_report.py"
RECOVERY_WORKFLOW_HELPER = ROOT / "scripts" / "backend_recovery_workflow.py"
BACKUP_MATRIX = ROOT / "docs" / "backend-backup-service-matrix.json"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
RECOVERY_WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-recovery.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
BOOTSTRAP = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
PROVISIONING_RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md"
RECOVERY_RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_RECOVERY.md"
BACKUP_RUNBOOK = ROOT / "runbooks" / "BACKUP_RESTORE_BASELINE.md"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def main() -> int:
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    helper = read(RECOVERY_HELPER)
    backup_runner = read(BACKUP_RUNNER)
    metrics = read(METRICS)
    ops_collector = read(OPS_COLLECTOR)
    health_report = read(HEALTH_REPORT)
    recovery_workflow_helper = read(RECOVERY_WORKFLOW_HELPER)
    backup_matrix = read(BACKUP_MATRIX)
    backend_checks = read(BACKEND_CHECKS)
    workflow = read(RECOVERY_WORKFLOW)
    protected_apply = read(PROTECTED_APPLY)
    bootstrap = read(BOOTSTRAP)
    provisioning_runbook = read(PROVISIONING_RUNBOOK)
    recovery_runbook = read(RECOVERY_RUNBOOK)
    backup_runbook = read(BACKUP_RUNBOOK)
    errors: list[str] = []

    for required in (
        "backend_rabbitmq_recovery_path: /usr/local/sbin/nutsnews-rabbitmq-recovery",
        "backend_rabbitmq_recovery_state_dir: /var/lib/nutsnews/rabbitmq-recovery",
    ):
        if required not in defaults:
            errors.append(f"RabbitMQ defaults missing recovery setting: {required}")

    for required in (
        "Ensure RabbitMQ recovery state directory exists",
        "Install RabbitMQ recovery helper",
        "Capture RabbitMQ recovery status summary",
        "backend_rabbitmq_recovery_status.stdout",
        "recovery_status",
        "not ansible_check_mode",
    ):
        if required not in tasks:
            errors.append(f"RabbitMQ tasks missing recovery integration: {required}")

    for required in (
        "rabbitmqctl",
        "export_definitions",
        "sanitize_definitions",
        "SENSITIVE_DEFINITION_KEYS",
        "<redacted>",
        "raw_export_retained",
        "definitions.sanitized.json",
        "last-definition-export.json",
        "clean-rebuild-drill",
        "current-candidate-reconciliation-drill",
        "CANDIDATE_CONSUMER_STAGES",
        "wait_for_expected_consumers",
        "consumer_registration_complete",
        "RECONCILIATION_MAX_ITEMS = 1",
        'publication_database_url = publication_env.get("NUTSNEWS_PUBLICATION_DATABASE_URL", "")',
        "side_effect_counts(",
        "persistence_database_url,",
        "publication_database_url,",
        "when jsonb_typeof(diagnostic_metadata->'reconciliationAuditHistory') = 'array'",
        "live_production_broker_unchanged",
        "duplicate_domain_or_api_side_effects",
        "throwaway-loopback-broker",
        "stopped-volume-restore-drill",
        'actions=("check", "permissions", "probe-transfers")',
        "scheduled-check",
        "same_node_name_required",
        "same_erlang_cookie_required",
        "PostgreSQL outbox/reconciliation",
        "normal Restic jobs exclude live /var/lib/nutsnews/rabbitmq",
        "stopped-volume restore requires quiesced broker snapshot",
        "rabbitmq@sha256:c427b73a15d01416346f429042125e663452e2a27e07fb3096fadb08f7033fc7",
        '"--env-file"',
    ):
        if required not in helper:
            errors.append(f"RabbitMQ recovery helper missing required behavior: {required}")
    start_block = helper.split("def start_drill_container", 1)[-1].split("def remove_container", 1)[0]
    for forbidden in ('f"RABBITMQ_DEFAULT_PASS=', 'f"RABBITMQ_ERLANG_COOKIE='):
        if forbidden in start_block:
            errors.append(f"RabbitMQ drill container leaks secret material in docker process args: {forbidden}")
    if 'shutil.copytree(Path("/var/lib/nutsnews/rabbitmq"' in helper or "copytree(args.data" in helper:
        errors.append("RabbitMQ recovery helper must not copy the live production message store")

    for required in (
        "/var/lib/nutsnews/rabbitmq-recovery",
        "live_message_store_excluded",
        "normal_rebuild_from_pinned_image_config_topology_bootstrap_credentials_then_postgresql_outbox_reconciliation",
        "stopped_volume_restore_only_from_quiesced_snapshot",
        "definition_export_freshness_clean_rebuild_drill_stopped_volume_restore_drill",
        "running-node message-store copies can be inconsistent",
    ):
        if required not in backup_matrix:
            errors.append(f"backup matrix missing RabbitMQ recovery policy: {required}")
    for forbidden in ("/etc/nutsnews-rabbitmq/rabbitmq.env", "/etc/nutsnews-rabbitmq/topology.env"):
        if forbidden in backup_matrix:
            errors.append(f"backup matrix must exclude RabbitMQ secret file: {forbidden}")

    for label, text, required_items in (
        (
            "backup runner",
            backup_runner,
            ("RABBITMQ_RECOVERY_STATUS_FILES", "rabbitmq_recovery", "last-definition-export.json"),
        ),
        (
            "metrics textfile",
            metrics,
            (
                "nutsnews_backend_rabbitmq_recovery_stage_healthy",
                "nutsnews_backend_rabbitmq_definition_export_age_seconds",
                "RABBITMQ_RECOVERY_STATE_DIR",
            ),
        ),
        (
            "ops dashboard collector",
            ops_collector,
            ("rabbitmq_recovery", "RABBITMQ_RECOVERY_STATUS_FILES"),
        ),
        (
            "health report",
            health_report,
            (
                "rabbitmq_definition_export",
                "rabbitmq_clean_rebuild_drill",
                "rabbitmq_stopped_volume_restore_drill",
            ),
        ),
        (
            "backend recovery workflow helper",
            recovery_workflow_helper,
            (
                "rabbitmq_definition_export",
                "rabbitmq_clean_rebuild_drill",
                "rabbitmq_stopped_volume_restore_drill",
            ),
        ),
    ):
        for required in required_items:
            if required not in text:
                errors.append(f"{label} missing RabbitMQ recovery signal: {required}")

    for action in (
        "status",
        "export-definitions",
        "clean-rebuild-drill",
        "current-candidate-reconciliation-drill",
        "stopped-volume-restore-drill",
        "scheduled-check",
    ):
        if f"- {action}" not in workflow:
            errors.append(f"RabbitMQ recovery workflow missing fixed action option: {action}")
    for required in (
        "type: choice",
        "confirm_target",
        "backend.nutsnews.com",
        "environment: production-backend",
        "schedule:",
        "sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery '$ACTION'",
        "backend-rabbitmq-recovery-report.json",
        "backend-rabbitmq-recovery-status.json",
        "backend-worker-runtime-post-recovery-status.json",
    ):
        if required not in workflow:
            errors.append(f"RabbitMQ recovery workflow missing guardrail: {required}")
    for forbidden in ("remote_command", "shell_command", "script_body", "ansible_tags", "definitions.raw.json", "definitions.sanitized.json"):
        if forbidden in workflow:
            errors.append(f"RabbitMQ recovery workflow contains forbidden free-form input or unsafe artifact: {forbidden}")

    for label, text, required_items in (
        (
            "protected backend apply workflow",
            protected_apply,
            (
                "deployment_scope:",
                "- full-baseline",
                "- rabbitmq-recovery-helper",
                "args+=(--tags worker_uplift_rabbitmq_recovery_helper)",
                "inputs.deployment_scope == 'full-baseline'",
            ),
        ),
        (
            "backend bootstrap playbook",
            bootstrap,
            ("worker_uplift_rabbitmq_recovery_helper",),
        ),
        (
            "RabbitMQ role tasks",
            tasks,
            (
                "Install RabbitMQ recovery helper",
                "worker_uplift_rabbitmq_recovery_helper",
            ),
        ),
    ):
        for required in required_items:
            if required not in text:
                errors.append(f"{label} missing fixed recovery-helper deployment boundary: {required}")
    ansible_step = protected_apply.split("- name: Run backend Ansible baseline", 1)[-1].split(
        "- name: Run deployment safety postcheck",
        1,
    )[0]
    if "DEPLOYMENT_SCOPE: ${{ inputs.deployment_scope }}" not in ansible_step:
        errors.append("protected backend Ansible step does not receive the fixed deployment scope")

    if "python3 scripts/validate_worker_uplift_rabbitmq_recovery.py" not in backend_checks:
        errors.append("Backend Checks must run RabbitMQ recovery validator")

    for required in (
        "#83",
        "rabbitmqctl export_definitions",
        "password hashes",
        "Do not hot-copy",
        "same node name",
        "same Erlang cookie",
        "PostgreSQL outbox/reconciliation",
        "https://www.rabbitmq.com/docs/backup",
        "https://www.rabbitmq.com/docs/definitions",
        "https://www.rabbitmq.com/docs/upgrade",
        "https://www.rabbitmq.com/docs/rolling-upgrade",
    ):
        if required not in recovery_runbook:
            errors.append(f"RabbitMQ recovery runbook missing required guidance: {required}")
    for required in ("#83", "Backend RabbitMQ Recovery", "live message-store snapshots are excluded"):
        if required not in provisioning_runbook:
            errors.append(f"RabbitMQ provisioning runbook missing recovery cross-reference: {required}")
    for required in ("RabbitMQ", "/var/lib/nutsnews/rabbitmq-recovery", "live message-store"):
        if required not in backup_runbook:
            errors.append(f"backup baseline runbook missing RabbitMQ recovery boundary: {required}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift RabbitMQ recovery is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
