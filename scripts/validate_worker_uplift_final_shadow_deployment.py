#!/usr/bin/env python3
"""Validate persistence/publication final-shadow deployment guardrails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "files" / "nutsnews_worker_runtime.py"
DEFAULTS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNTIME_WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-runtime-operations.yml"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_SERVICE_RUNTIME.md"
RABBITMQ_TOPOLOGY = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "worker-uplift-topology.json.j2"
SHADOW_MODEL = ROOT / "ansible" / "roles" / "backend_baseline" / "templates" / "worker-uplift-shadow-data-model.sql.j2"


SERVICES = [
    {
        "name": "persistence",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-persistence",
        "source": "ramideltoro/nutsnews-worker-article-persistence",
        "digest": "sha256:c5b5ac93d54742ff7a418da33c3cd2f373ceaaf20e5dc3f566a7b129fcda2ee1",
        "tag": "9d2f618a7f43e76a3230585ea5bdb28e2d43ab5a",
        "port": "18087",
        "queue": "nutsnews.worker.persistence.v1",
        "publishes": "nutsnews.worker.publication.v1",
        "schema": "worker_uplift_persistence",
        "role": "nutsnews_worker_uplift_persistence",
        "prefix": "NUTSNEWS_PERSISTENCE",
        "secrets": ("persistence-database-url", "persistence-rabbitmq-url", "persistence-backend-api-token", "reconciliation-token"),
        "token": "NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN",
    },
    {
        "name": "publication",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-publication",
        "source": "ramideltoro/nutsnews-worker-article-publication",
        "digest": "sha256:85ba95750ddc94e38c52962b896394b98ca0e64e59313e758963fd5e9dd4a94d",
        "tag": "97920706fdcad69030f649ed1a3ac34dab7d7982",
        "port": "18088",
        "queue": "nutsnews.worker.publication.v1",
        "publishes": "",
        "schema": "worker_uplift_publication",
        "role": "nutsnews_worker_uplift_publication",
        "prefix": "NUTSNEWS_PUBLICATION",
        "secrets": ("publication-database-url", "publication-rabbitmq-url", "publication-backend-api-token", "reconciliation-token"),
        "token": "NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN",
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
        "allowed_stages": [service["name"] for service in SERVICES],
        "max_replicas_per_service": 3,
        "backend_api": {"writes_enabled": False},
        "services": [
            {
                "name": service["name"],
                "stage": service["name"],
                "image": f"{service['repo']}@{service['digest']}",
                "runtime_mode": "shadow",
                "network_mode": "host",
                "replicas": 1,
                "resources": {"memory": "512m", "cpus": "0.50"},
                "healthcheck": {"test": ["CMD", "node", "-e", f"fetch('http://127.0.0.1:{service['port']}/ready')"]},
                "provenance": {
                    "required": True,
                    "signed": True,
                    "subject_digest": service["digest"],
                    "source_repository": service["source"],
                },
                "env": {
                    f"{service['prefix']}_DEPENDENCY_MODE": "production",
                    f"{service['prefix']}_BACKEND_API_BASE_URL": "https://backend.nutsnews.com/api/worker/db",
                    f"{service['prefix']}_BACKEND_API_IDENTITY": f"worker-uplift-{service['name']}",
                },
                "secret_env": [{"name": name, "env_key": f"{service['prefix']}_DATABASE_URL"} for name in service["secrets"]],
                "queues": {"main": service["queue"], "retry": [], "dlq": f"{service['queue']}.dlq"},
                "postgres": {"production_write_path": False},
            }
            for service in SERVICES
        ],
    }


def validate() -> list[str]:
    errors: list[str] = []
    defaults = read(DEFAULTS)
    manager = read(MANAGER_PATH)
    protected = read(PROTECTED_APPLY)
    checks = read(CHECKS_WORKFLOW)
    runtime_workflow = read(RUNTIME_WORKFLOW)
    runbook = read(RUNBOOK)
    rabbitmq_topology = read(RABBITMQ_TOPOLOGY)
    shadow_model = read(SHADOW_MODEL)

    require(
        "protected apply",
        protected,
        (
            "Worker runtime final shadow services require NUTSNEWS_BACKEND_WORKER_UPLIFT_SCOPED_TOKENS_ENABLED=true.",
            "Worker runtime legacy, persistence, and publication API tokens must be distinct",
            "persistence-backend-api-token",
            "publication-backend-api-token",
            "rabbitmq_url(\"RABBITMQ_PERSISTENCE_CONSUMER_USERNAME\", \"RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD\")",
            "rabbitmq_url(\"RABBITMQ_PUBLICATION_CONSUMER_USERNAME\", \"RABBITMQ_PUBLICATION_CONSUMER_PASSWORD\")",
            "postgres_url(\"persistence\")",
            "postgres_url(\"publication\")",
        ),
        errors,
    )
    for service in SERVICES:
        require(
            f"{service['name']} service",
            defaults,
            (
                f"name: {service['name']}",
                "tracking_issue: 119",
                f"{service['repo']}@{service['digest']}",
                f"image_tag: {service['tag']}",
                f"source_repository: {service['source']}",
                f"subject_digest: {service['digest']}",
                "contract_version: \"0.4.0\"",
                "runtime_package_version: \"0.5.0\"",
                "runtime_mode: shadow",
                "network_mode: host",
                f"127.0.0.1:{service['port']}/ready",
                f"{service['prefix']}_DEPENDENCY_MODE: production",
                f"{service['prefix']}_BACKEND_API_BASE_URL: \"{{{{ backend_worker_runtime_backend_api_url }}}}/api/worker/db\"",
                f"{service['prefix']}_BACKEND_API_IDENTITY: worker-uplift-{service['name']}",
                f"main: {service['queue']}",
                service["schema"],
                "production_write_path: false",
            )
            + tuple(service["secrets"]),
            errors,
        )
        require(
            f"{service['name']} credential topology",
            "\n".join([protected, rabbitmq_topology, shadow_model]),
            (
                service["token"],
                service["role"],
                service["queue"],
            ),
            errors,
        )
        if service["publishes"]:
            require(f"{service['name']} publish route", defaults, (service["publishes"],), errors)

    require(
        "publication hard guard",
        defaults,
        (
            "NUTSNEWS_PUBLICATION_WRITE_MODE: shadow_comparison",
            "NUTSNEWS_PUBLICATION_FEATURE_FLAG: worker-uplift-publication-shadow",
            "NUTSNEWS_PUBLICATION_POLICY_ID: worker-uplift-api-admin-compatibility-contract",
        ),
        errors,
    )
    require(
        "persistence hard guard",
        defaults,
        (
            "NUTSNEWS_PERSISTENCE_SHADOW_MODE: \"true\"",
            "NUTSNEWS_PERSISTENCE_PRODUCTION_WRITES_ENABLED: \"false\"",
            "worker_uplift_final",
            "worker_uplift_views",
        ),
        errors,
    )
    require(
        "worker runtime final smoke",
        manager,
        (
            "final_shadow_smoke_query",
            "publication_smoke_query",
            "persistence_smoke_diagnostic_query",
            "publication_smoke_diagnostic_query",
            "downstream_db_checks",
            "downstream_diagnostics",
            "worker_uplift_final.article_shadow_aggregates",
            "worker_uplift_persistence.outbox",
            "worker_uplift_persistence.inbox",
            "worker_uplift_publication.publication_readiness",
            "worker_uplift_publication.publication_decisions",
            "shadow-publication-comparison",
        ),
        errors,
    )
    require(
        "worker runtime workflow",
        runtime_workflow,
        (
            "ServerAliveInterval=30",
            "ServerAliveCountMax=20",
        ),
        errors,
    )
    if "NUTSNEWS_PUBLICATION_PRODUCTION_WRITE_CONFIRMATION:" in defaults:
        errors.append("publication service must not receive the protected production-write confirmation in shadow mode")
    for forbidden in (
        "NUTSNEWS_PUBLICATION_WRITE_MODE: production",
        "NUTSNEWS_PERSISTENCE_PRODUCTION_WRITES_ENABLED: \"true\"",
        "production_write_path: true",
        "CLOUDFLARE_API_TOKEN",
        "wrangler",
    ):
        if forbidden in defaults + protected:
            errors.append(f"final shadow deployment contains forbidden fragment: {forbidden}")

    if "python3 scripts/validate_worker_uplift_final_shadow_deployment.py" not in checks:
        errors.append("Backend Checks must run worker uplift final shadow deployment validator")
    require(
        "runbook",
        runbook,
        (
            "#119 Final Shadow Verification",
            "persistence",
            "publication",
            "NUTSNEWS_BACKEND_WORKER_UPLIFT_SCOPED_TOKENS_ENABLED=true",
            "shadow_comparison",
            "legacy Cloudflare ingestion",
        ),
        errors,
    )

    manager_errors = load_manager().validate_manifest(manifest_for_manager())
    if manager_errors:
        errors.append(f"final shadow manifest failed worker runtime manager validation: {manager_errors}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift final shadow deployment is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
