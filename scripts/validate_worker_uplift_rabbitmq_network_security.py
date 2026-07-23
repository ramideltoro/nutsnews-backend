#!/usr/bin/env python3
"""Validate worker-uplift RabbitMQ network security guardrails."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "backend_rabbitmq"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
COMPOSE_TEMPLATE = ROLE / "templates" / "rabbitmq-compose.yml.j2"
NETWORK_CHECK = ROLE / "files" / "nutsnews_rabbitmq_network_check.py"
SAFETY = ROOT / "scripts" / "backend_deployment_safety.py"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
PROTECTED_WORKFLOW = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
FIREWALL_RUNBOOK = ROOT / "runbooks" / "FIREWALL_BASELINE.md"
RABBITMQ_RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def main() -> int:
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    compose = read(COMPOSE_TEMPLATE)
    network_check = read(NETWORK_CHECK)
    safety = read(SAFETY)
    backend_checks = read(BACKEND_CHECKS)
    protected = read(PROTECTED_WORKFLOW)
    firewall_runbook = read(FIREWALL_RUNBOOK)
    rabbitmq_runbook = read(RABBITMQ_RUNBOOK)
    errors: list[str] = []

    for required in (
        "backend_rabbitmq_bind_address: 127.0.0.1",
        "backend_rabbitmq_network_check_path: /usr/local/sbin/nutsnews-rabbitmq-network-check",
        'backend_rabbitmq_amqp_port: "5672"',
        'backend_rabbitmq_management_port: "15672"',
        'backend_rabbitmq_prometheus_port: "15692"',
    ):
        if required not in defaults:
            errors.append(f"RabbitMQ defaults missing network guardrail: {required}")

    for required in (
        '"{{ backend_rabbitmq_bind_address }}:{{ backend_rabbitmq_amqp_port }}:5672"',
        '"{{ backend_rabbitmq_bind_address }}:{{ backend_rabbitmq_management_port }}:15672"',
        '"{{ backend_rabbitmq_bind_address }}:{{ backend_rabbitmq_prometheus_port }}:15692"',
    ):
        if required not in compose:
            errors.append(f"Compose template missing loopback-published port: {required}")
    if "0.0.0.0" in compose:
        errors.append("Compose template must not publish RabbitMQ ports on 0.0.0.0")

    for required in (
        "Install RabbitMQ network security check",
        "Verify RabbitMQ network security posture",
        "backend_rabbitmq_network_check_path",
        "backend_rabbitmq_network_security.stdout",
        "--topology-env",
        "not ansible_check_mode",
    ):
        if required not in tasks:
            errors.append(f"RabbitMQ tasks missing network verification step: {required}")

    for required in (
        "parse_ss_listeners",
        "check_ufw",
        "check_docker_publish",
        "check_docker_networks",
        "check_loopback_tcp",
        "check_prometheus",
        "check_anonymous_management",
        "check_guest_user",
        "check_topology_credentials",
        "check_tls_posture",
        "secret values are never emitted",
    ):
        if required not in network_check:
            errors.append(f"RabbitMQ network checker missing required behavior: {required}")
    for forbidden in ("RABBITMQ_DEFAULT_PASS}", "print(password", "0.0.0.0:5672"):
        if forbidden in network_check:
            errors.append(f"RabbitMQ network checker contains forbidden secret/public-bind pattern: {forbidden}")

    for required in (
        "rabbitmq_network_security",
        "rabbitmq_public_exposure",
        "RABBITMQ_PUBLIC_PORTS = (5672, 15672, 15692)",
        '"docker_health", "rabbitmq_health", "rabbitmq_network_security", "rabbitmq_drift", "rabbitmq_public_exposure"',
    ):
        if required not in safety:
            errors.append(f"deployment safety missing RabbitMQ network blocker: {required}")

    if "python3 scripts/backend_deployment_safety.py" not in protected:
        errors.append("protected workflow must run deployment safety")
    if "NUTSNEWS_BACKEND_RABBITMQ_ENABLED" not in protected:
        errors.append("protected workflow must expose the RabbitMQ enabled gate to deployment safety")
    if "python3 scripts/validate_worker_uplift_rabbitmq_network_security.py" not in backend_checks:
        errors.append("Backend Checks must run RabbitMQ network security validator")

    for required in ("5672", "15672", "15692", "must remain private", "public scan"):
        if required not in firewall_runbook:
            errors.append(f"firewall runbook missing RabbitMQ network guidance: {required}")
    for required in ("#82", "SSH tunnel", "emergency revocation", "TLS", "anonymous", "guest"):
        if required not in rabbitmq_runbook:
            errors.append(f"RabbitMQ runbook missing #82 management/security guidance: {required}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift RabbitMQ network security is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
