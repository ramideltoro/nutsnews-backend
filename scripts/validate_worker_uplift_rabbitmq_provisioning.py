#!/usr/bin/env python3
"""Validate worker-uplift RabbitMQ Ansible/Compose provisioning."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "worker-uplift-rabbitmq-capacity-security-decision.json"
BACKUP_MATRIX_PATH = ROOT / "docs" / "backend-backup-service-matrix.json"
ROLE = ROOT / "ansible" / "roles" / "backend_rabbitmq"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"
COMPOSE_TEMPLATE = ROLE / "templates" / "rabbitmq-compose.yml.j2"
RABBITMQ_CONFIG_TEMPLATE = ROLE / "templates" / "rabbitmq.conf.j2"
PROBE = ROLE / "files" / "nutsnews_rabbitmq_probe.py"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
PROTECTED_WORKFLOW = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
SAFETY = ROOT / "scripts" / "backend_deployment_safety.py"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def main() -> int:
    decision = json.loads(read(DECISION_PATH))
    backup_matrix = json.loads(read(BACKUP_MATRIX_PATH))
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    handlers = read(HANDLERS)
    compose = read(COMPOSE_TEMPLATE)
    rabbitmq_conf = read(RABBITMQ_CONFIG_TEMPLATE)
    probe = read(PROBE)
    playbook = read(PLAYBOOK)
    protected = read(PROTECTED_WORKFLOW)
    checks = read(BACKEND_CHECKS)
    safety = read(SAFETY)
    runbook = read(RUNBOOK)
    errors: list[str] = []

    digest = decision.get("decision", {}).get("image", {}).get("linux_amd64_manifest_digest")
    if not digest or not str(digest).startswith("sha256:"):
        errors.append("capacity decision must record linux/amd64 digest")
    pinned_image = f"rabbitmq@{digest}"
    for label, text in (("defaults", defaults), ("benchmark/protected workflow", protected + checks)):
        if pinned_image not in text:
            errors.append(f"{label} must use approved pinned RabbitMQ image {pinned_image}")

    if "name: backend_rabbitmq" not in playbook or "backend_rabbitmq_enabled | default(false) | bool" not in playbook:
        errors.append("bootstrap playbook must include focused backend_rabbitmq role behind enable flag")

    for path in (DEFAULTS, TASKS, HANDLERS, COMPOSE_TEMPLATE, RABBITMQ_CONFIG_TEMPLATE, PROBE, RUNBOOK):
        if not path.exists():
            errors.append(f"missing RabbitMQ provisioning file: {path}")

    if "backend_rabbitmq_packages:" not in defaults or "docker.io" not in defaults or "docker-compose-v2" not in defaults:
        errors.append("RabbitMQ defaults must install docker.io and docker-compose-v2")
    if "backend_rabbitmq_bind_address: 127.0.0.1" not in defaults:
        errors.append("RabbitMQ defaults must bind host ports to loopback")
    if "backend_rabbitmq_memory_limit: 1g" not in defaults:
        errors.append("RabbitMQ defaults must set 1g memory limit")
    if "backend_rabbitmq_disk_free_limit: 20GB" not in defaults:
        errors.append("RabbitMQ defaults must set 20GB disk free limit")
    if "backend_rabbitmq_nofile: 65536" not in defaults:
        errors.append("RabbitMQ defaults must set nofile to 65536")

    for required in (
        'restart: unless-stopped',
        'env_file:',
        '{{ backend_rabbitmq_bind_address }}',
        '5672',
        '15672',
        '15692',
        'mem_limit:',
        'ulimits:',
        'rabbitmq-diagnostics -q ping',
        '/var/lib/rabbitmq',
    ):
        if required not in compose:
            errors.append(f"Compose template missing required setting: {required}")
    if "0.0.0.0" in compose:
        errors.append("Compose template must not bind RabbitMQ ports to 0.0.0.0")

    for required in (
        "vm_memory_high_watermark.absolute = {{ backend_rabbitmq_memory_high_watermark }}",
        "disk_free_limit.absolute = {{ backend_rabbitmq_disk_free_limit }}",
        "heartbeat = {{ backend_rabbitmq_heartbeat_seconds }}",
        "channel_max = {{ backend_rabbitmq_channel_max }}",
        "default_queue_type = classic",
    ):
        if required not in rabbitmq_conf:
            errors.append(f"RabbitMQ config template missing: {required}")

    if "RABBITMQ_DEFAULT_PASS={{ backend_rabbitmq_admin_password }}" not in tasks:
        errors.append("RabbitMQ environment file must be rendered from protected admin password var")
    env_task = tasks.split("Render RabbitMQ root-only environment", 1)[-1].split("- name:", 1)[0]
    if "no_log: true" not in env_task or 'mode: "0600"' not in env_task:
        errors.append("RabbitMQ environment render must be root-only and no_log")

    forbidden_process_arg_patterns = [
        r"docker\s+exec[^\n]+backend_rabbitmq_admin_password",
        r"curl[^\n]+backend_rabbitmq_admin_password",
        r"rabbitmqctl[^\n]+backend_rabbitmq_admin_password",
    ]
    for pattern in forbidden_process_arg_patterns:
        if re.search(pattern, tasks):
            errors.append(f"RabbitMQ secret could appear in process args: {pattern}")

    for compose_arg in ("docker", "compose", "{{ backend_rabbitmq_compose_path }}", "config"):
        if compose_arg not in tasks:
            errors.append(f"RabbitMQ role must validate Compose config with arg: {compose_arg}")
    if "Validate RabbitMQ Compose definition" not in tasks:
        errors.append("RabbitMQ role must validate Compose config")
    if "Pull pinned RabbitMQ image during protected apply" not in tasks:
        errors.append("RabbitMQ role must pull the pinned image during protected apply")
    if "ExecStartPre=/usr/bin/docker compose" in tasks:
        errors.append("RabbitMQ systemd unit must not require registry access during host restart")
    headroom_task = tasks.split("Capture root filesystem free bytes before RabbitMQ bootstrap", 1)[-1].split("- name:", 1)[0]
    if "check_mode: false" not in headroom_task:
        errors.append("RabbitMQ root filesystem headroom check must run in Ansible check mode")
    if "stdout_lines[-1]" in tasks:
        errors.append("RabbitMQ role must avoid negative-list indexing in Jinja assertions")
    if "backend_rabbitmq_runtime_manageable" not in tasks or "not ansible_check_mode" not in tasks:
        errors.append("RabbitMQ role must support check mode without managing missing services")
    if "Publish RabbitMQ durable restart probe message" not in tasks or "Verify RabbitMQ durable probe message after restart" not in tasks:
        errors.append("RabbitMQ role must run durable restart probe")
    if "Restart RabbitMQ" not in handlers or "when: not ansible_check_mode" not in handlers:
        errors.append("RabbitMQ handlers must be check-mode guarded")

    for required in (
        "NUTSNEWS_BACKEND_RABBITMQ_ENABLED",
        "RABBITMQ_VHOST",
        "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME",
        "RABBITMQ_ERLANG_COOKIE",
        "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
        "backend_rabbitmq_enabled",
        "backend_rabbitmq_admin_password",
        "backend_rabbitmq_erlang_cookie",
    ):
        if required not in protected:
            errors.append(f"protected workflow missing RabbitMQ mapping: {required}")
    for required_secret in ("--required-secret RABBITMQ_ERLANG_COOKIE", "--required-secret RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD"):
        if required_secret not in protected:
            errors.append(f"protected workflow missing deployment safety secret check: {required_secret}")

    if "rabbitmq_health" not in safety or "rabbitmq_post_apply_blockers" not in safety:
        errors.append("deployment safety must classify RabbitMQ post-apply health")

    services = {service.get("id"): service for service in backup_matrix.get("services", [])}
    rabbitmq_backup = services.get("rabbitmq_broker_state")
    if not rabbitmq_backup:
        errors.append("backup matrix must include rabbitmq_broker_state")
    else:
        data_sources = set(rabbitmq_backup.get("data_sources", []))
        if "/var/lib/nutsnews/rabbitmq" not in data_sources:
            errors.append("RabbitMQ backup matrix must include persistent data dir")
        if "/etc/nutsnews-rabbitmq/rabbitmq.env" in data_sources:
            errors.append("RabbitMQ backup matrix must not include secret env file")
        if "PostgreSQL" not in rabbitmq_backup.get("backup_method", "") and "postgresql" not in rabbitmq_backup.get("backup_method", ""):
            errors.append("RabbitMQ backup method must mention PostgreSQL recovery source")

    if "python3 scripts/validate_worker_uplift_rabbitmq_provisioning.py" not in checks:
        errors.append("Backend Checks must run RabbitMQ provisioning validator")
    if "host restart" not in runbook.lower() or "durable probe" not in runbook.lower():
        errors.append("RabbitMQ provisioning runbook must document durable probe and host restart verification")
    if "legacy Cloudflare Worker" not in runbook:
        errors.append("RabbitMQ provisioning runbook must keep legacy Worker guardrail visible")

    if probe.count("RABBITMQ_DEFAULT_PASS") < 1:
        errors.append("RabbitMQ probe must read credentials from env file")
    if "argparse" not in probe or "Authorization" not in probe:
        errors.append("RabbitMQ probe must use local management API without password args")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker-uplift RabbitMQ provisioning is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
