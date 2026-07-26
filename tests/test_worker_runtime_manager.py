#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MANAGER_PATH = Path("ansible/roles/backend_worker_runtime/files/nutsnews_worker_runtime.py")


def load_manager():
    spec = importlib.util.spec_from_file_location("nutsnews_worker_runtime", MANAGER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
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


def ai_manifest() -> dict:
    manifest = valid_manifest()
    approval_digest = "sha256:" + ("b" * 64)
    translation_digest = "sha256:" + ("c" * 64)
    manifest["allowed_image_repositories"] = [
        "ghcr.io/ramideltoro/nutsnews-worker-article-approval",
        "ghcr.io/ramideltoro/nutsnews-worker-article-translation",
    ]
    manifest["allowed_source_repositories"] = [
        "ramideltoro/nutsnews-worker-article-approval",
        "ramideltoro/nutsnews-worker-article-translation",
    ]
    manifest["allowed_stages"] = ["approval", "translation"]
    manifest["services"] = [
        {
            "name": "approval",
            "stage": "approval",
            "image": f"ghcr.io/ramideltoro/nutsnews-worker-article-approval@{approval_digest}",
            "runtime_mode": "shadow",
            "network_mode": "host",
            "replicas": 1,
            "resources": {"memory": "512m", "cpus": "0.75"},
            "healthcheck": {"test": ["CMD", "node", "-e", "fetch('http://127.0.0.1:18085/ready')"]},
            "provenance": {
                "required": True,
                "signed": True,
                "subject_digest": approval_digest,
                "source_repository": "ramideltoro/nutsnews-worker-article-approval",
            },
            "env": {
                "NUTSNEWS_APPROVAL_DEPENDENCY_MODE": "production",
                "NUTSNEWS_APPROVAL_SHADOW_MODE": "true",
                "NUTSNEWS_APPROVAL_OPENAI_FALLBACK_ENABLED": "false",
            },
            "secret_env": [
                {"name": "approval-database-url", "env_key": "NUTSNEWS_APPROVAL_DATABASE_URL"},
                {"name": "approval-rabbitmq-url", "env_key": "NUTSNEWS_APPROVAL_RABBITMQ_URL"},
            ],
            "queues": {"main": "nutsnews.worker.approval.v1", "retry": [], "dlq": "nutsnews.worker.approval.v1.dlq"},
            "postgres": {"production_write_path": False},
        },
        {
            "name": "translation",
            "stage": "translation",
            "image": f"ghcr.io/ramideltoro/nutsnews-worker-article-translation@{translation_digest}",
            "runtime_mode": "shadow",
            "network_mode": "host",
            "replicas": 1,
            "resources": {"memory": "768m", "cpus": "0.75"},
            "healthcheck": {"test": ["CMD", "node", "-e", "fetch('http://127.0.0.1:18086/ready')"]},
            "provenance": {
                "required": True,
                "signed": True,
                "subject_digest": translation_digest,
                "source_repository": "ramideltoro/nutsnews-worker-article-translation",
            },
            "env": {
                "NUTSNEWS_TRANSLATION_DEPENDENCY_MODE": "production",
                "NUTSNEWS_TRANSLATION_SHADOW_MODE": "true",
            },
            "secret_env": [
                {"name": "translation-database-url", "env_key": "NUTSNEWS_TRANSLATION_DATABASE_URL"},
                {"name": "translation-rabbitmq-url", "env_key": "NUTSNEWS_TRANSLATION_RABBITMQ_URL"},
            ],
            "queues": {"main": "nutsnews.worker.translation.v1", "retry": [], "dlq": "nutsnews.worker.translation.v1.dlq"},
            "postgres": {"production_write_path": False},
        },
    ]
    return manifest


class WorkerRuntimeManagerTests(unittest.TestCase):
    def test_valid_manifest_passes(self):
        manager = load_manager()
        self.assertEqual(manager.validate_manifest(valid_manifest()), [])

    def test_rejects_mutable_or_untrusted_images(self):
        manager = load_manager()
        manifest = valid_manifest()
        manifest["services"][0]["image"] = "docker.io/library/busybox:latest"
        errors = "\n".join(manager.validate_manifest(manifest))
        self.assertIn("image must be", errors)
        manifest = valid_manifest()
        manifest["services"][0]["image"] = "ghcr.io/ramideltoro/evil/fetcher@sha256:" + ("b" * 64)
        manifest["services"][0]["provenance"]["subject_digest"] = "sha256:" + ("b" * 64)
        errors = "\n".join(manager.validate_manifest(manifest))
        self.assertIn("untrusted", errors)

    def test_rejects_production_writes_before_cutover(self):
        manager = load_manager()
        manifest = valid_manifest()
        manifest["production_writes_enabled"] = True
        errors = "\n".join(manager.validate_manifest(manifest))
        self.assertIn("cutover_state=cutover-approved", errors)

    def test_rejects_committed_secret_env_values(self):
        manager = load_manager()
        manifest = valid_manifest()
        manifest["services"][0]["secret_env"][0]["value"] = "postgresql://user:password@127.0.0.1/db"
        errors = "\n".join(manager.validate_manifest(manifest))
        self.assertIn("must not store values in manifest", errors)

    def test_rejects_invalid_network_mode(self):
        manager = load_manager()
        manifest = valid_manifest()
        manifest["services"][0]["network_mode"] = "service:rabbitmq"
        errors = "\n".join(manager.validate_manifest(manifest))
        self.assertIn("network_mode must be bridge or host", errors)

    def test_mutating_action_requires_confirmation_before_docker(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "services.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            args = manager.parse_args(
                [
                    "scale",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(Path(tmpdir) / "compose.yml"),
                    "--service-name",
                    "fetcher",
                    "--replicas",
                    "1",
                ]
            )
            report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "fail")
        self.assertIn("mutating action requires --confirm-action", report["errors"])
        self.assertEqual(report["commands"], [])

    def test_status_passes_with_no_services_configured(self):
        manager = load_manager()
        manifest = valid_manifest()
        manifest["services"] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "services.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            args = manager.parse_args(["status", "--manifest", str(manifest_path), "--compose", str(Path(tmpdir) / "missing.yml")])
            report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "pass")
        self.assertIn("no services are configured", report["summary"])
        self.assertEqual(report["commands"], [])

    def test_queue_inspect_is_fixed_to_declared_queues(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "services.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            args = manager.parse_args(
                [
                    "queue-inspect",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(Path(tmpdir) / "compose.yml"),
                    "--service-name",
                    "fetcher",
                    "--rabbitmq-env",
                    str(Path(tmpdir) / "missing.env"),
                ]
            )
            report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["queues"][0]["queue"], "nutsnews.worker.fetch.v1")
        self.assertEqual(report["queues"][0]["status"], "not_configured")

    def test_ai_smoke_dry_run_has_fixed_fixture_contract(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "services.json"
            manifest_path.write_text(json.dumps(ai_manifest()), encoding="utf-8")
            args = manager.parse_args(
                [
                    "smoke",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(Path(tmpdir) / "compose.yml"),
                    "--service-name",
                    "approval",
                    "--dry-run",
                    "--confirm-action",
                ]
            )
            report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["smoke"]["service"], "approval")
        self.assertIn("approval accepted/rejected", report["smoke"]["fixtures"])
        self.assertEqual(report["smoke"]["expected_target_languages"], ["fr", "ja", "de-CH", "de", "el"])

    def test_approval_smoke_queries_downstream_final_shadow_state(self):
        manager = load_manager()
        final_query = manager.final_shadow_smoke_query("article-001")
        publication_query = manager.publication_smoke_query("article-001")
        self.assertIn("worker_uplift_final.article_shadow_aggregates", final_query)
        self.assertIn("worker_uplift_persistence.outbox", final_query)
        self.assertIn("nutsnews.worker.publication.v1", final_query)
        self.assertIn("worker_uplift_publication.publication_readiness", publication_query)
        self.assertIn("worker_uplift_publication.publication_decisions", publication_query)
        self.assertIn("shadow-publication-comparison", publication_query)


if __name__ == "__main__":
    unittest.main()
