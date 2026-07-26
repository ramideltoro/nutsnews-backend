#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


def scheduler_manifest() -> dict:
    manifest = valid_manifest()
    digest = "sha256:" + ("d" * 64)
    manifest["allowed_image_repositories"] = [
        "ghcr.io/ramideltoro/nutsnews-worker-feed-scheduler",
    ]
    manifest["allowed_source_repositories"] = [
        "ramideltoro/nutsnews-worker-feed-scheduler",
    ]
    manifest["allowed_stages"] = ["scheduler"]
    manifest["services"] = [
        {
            "name": "scheduler",
            "stage": "scheduler",
            "image": f"ghcr.io/ramideltoro/nutsnews-worker-feed-scheduler@{digest}",
            "runtime_mode": "shadow",
            "network_mode": "host",
            "replicas": 1,
            "resources": {"memory": "256m", "cpus": "0.35"},
            "healthcheck": {"test": ["CMD", "node", "-e", "fetch('http://127.0.0.1:18081/ready')"]},
            "provenance": {
                "required": True,
                "signed": True,
                "subject_digest": digest,
                "source_repository": "ramideltoro/nutsnews-worker-feed-scheduler",
            },
            "env": {
                "NUTSNEWS_SCHEDULER_DEPENDENCY_MODE": "production",
                "NUTSNEWS_SCHEDULER_SHADOW_MODE": "true",
            },
            "secret_env": [
                {"name": "scheduler-database-url", "env_key": "NUTSNEWS_SCHEDULER_DATABASE_URL"},
                {"name": "scheduler-rabbitmq-url", "env_key": "NUTSNEWS_SCHEDULER_RABBITMQ_URL"},
            ],
            "queues": {"main": "nutsnews.worker.fetch.v1", "retry": [], "dlq": "nutsnews.worker.fetch.v1.dlq"},
            "postgres": {"production_write_path": False},
        }
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

    def test_reconciliation_dry_run_emits_safe_stage_plan(self):
        manager = load_manager()
        values = {
            "received_inbox": "2",
            "stale_unprocessed_inbox": "1",
            "failed_or_parked_inbox": "0",
            "unconfirmed_outbox": "3",
            "stale_unconfirmed_outbox": "2",
            "dead_lettered_outbox": "0",
            "oldest_unconfirmed_outbox_age_seconds": "1200",
            "watermark_rows": "1",
            "watermark_lag_total": "0",
            "sample_pipeline_count": "1",
            "sample_outbox_pipeline_count": "2",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "services.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            service_dir = root / "services"
            service_dir.mkdir()
            (service_dir / "fetcher.env").write_text(
                "NUTSNEWS_FETCHER_DATABASE_URL=postgresql://example\n"
                "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN=test-reconcile-token\n",
                encoding="utf-8",
            )
            args = manager.parse_args(
                [
                    "reconciliation",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(root / "compose.yml"),
                    "--service-name",
                    "fetcher",
                    "--dry-run",
                    "--confirm-action",
                ]
            )
            service_response = {
                "status": "received",
                "http_status": 409,
                "body": {
                    "service": "fetcher",
                    "status": "failed_closed",
                    "selectedCount": 0,
                    "replayedCount": 0,
                    "writesPerformed": False,
                    "productionVisibilityEnabled": False,
                    "legacyRuntimeRequired": False,
                },
            }
            with (
                mock.patch.object(manager, "psql_key_values", return_value=values) as psql,
                mock.patch.object(manager, "http_post_json_local", return_value=service_response) as post,
            ):
                report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "dry_run")
        self.assertTrue(report["reconciliation"]["safe_metadata_only"])
        self.assertFalse(report["reconciliation"]["writes_performed"])
        self.assertFalse(report["reconciliation"]["legacy_runtime_required"])
        self.assertEqual(report["reconciliation"]["schema"], "worker_uplift_fetcher")
        self.assertEqual(report["reconciliation"]["values"]["unconfirmed_outbox"], "3")
        self.assertIn("service-owned-outbox-republish", [item["id"] for item in report["reconciliation"]["planned_actions"]])
        self.assertEqual(report["reconciliation"]["service_invocation"]["service_report"]["status"], "failed_closed")
        self.assertEqual(report["reconciliation"]["service_invocation"]["request"]["mode"], "dry-run")
        self.assertEqual(psql.call_count, 1)
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("test-reconcile-token", json.dumps(report))

    def test_reconciliation_apply_invokes_service_replayer(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "services.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            service_dir = root / "services"
            service_dir.mkdir()
            (service_dir / "fetcher.env").write_text(
                "NUTSNEWS_FETCHER_DATABASE_URL=postgresql://example\n"
                "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN=test-reconcile-token\n",
                encoding="utf-8",
            )
            args = manager.parse_args(
                [
                    "reconciliation",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(root / "compose.yml"),
                    "--service-name",
                    "fetcher",
                    "--confirm-action",
                ]
            )
            service_response = {
                "status": "received",
                "http_status": 200,
                "body": {
                    "service": "fetcher",
                    "status": "applied",
                    "selectedCount": 0,
                    "replayedCount": 0,
                    "writesPerformed": False,
                    "productionVisibilityEnabled": False,
                    "legacyRuntimeRequired": False,
                },
            }
            with (
                mock.patch.object(manager, "psql_key_values", return_value={}),
                mock.patch.object(manager, "http_post_json_local", return_value=service_response) as post,
            ):
                report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reconciliation"]["planned_actions"][0]["id"], "no-op")
        request_payload = post.call_args.args[2]
        self.assertEqual(request_payload["mode"], "apply")
        self.assertEqual(request_payload["protectedConfirmation"], "fetcher:fail-closed:v1")

    def test_reconciliation_apply_fails_when_selected_candidates_cannot_hydrate(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "services.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            service_dir = root / "services"
            service_dir.mkdir()
            (service_dir / "fetcher.env").write_text(
                "NUTSNEWS_FETCHER_DATABASE_URL=postgresql://example\n"
                "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN=test-reconcile-token\n",
                encoding="utf-8",
            )
            args = manager.parse_args(
                [
                    "reconciliation",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(root / "compose.yml"),
                    "--service-name",
                    "fetcher",
                    "--confirm-action",
                ]
            )
            service_response = {
                "status": "received",
                "http_status": 409,
                "body": {
                    "service": "fetcher",
                    "status": "failed_closed",
                    "selectedCount": 1,
                    "replayedCount": 0,
                    "writesPerformed": False,
                    "productionVisibilityEnabled": False,
                    "legacyRuntimeRequired": False,
                },
            }
            with (
                mock.patch.object(manager, "psql_key_values", return_value={}),
                mock.patch.object(manager, "http_post_json_local", return_value=service_response),
            ):
                report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "fail")
        self.assertIn("failed_closed", report["summary"])

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

    def test_approval_smoke_diagnostics_include_downstream_failure_context(self):
        manager = load_manager()
        persistence_query = manager.persistence_smoke_diagnostic_query("article-001")
        publication_query = manager.publication_smoke_diagnostic_query("article-001")
        self.assertIn("worker_uplift_persistence.inbox", persistence_query)
        self.assertIn("sanitized_error_code", persistence_query)
        self.assertIn("worker_uplift_final.article_shadow_aggregates", persistence_query)
        self.assertNotIn("worker_uplift_final.api_command_receipts", persistence_query)
        self.assertIn("worker_uplift_publication.publication_readiness", publication_query)
        self.assertIn("worker_uplift_publication.publication_decisions", publication_query)

    def test_scheduler_smoke_dry_run_has_pipeline_contract(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "services.json"
            manifest_path.write_text(json.dumps(scheduler_manifest()), encoding="utf-8")
            args = manager.parse_args(
                [
                    "smoke",
                    "--manifest",
                    str(manifest_path),
                    "--compose",
                    str(Path(tmpdir) / "compose.yml"),
                    "--service-name",
                    "scheduler",
                    "--dry-run",
                    "--confirm-action",
                ]
            )
            report = manager.build_report(args, manager.load_json(manifest_path))
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["smoke"]["service"], "scheduler")
        self.assertIn("scheduler-compatible feed-to-final pipeline fixture", report["smoke"]["fixtures"])
        self.assertEqual(report["smoke"]["pipeline_stages"], ["fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"])

    def test_pipeline_fetch_payload_and_envelope_match_scheduler_contract(self):
        manager = load_manager()
        fixture = {
            "feed_id": "worker-shadow-smoke-pipeline-001",
            "feed_url": "http://65.75.201.18:49152/worker-uplift-shadow-smoke/token/feed.xml",
        }
        occurred_at = "2026-07-26T12:00:00.000Z"
        payload = manager.pipeline_fetch_payload(fixture, "pipeline-001", occurred_at)
        envelope = manager.pipeline_fetch_envelope(fixture, "pipeline-001", payload, occurred_at)
        self.assertEqual(payload["schemaId"], "nutsnews.worker.payload.feed-fetch-request.v1")
        self.assertEqual(payload["fetchReason"], "scheduled")
        self.assertEqual(payload["limits"]["maxItems"], 1)
        self.assertEqual(envelope["schemaId"], "nutsnews.worker.envelope.v1")
        self.assertEqual(envelope["route"], "fetch")
        self.assertEqual(envelope["aggregate"], {"type": "feed", "id": fixture["feed_id"], "version": 1})
        self.assertEqual(envelope["payloadRef"]["digest"], manager.sha256_json(payload))

    def test_pipeline_article_id_uses_canonicalizer_normalization_seed(self):
        manager = load_manager()
        article_url = "HTTP://Example.COM:80/news/story/?utm_source=x&b=2&a=1#fragment"
        self.assertEqual(manager.normalize_article_url(article_url), "http://example.com/news/story?a=1&b=2")
        self.assertTrue(manager.stable_article_id(article_url).startswith("article_"))
        self.assertEqual(len(manager.stable_article_id(article_url)), len("article_") + 32)

    def test_pipeline_queries_cover_shadow_final_and_api_receipts(self):
        manager = load_manager()
        approval_query = manager.pipeline_approval_smoke_query("article-001")
        api_query = manager.pipeline_api_audit_query("article-001")
        self.assertIn("worker_uplift_approval.approval_decisions", approval_query)
        self.assertIn("decision = 'approved'", approval_query)
        self.assertIn("worker_uplift_persistence.write_requests", api_query)
        self.assertIn("failed_api_requests", api_query)


if __name__ == "__main__":
    unittest.main()
