#!/usr/bin/env python3
"""Validate approval/translation worker-uplift shadow service guardrails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "files" / "nutsnews_worker_runtime.py"
DEFAULTS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_SERVICE_RUNTIME.md"


SERVICES = [
    {
        "name": "approval",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-approval",
        "source": "ramideltoro/nutsnews-worker-article-approval",
        "digest": "sha256:c2c18a6a8066e7b386ea293322c326a7cd8a5873b4b006cd7bca75a5ab886e45",
        "tag": "0eb56ec47794cdba804282c7468d81448715bd8b",
        "port": "18085",
        "queue": "nutsnews.worker.approval.v1",
        "publishes": "nutsnews.worker.translation.v1",
        "schema": "worker_uplift_approval",
        "secrets": ("approval-database-url", "approval-rabbitmq-url", "approval-qwen-base-url", "approval-qwen-api-key"),
        "prefix": "NUTSNEWS_APPROVAL",
    },
    {
        "name": "translation",
        "repo": "ghcr.io/ramideltoro/nutsnews-worker-article-translation",
        "source": "ramideltoro/nutsnews-worker-article-translation",
        "digest": "sha256:9f22bd32d54924fed448d44fd1e636f03d2a3bb0f944316fb10e977b5de4cdaf",
        "tag": "a6e47cb95ee78314bc3d2dfe980e61e6a32291a2",
        "port": "18086",
        "queue": "nutsnews.worker.translation.v1",
        "publishes": "nutsnews.worker.persistence.v1",
        "schema": "worker_uplift_translation",
        "secrets": ("translation-database-url", "translation-rabbitmq-url", "translation-qwen-base-url", "translation-qwen-api-key"),
        "prefix": "NUTSNEWS_TRANSLATION",
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
                "resources": {"memory": "512m", "cpus": "0.75"},
                "healthcheck": {"test": ["CMD", "node", "-e", f"fetch('http://127.0.0.1:{service['port']}/ready')"]},
                "provenance": {
                    "required": True,
                    "signed": True,
                    "subject_digest": service["digest"],
                    "source_repository": service["source"],
                },
                "env": {
                    f"{service['prefix']}_DEPENDENCY_MODE": "production",
                    f"{service['prefix']}_SHADOW_MODE": "true",
                },
                "secret_env": [{"name": name, "env_key": f"{service['prefix']}_DATABASE_URL"} for name in service["secrets"]],
                "queues": {"main": service["queue"], "retry": [], "dlq": f"{service['queue']}.dlq"},
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
    runbook = read(RUNBOOK)

    require(
        "protected apply",
        protected,
        (
            "LOCAL_AI_URL",
            "LOCAL_AI_API_KEY",
            "Worker runtime AI shadow services require LOCAL_AI_API_KEY",
            "approval-qwen-api-key",
            "translation-qwen-api-key",
            "rabbitmq_url(\"RABBITMQ_APPROVAL_CONSUMER_USERNAME\", \"RABBITMQ_APPROVAL_CONSUMER_PASSWORD\")",
            "rabbitmq_url(\"RABBITMQ_TRANSLATION_CONSUMER_USERNAME\", \"RABBITMQ_TRANSLATION_CONSUMER_PASSWORD\")",
        ),
        errors,
    )
    if "python3 scripts/validate_worker_uplift_ai_shadow_deployment.py" not in checks:
        errors.append("Backend Checks must run worker uplift AI shadow deployment validator")
    require(
        "worker runtime manager",
        manager,
        (
            "run_approval_smoke",
            "run_translation_smoke",
            "approval accepted/rejected",
            "translation per-language task",
            "accepted_translation_outbox",
            "accepted_language_records",
        ),
        errors,
    )
    require(
        "runbook",
        runbook,
        (
            "#118",
            "approval",
            "translation",
            "Qwen",
            "OpenAI fallback",
            "persistence queue",
        ),
        errors,
    )

    for service in SERVICES:
        require(
            f"{service['name']} service",
            defaults,
            (
                f"name: {service['name']}",
                "tracking_issue: 118",
                f"{service['repo']}@{service['digest']}",
                f"image_tag: {service['tag']}",
                f"source_repository: {service['source']}",
                f"subject_digest: {service['digest']}",
                "contract_version: \"0.4.0\"",
                "runtime_package_version: \"0.4.0\"",
                "runtime_mode: shadow",
                "network_mode: host",
                f"127.0.0.1:{service['port']}/ready",
                f"{service['prefix']}_DEPENDENCY_MODE: production",
                f"{service['prefix']}_SHADOW_MODE: \"true\"",
                "QWEN_MODEL: qwen2.5:3b",
                f"main: {service['queue']}",
                service["publishes"],
                service["schema"],
                "production_write_path: false",
                "prompt_logging: metadata_only",
                "openai_fallback_enabled: false",
            )
            + tuple(service["secrets"]),
            errors,
        )

    for forbidden in (
        "NUTSNEWS_APPROVAL_OPENAI_FALLBACK_ENABLED: \"true\"",
        "OPENAI_API_KEY",
        "production_write_path: true",
    ):
        if forbidden in defaults:
            errors.append(f"AI shadow deployment contains forbidden fragment: {forbidden}")

    manager_errors = load_manager().validate_manifest(manifest_for_manager())
    if manager_errors:
        errors.append(f"AI manifest failed worker runtime manager validation: {manager_errors}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift AI shadow deployment is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
