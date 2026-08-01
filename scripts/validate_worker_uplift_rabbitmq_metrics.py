#!/usr/bin/env python3
"""Validate backend RabbitMQ metrics collection guardrails."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible" / "roles" / "backend_baseline" / "defaults" / "main.yml"
ALLOY_TEMPLATE = ROOT / "ansible" / "roles" / "backend_baseline" / "templates" / "alloy-config.alloy.j2"
RABBITMQ_PLUGINS = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "enabled_plugins.j2"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
METRICS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-metrics-check.yml"
METRICS_CHECK = ROOT / "scripts" / "backend_rabbitmq_metrics_check.py"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOKS = [
    ROOT / "runbooks" / "MONITORING_BASELINE.md",
    ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md",
    ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_METRICS.md",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(label: str, text: str, fragments: tuple[str, ...], errors: list[str]) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} missing required fragment: {fragment}")


def validate() -> list[str]:
    errors: list[str] = []
    defaults = read(DEFAULTS)
    alloy = read(ALLOY_TEMPLATE)
    plugins = read(RABBITMQ_PLUGINS)
    protected_apply = read(PROTECTED_APPLY)
    metrics_workflow = read(METRICS_WORKFLOW)
    metrics_check = read(METRICS_CHECK)
    checks_workflow = read(CHECKS_WORKFLOW)
    runbooks = "\n".join(read(path) for path in RUNBOOKS if path.exists())

    require(
        "backend metrics defaults",
        defaults,
        (
            "backend_metrics_alloy_self_enabled: true",
            "backend_metrics_rabbitmq_enabled: false",
            "backend_metrics_rabbitmq_queue_regex",
            "queue_coarse_metrics",
            "queue_consumer_count",
            "queue_delivery_metrics",
            "queue_exchange_metrics",
            "backend_metrics_rabbitmq_sample_limit: 1200",
            "backend_metrics_rabbitmq_label_limit: 13",
        ),
        errors,
    )
    require(
        "Alloy metrics template",
        alloy,
        (
            'prometheus.exporter.self "alloy"',
            'prometheus.scrape "rabbitmq_aggregate"',
            'prometheus.scrape "rabbitmq_detailed"',
            'metrics_path    = "/metrics/detailed"',
            '"family" = {{ backend_metrics_rabbitmq_detailed_families | to_json }}',
            '"queue"  = [{{ backend_metrics_rabbitmq_queue_regex | to_json }}]',
            'source_labels = ["queue"]',
            "regex         = {{ backend_metrics_rabbitmq_queue_regex | to_json }}",
            "backend_metrics_rabbitmq_queue_regex",
            "sample_limit",
            "label_limit",
            "cache_ttl",
            "labeldrop",
            "labelkeep",
            "|queue|exchange)$",
            "service_namespace",
            "prometheus.remote_write.grafana_cloud.receiver",
        ),
        errors,
    )
    for forbidden in (
        "article_id",
        "feed_id",
        "message_id",
        "trace_id",
        "span_id",
        "correlation_id",
        "idempotency",
        "loki.write",
    ):
        if forbidden in alloy.split('prometheus.relabel "rabbitmq_detailed"', 1)[-1].split("{% endif %}", 1)[0] and f"|{forbidden}|" not in alloy:
            errors.append(f"RabbitMQ detailed metrics must not retain high-cardinality label: {forbidden}")
    if "rabbitmq_prometheus" not in plugins:
        errors.append("RabbitMQ Prometheus plugin must remain enabled")
    require(
        "protected apply",
        protected_apply,
        (
            "backend_metrics_rabbitmq_enabled",
            "backend_metrics_rabbitmq_vhost",
            "GRAFANA_CLOUD_PROMETHEUS_URL",
        ),
        errors,
    )
    require(
        "RabbitMQ metrics check workflow",
        metrics_workflow,
        (
            "Backend RabbitMQ Metrics Check",
            "environment: production-backend",
            "require_grafana_data",
            "scripts/backend_rabbitmq_metrics_check.py",
            "backend-rabbitmq-metrics-check",
        ),
        errors,
    )
    for forbidden in ("remote_command", "command_input", "script_body", "GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"):
        if forbidden in metrics_workflow:
            errors.append(f"RabbitMQ metrics workflow contains forbidden fragment: {forbidden}")
    require(
        "RabbitMQ metrics check script",
        metrics_check,
        (
            "/metrics/detailed",
            "queue_coarse_metrics",
            "queue_consumer_count",
            "queue_delivery_metrics",
            "queue_exchange_metrics",
            "derive_prometheus_query_url",
            "safe_metadata_only",
            "grafana_rabbitmq_query",
        ),
        errors,
    )
    require(
        "runbooks",
        runbooks,
        (
            "#87",
            "RabbitMQ metrics",
            "Backend RabbitMQ Metrics Check",
            "/metrics/detailed",
            "ramideltoro/nutsnews-infra",
            "Grafana resources",
            "bounded",
        ),
        errors,
    )
    if "python3 scripts/validate_worker_uplift_rabbitmq_metrics.py" not in checks_workflow:
        errors.append("Backend Checks must run RabbitMQ metrics validator")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift RabbitMQ metrics collection is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
