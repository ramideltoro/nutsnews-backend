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


if __name__ == "__main__":
    unittest.main()
