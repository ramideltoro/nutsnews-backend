#!/usr/bin/env python3
"""Validate worker-uplift logs and deferred-trace guardrails."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible" / "roles" / "backend_baseline" / "defaults" / "main.yml"
RUNTIME_DEFAULTS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
ALLOY_TEMPLATE = ROOT / "ansible" / "roles" / "backend_baseline" / "templates" / "alloy-config.alloy.j2"
RABBITMQ_COMPOSE = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "rabbitmq-compose.yml.j2"
WORKER_COMPOSE = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "templates" / "worker-uplift-compose.yml.j2"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
LOGS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-uplift-logs-check.yml"
LOGS_CHECK = ROOT / "scripts" / "backend_worker_uplift_logs_check.py"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_LOGS_TRACES.md"

SERVICES = (
    "rabbitmq",
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
WORKER_SERVICES = SERVICES[1:]

FORBIDDEN_STREAM_LABELS = (
    "article",
    "feed",
    "message_id",
    "idempotency",
    "trace_id",
    "span_id",
    "correlation_id",
    "causation_id",
    "payload",
    "url",
    "path",
    "user",
    "ip",
    "token",
    "secret",
    "prompt",
    "model_output",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(label: str, text: str, fragments: tuple[str, ...], errors: list[str]) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} missing required fragment: {fragment}")


def service_block(text: str, list_key: str, item_key: str, service: str) -> str:
    section = text.split(f"{list_key}:\n", 1)[1]
    start_match = re.search(rf"^  - {re.escape(item_key)}: {re.escape(service)}$", section, re.MULTILINE)
    if start_match is None:
        return ""
    end_match = re.search(rf"^  - {re.escape(item_key)}: ", section[start_match.end():], re.MULTILINE)
    end = start_match.end() + end_match.start() if end_match else len(section)
    return section[start_match.start():end]


def block_value(block: str, key: str) -> str:
    match = re.search(rf'^    {re.escape(key)}: "?([^"\n]+)"?$', block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def validate() -> list[str]:
    errors: list[str] = []
    defaults = read(DEFAULTS)
    runtime_defaults = read(RUNTIME_DEFAULTS)
    alloy = read(ALLOY_TEMPLATE)
    rabbitmq_compose = read(RABBITMQ_COMPOSE)
    worker_compose = read(WORKER_COMPOSE)
    protected_apply = read(PROTECTED_APPLY)
    logs_workflow = read(LOGS_WORKFLOW)
    logs_check = read(LOGS_CHECK)
    checks_workflow = read(CHECKS_WORKFLOW)
    runbook = read(RUNBOOK)

    require(
        "backend logs defaults",
        defaults,
        (
            "backend_logs_enabled: false",
            "backend_logs_line_drop_max_size: 8KiB",
            "backend_logs_worker_uplift_traces_enabled: false",
            'backend_logs_worker_uplift_trace_sample_ratio: "0"',
            "backend_logs_worker_uplift_container_sources:",
            "expected_service_version:",
            "expected_revision:",
            "expected_image_digest:",
            "nutsnews-worker-uplift-rabbitmq",
            "nutsnews.worker.fetch.v1",
            "nutsnews.worker.publication.v1",
        ),
        errors,
    )
    if "backend_logs_worker_uplift_contract_version" in defaults:
        errors.append("worker log service_version must not be derived from the telemetry contract version")
    for service in SERVICES:
        if f"service: {service}" not in defaults:
            errors.append(f"backend logs defaults must declare worker-uplift service source: {service}")
        if f"nutsnews-worker-uplift-{service}" not in defaults and service != "rabbitmq":
            errors.append(f"backend logs defaults must declare container tag for service: {service}")

    require(
        "Alloy logs template",
        alloy,
        (
            'loki.write "grafana_cloud_loki"',
            'loki.source.journal "container_',
            "CONTAINER_TAG={{ source.tag }}",
            'source                 = "container"',
            "stage.json",
            "stage.structured_metadata",
            "__journal_com_nutsnews_service_version",
            "__journal_com_nutsnews_revision",
            "__journal_com_nutsnews_image_digest",
            'regex         = "^([0-9]+[.][0-9]+[.][0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$"',
            'revision        = "deployed_revision"',
            'image_digest    = "deployed_image_digest"',
            "correlationId",
            "idempotencyKey",
            "traceparent",
            "drop_counter_reason = \"debug_trace_log_level\"",
            "stage.label_keep",
            '"service_version"',
            '"queue"',
            '"outcome"',
            'values = ["deployment_environment", "service", "service_version", "host", "source", "severity"]',
        ),
        errors,
    )
    label_keep_section = alloy.split("stage.label_keep", 1)[-1].split("}", 1)[0]
    for forbidden in FORBIDDEN_STREAM_LABELS:
        if f'"{forbidden}"' in label_keep_section:
            errors.append(f"forbidden high-cardinality stream label retained: {forbidden}")
    for forbidden in ("loki.source.docker", "/var/run/docker.sock", "otelcol.receiver.otlp", "otelcol.exporter.otlp", "tempo.write"):
        if forbidden in alloy:
            errors.append(f"Alloy logs template contains forbidden fragment while traces are deferred: {forbidden}")

    journal_context = alloy.split('loki.relabel "journal_context" {', 1)[-1].split(
        'local.file_match "backend_logs"', 1
    )[0]
    worker_context = alloy.split('loki.relabel "worker_uplift_container_context" {', 1)[-1].split(
        "{% for source in backend_logs_worker_uplift_container_sources %}", 1
    )[0]
    worker_identity_sources = (
        "__journal_com_nutsnews_service_version",
        "__journal_com_nutsnews_revision",
        "__journal_com_nutsnews_image_digest",
    )
    for source_label in worker_identity_sources:
        if source_label not in worker_context:
            errors.append(f"worker container relabel rules must extract deployed identity: {source_label}")
        if source_label in journal_context:
            errors.append(f"generic journal relabel rules must not apply worker identity: {source_label}")
    worker_sources = alloy.split(
        "{% for source in backend_logs_worker_uplift_container_sources %}", 1
    )[-1].split("{% endfor %}", 1)[0]
    if "relabel_rules = loki.relabel.worker_uplift_container_context.rules" not in worker_sources:
        errors.append("worker container journals must use the worker identity relabel rules")

    require(
        "RabbitMQ Compose",
        rabbitmq_compose,
        (
            "driver: journald",
            'tag: "nutsnews-worker-uplift-rabbitmq"',
            "com.nutsnews.service",
            "com.nutsnews.version",
        ),
        errors,
    )
    require(
        "worker runtime Compose",
        worker_compose,
        (
            "driver: journald",
            'tag: "nutsnews-worker-uplift-{{ service.name }}"',
            "com.nutsnews.service",
            "com.nutsnews.service_version",
            "com.nutsnews.revision",
            "com.nutsnews.image_digest",
            "com.nutsnews.queue",
        ),
        errors,
    )

    for service in WORKER_SERVICES:
        log_block = service_block(
            defaults,
            "backend_logs_worker_uplift_container_sources",
            "service",
            service,
        )
        runtime_block = service_block(
            runtime_defaults,
            "backend_worker_runtime_services",
            "name",
            service,
        )
        expected_identity = (
            block_value(log_block, "expected_service_version"),
            block_value(log_block, "expected_revision"),
            block_value(log_block, "expected_image_digest"),
        )
        runtime_identity = (
            block_value(runtime_block, "service_version"),
            block_value(runtime_block, "build_revision"),
            block_value(runtime_block, "image_digest"),
        )
        if not log_block or not runtime_block or expected_identity != runtime_identity:
            errors.append(
                f"{service} log identity must exactly match worker runtime service version/revision/image digest"
            )
        if block_value(runtime_block, "image_tag") != runtime_identity[1]:
            errors.append(f"{service} build_revision must match the compatibility image_tag")
        if not block_value(runtime_block, "image").endswith(f"@{runtime_identity[2]}"):
            errors.append(f"{service} image_digest must match its immutable image reference")

    require(
        "protected apply",
        protected_apply,
        (
            "GRAFANA_CLOUD_LOKI_URL",
            "GRAFANA_CLOUD_LOKI_USERNAME",
            "GRAFANA_CLOUD_LOKI_PASSWORD",
            '"backend_logs_enabled"] = True',
        ),
        errors,
    )
    require(
        "logs check workflow",
        logs_workflow,
        (
            "Backend Worker-Uplift Logs Check",
            "environment: production-backend",
            "require_loki_data",
            "scripts/backend_worker_uplift_logs_check.py",
            "backend-worker-uplift-logs-check",
        ),
        errors,
    )
    require(
        "logs check script",
        logs_check,
        (
            "safe_metadata_only",
            "loki_rabbitmq_query",
            "loki_worker_service_query",
            "trace_export_deferred",
            "credential_error",
            "CONTAINER_TAG=nutsnews-worker-uplift-rabbitmq",
        ),
        errors,
    )
    require(
        "runbook",
        runbook,
        (
            "#88",
            "structured logs",
            "traces are deferred",
            "Backend Worker-Uplift Logs Check",
            "Grafana Cloud Loki",
            "ramideltoro/nutsnews-infra",
        ),
        errors,
    )
    if "python3 scripts/validate_worker_uplift_logs_traces.py" not in checks_workflow:
        errors.append("Backend Checks must run worker-uplift logs/traces validator")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift logs and deferred traces are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
