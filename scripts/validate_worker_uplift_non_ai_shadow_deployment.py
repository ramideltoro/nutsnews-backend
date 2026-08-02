#!/usr/bin/env python3
"""Validate non-AI worker-uplift shadow service deployment guardrails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "files" / "nutsnews_worker_runtime.py"
DEFAULTS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
TASKS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "tasks" / "main.yml"
COMPOSE_TEMPLATE = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "templates" / "worker-uplift-compose.yml.j2"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
RABBITMQ_TOPOLOGY = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "worker-uplift-topology.json.j2"
RABBITMQ_MANAGER = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_topology.py"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_SERVICE_RUNTIME.md"


SERVICES = [
    {
        "name": "scheduler",
        "stage": "scheduler",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-feed-scheduler",
        "source": "ramideltoro/nutsnews-worker-feed-scheduler",
        "digest": "sha256:03f52c161f6eb7ea9ac38aaed22f3bf09df6f86192eb05aa108c6bde5420ab07",
        "tag": "e92d2f93c93fa5ceb507a2e6c9a62f91a46fcc07",
        "runtime": "0.4.0",
        "contract": "0.3.1",
        "port": "18081",
        "secrets": ("scheduler-database-url", "scheduler-rabbitmq-url", "scheduler-backend-api-token", "reconciliation-token"),
        "schema": "worker_uplift_scheduler",
        "main_queue": "nutsnews.worker.fetch.v1",
        "publishes": ("nutsnews.worker.fetch.v1",),
    },
    {
        "name": "fetcher",
        "stage": "fetcher",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-feed-fetcher",
        "source": "ramideltoro/nutsnews-worker-feed-fetcher",
        "digest": "sha256:13781f7143820b8b827ddd3c441574595e4e357693f86d160e6cd7123b17270d",
        "tag": "905e6af5989bb4eb1eba772cde728f86026eb229",
        "runtime": "0.5.0",
        "contract": "0.3.1",
        "port": "18082",
        "secrets": ("fetcher-database-url", "fetcher-rabbitmq-url", "reconciliation-token"),
        "schema": "worker_uplift_fetcher",
        "main_queue": "nutsnews.worker.fetch.v1",
        "publishes": ("nutsnews.worker.canonicalization.v1",),
    },
    {
        "name": "canonicalizer",
        "stage": "canonicalizer",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-canonicalizer",
        "source": "ramideltoro/nutsnews-worker-article-canonicalizer",
        "digest": "sha256:9fadf70c9349bd7744a06c56ffa3863eab7846b319d45a27c9f829a20e379e4c",
        "tag": "d6bfe689c7aa5f625bd1204e73fef908ec528366",
        "runtime": "0.5.0",
        "contract": "0.4.0",
        "port": "18083",
        "secrets": ("canonicalizer-database-url", "canonicalizer-rabbitmq-url", "reconciliation-token"),
        "schema": "worker_uplift_canonicalizer",
        "main_queue": "nutsnews.worker.canonicalization.v1",
        "publishes": ("nutsnews.worker.enrichment.v1",),
    },
    {
        "name": "enrichment",
        "stage": "enrichment",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-enrichment",
        "source": "ramideltoro/nutsnews-worker-article-enrichment",
        "digest": "sha256:1ba9e52f01ac49013d3c115aa156132a594c865728be5a4f69474c7c04d45ce6",
        "tag": "2534fa7594ab9a1ad85dad2c15fda6b8a4cb0449",
        "runtime": "0.5.0",
        "contract": "0.4.0",
        "port": "18084",
        "secrets": ("enrichment-database-url", "enrichment-rabbitmq-url", "reconciliation-token"),
        "schema": "worker_uplift_enrichment",
        "main_queue": "nutsnews.worker.enrichment.v1",
        "publishes": ("nutsnews.worker.approval.v1",),
    },
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(label: str, text: str, fragments: tuple[str, ...], errors: list[str]) -> None:
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{label} missing required fragment: {fragment}")


def load_manager():
    spec = importlib.util.spec_from_file_location("nutsnews_worker_runtime", MANAGER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load worker runtime manager")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest_for_manager() -> dict:
    return {
        "schema_version": 1,
        "tracking_issue": 85,
        "mode": "shadow",
        "production_writes_enabled": False,
        "cutover_state": "shadow",
        "allowed_image_repositories": [service["repo"] for service in SERVICES],
        "allowed_source_repositories": [service["source"] for service in SERVICES],
        "allowed_stages": [service["stage"] for service in SERVICES],
        "max_replicas_per_service": 3,
        "backend_api": {"writes_enabled": False},
        "services": [
            {
                "name": service["name"],
                "stage": service["stage"],
                "image": f"{service['repo']}@{service['digest']}",
                "runtime_mode": "shadow",
                "network_mode": "host",
                "replicas": 1,
                "resources": {"memory": "256m", "cpus": "0.50"},
                "healthcheck": {"test": ["CMD", "node", "-e", f"fetch('http://127.0.0.1:{service['port']}/ready')"]},
                "provenance": {
                    "required": True,
                    "signed": True,
                    "subject_digest": service["digest"],
                    "source_repository": service["source"],
                },
                "env": {"NUTSNEWS_ENVIRONMENT": "production"},
                "secret_env": [{"name": name, "env_key": f"NUTSNEWS_{service['stage'].upper()}_DATABASE_URL"} for name in service["secrets"]],
                "queues": {"main": service["main_queue"], "retry": [], "dlq": f"{service['main_queue']}.dlq"},
            }
            for service in SERVICES
        ],
    }


def validate() -> list[str]:
    errors: list[str] = []
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    compose = read(COMPOSE_TEMPLATE)
    protected = read(PROTECTED_APPLY)
    rabbitmq_topology = read(RABBITMQ_TOPOLOGY)
    rabbitmq_manager = read(RABBITMQ_MANAGER)
    checks = read(CHECKS_WORKFLOW)
    runbook = read(RUNBOOK)

    require(
        "worker runtime role",
        "\n".join([defaults, tasks, compose]),
        (
            "backend_worker_runtime_services:",
            "tracking_issue: 117",
            "runtime_mode: shadow",
            "network_mode: host",
            "NUTSNEWS_ENVIRONMENT: production",
            "DEPENDENCY_MODE: production",
            "SHADOW_MODE: \"true\"",
            "secret_env",
            "backend_worker_runtime_secret_values",
            "production_write_path: false",
        ),
        errors,
    )
    require(
        "worker runtime protected apply",
        protected,
        (
            "urllib.parse",
            "Worker runtime requires worker-uplift PostgreSQL stage roles to be enabled.",
            "Worker runtime scheduler requires NUTSNEWS_BACKEND_API_TOKEN",
            "postgres_url(\"scheduler\")",
            "rabbitmq_url(\"RABBITMQ_FETCHER_CONSUMER_USERNAME\", \"RABBITMQ_FETCHER_CONSUMER_PASSWORD\")",
            "backend_worker_runtime_secret_values",
        ),
        errors,
    )
    require(
        "RabbitMQ runtime topology",
        "\n".join([rabbitmq_topology, rabbitmq_manager]),
        (
            "\"kind\": \"stage_runtime\"",
            "stage_runtime requires a declared outbound route",
            "cannot write the main exchange for its outbound route",
            "can read unrelated queue",
        ),
        errors,
    )
    if "python3 scripts/validate_worker_uplift_non_ai_shadow_deployment.py" not in checks:
        errors.append("Backend Checks must run worker uplift non-AI shadow deployment validator")
    require(
        "worker runtime runbook",
        runbook,
        (
            "#117",
            "scheduler",
            "fetcher",
            "canonicalizer",
            "enrichment",
            "host networking",
            "approval queue",
        ),
        errors,
    )

    for service in SERVICES:
        require(
            f"{service['name']} service config",
            defaults,
            (
                f"name: {service['name']}",
                f"stage: {service['stage']}",
                f"{service['repo']}@{service['digest']}",
                f"image_tag: {service['tag']}",
                f"source_repository: {service['source']}",
                f"subject_digest: {service['digest']}",
                f"contract_version: \"{service['contract']}\"",
                f"runtime_package_version: \"{service['runtime']}\"",
                f"127.0.0.1:{service['port']}/ready",
                f"main: {service['main_queue']}",
                service["schema"],
            )
            + tuple(service["secrets"])
            + tuple(service["publishes"]),
            errors,
        )

    for forbidden in (
        "NUTSNEWS_BACKEND_WORKER_RUNTIME_PRODUCTION_WRITES_ENABLED=true",
        "wrangler",
        "CLOUDFLARE_API_TOKEN",
    ):
        if forbidden in defaults + tasks + compose + protected:
            errors.append(f"non-AI shadow deployment contains forbidden fragment: {forbidden}")

    manager_errors = load_manager().validate_manifest(manifest_for_manager())
    if manager_errors:
        errors.append(f"non-AI manifest failed worker runtime manager validation: {manager_errors}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift non-AI shadow deployment is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
