#!/usr/bin/env python3
"""Validate worker-uplift service runtime framework guardrails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "files" / "nutsnews_worker_runtime.py"
DEFAULTS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
TASKS = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "tasks" / "main.yml"
MANIFEST_TEMPLATE = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "templates" / "worker-uplift-services.json.j2"
COMPOSE_TEMPLATE = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "templates" / "worker-uplift-compose.yml.j2"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
OPERATIONS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-runtime-operations.yml"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_SERVICE_RUNTIME.md"


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


def valid_manifest() -> dict:
    digest = "sha256:" + ("a" * 64)
    return {
        "schema_version": 1,
        "tracking_issue": 85,
        "mode": "shadow",
        "production_writes_enabled": False,
        "cutover_state": "shadow",
        "allowed_image_repositories": ["ghcr.io/ramideltoro/nutsnews-worker-uplift/"],
        "allowed_source_repositories": ["ramideltoro/nutsnews-backend"],
        "allowed_stages": ["fetcher"],
        "max_replicas_per_service": 3,
        "backend_api": {"writes_enabled": False},
        "services": [
            {
                "name": "fetcher",
                "stage": "fetcher",
                "image": f"ghcr.io/ramideltoro/nutsnews-worker-uplift/fetcher@{digest}",
                "runtime_mode": "shadow",
                "replicas": 1,
                "resources": {"memory": "256m", "cpus": "0.50"},
                "healthcheck": {"test": ["CMD", "/app/healthcheck"]},
                "provenance": {
                    "required": True,
                    "signed": True,
                    "subject_digest": digest,
                    "source_repository": "ramideltoro/nutsnews-backend",
                },
                "env": {"NUTSNEWS_RUNTIME_MODE": "shadow"},
                "network_mode": "host",
                "secret_files": [
                    {
                        "name": "backend-api-token",
                        "env_key": "NUTSNEWS_BACKEND_API_TOKEN_FILE",
                        "host_path": "/etc/nutsnews-worker-uplift/services/fetcher/secrets/backend-api-token",
                        "path": "/run/secrets/backend-api-token",
                    }
                ],
                "secret_env": [
                    {
                        "name": "database-url",
                        "env_key": "NUTSNEWS_FETCHER_DATABASE_URL",
                    }
                ],
                "queues": {"main": "nutsnews.worker.fetch.v1", "retry": [], "dlq": "nutsnews.worker.fetch.v1.dlq"},
            }
        ],
    }


def validate() -> list[str]:
    errors: list[str] = []
    manager_text = read(MANAGER_PATH)
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    manifest_template = read(MANIFEST_TEMPLATE)
    compose_template = read(COMPOSE_TEMPLATE)
    playbook = read(PLAYBOOK)
    protected_apply = read(PROTECTED_APPLY)
    operations_workflow = read(OPERATIONS_WORKFLOW)
    checks_workflow = read(CHECKS_WORKFLOW)
    runbook = read(RUNBOOK) if RUNBOOK.exists() else ""

    require(
        "worker runtime defaults",
        defaults,
        (
            "backend_worker_runtime_enabled: false",
            "backend_worker_runtime_default_mode: shadow",
            "backend_worker_runtime_production_writes_enabled: false",
            "ghcr.io/ramideltoro/nutsnews-worker-uplift/",
            "ghcr.io/ramideltoro/nutsnews-worker-feed-scheduler",
            "ghcr.io/ramideltoro/nutsnews-worker-feed-fetcher",
            "ghcr.io/ramideltoro/nutsnews-worker-article-canonicalizer",
            "ghcr.io/ramideltoro/nutsnews-worker-article-enrichment",
            "backend_worker_runtime_allowed_actions:",
            "backend_worker_runtime_services:",
            "tracking_issue: 117",
        ),
        errors,
    )
    require(
        "worker runtime manager",
        manager_text,
        (
            "IMAGE_RE",
            "MUTATING_ACTIONS",
            "--confirm-action",
            "provenance.required must be true",
            "production writes require cutover_state=cutover-approved",
            "promote requires cutover_state=cutover-approved",
            "dlq-replay currently fails closed",
            "build_reconciliation_plan",
            "invoke_service_reconciliation",
            "service-owned reconciliation endpoint",
            "service-owned payload_ref hydration",
            "secret env",
            "network_mode must be bridge or host",
            "run_service_smoke",
            "approval_smoke_query",
            "translation_smoke_query",
            "Runtime smoke would publish sanitized service fixtures",
        ),
        errors,
    )
    for forbidden in ("eval(", "shell=True", "remote_command", "worker-pipeline.yml", "deploy_worker_shards", "wrangler"):
        if forbidden in manager_text:
            errors.append(f"worker runtime manager contains forbidden fragment: {forbidden}")

    require(
        "worker runtime tasks/templates",
        "\n".join([tasks, manifest_template, compose_template, playbook]),
        (
            "backend_worker_runtime",
            "worker-uplift-services.json.j2",
            "worker-uplift-compose.yml.j2",
            "backend_worker_runtime_manager_path",
            "legacy_worker_checkout_required",
            "grafana_resource_owner",
            "ramideltoro/nutsnews-infra",
            "backend_worker_runtime_secret_values",
            "secret_files",
            "secret_env",
            "network_mode:",
        ),
        errors,
    )
    require(
        "protected apply",
        protected_apply,
        (
            "NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED",
            "NUTSNEWS_BACKEND_WORKER_RUNTIME_PRODUCTION_WRITES_ENABLED",
            "Worker runtime production writes must remain disabled until the protected cutover state is implemented.",
            "backend_worker_runtime_enabled",
            "backend_worker_runtime_secret_values",
            "scheduler-database-url",
            "fetcher-rabbitmq-url",
        ),
        errors,
    )
    if protected_apply.count("NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED") < 3:
        errors.append("protected apply must pass NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED into both preflight and extra-vars steps")
    if protected_apply.count("NUTSNEWS_BACKEND_WORKER_RUNTIME_PRODUCTION_WRITES_ENABLED") < 3:
        errors.append("protected apply must pass NUTSNEWS_BACKEND_WORKER_RUNTIME_PRODUCTION_WRITES_ENABLED into both preflight and extra-vars steps")
    require(
        "worker runtime operations workflow",
        operations_workflow,
        (
            "Backend Worker Runtime Operations",
            "environment: production-backend",
            "- promote",
            "confirm_target",
            "backend.nutsnews.com",
            "/usr/local/sbin/nutsnews-worker-runtime",
            "backend-worker-runtime-report",
        ),
        errors,
    )
    for forbidden in ("remote_command", "command_input", "script_body", "GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"):
        if forbidden in operations_workflow:
            errors.append(f"worker runtime operations workflow contains forbidden fragment: {forbidden}")

    manager = load_manager()
    manifest = valid_manifest()
    manifest_errors = manager.validate_manifest(manifest)
    if manifest_errors:
        errors.append(f"valid worker runtime manifest failed validation: {manifest_errors}")
    bad_manifest = valid_manifest()
    bad_manifest["services"][0]["image"] = "docker.io/library/busybox:latest"
    bad_errors = "\n".join(manager.validate_manifest(bad_manifest))
    if "image must be" not in bad_errors:
        errors.append("worker runtime validator must reject mutable or non-GHCR image references")
    bad_manifest = valid_manifest()
    bad_manifest["production_writes_enabled"] = True
    bad_errors = "\n".join(manager.validate_manifest(bad_manifest))
    if "cutover_state=cutover-approved" not in bad_errors:
        errors.append("worker runtime validator must reject production writes before cutover")

    if "python3 scripts/validate_worker_uplift_service_runtime.py" not in checks_workflow:
        errors.append("Backend Checks must run worker uplift service runtime validator")
    require(
        "worker runtime runbook",
        runbook,
        (
            "#85",
            "Backend Worker Runtime Operations",
            "No backend workflow provisions Grafana resources",
            "legacy checkout",
            "shadow",
        ),
        errors,
    )
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift service runtime framework is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
