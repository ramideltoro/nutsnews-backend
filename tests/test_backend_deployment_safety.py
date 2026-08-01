#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backend_deployment_safety as safety
from tests.test_backend_controlled_maintenance import command, evidence


ROOT = Path(__file__).resolve().parents[1]


class BackendDeploymentSafetyTests(unittest.TestCase):
    @staticmethod
    def worker_runtime_status(*, expected_active=False, readiness_failures=None):
        services = {
            name: {
                "liveness": {"status": "healthy"},
                "readiness": {"status": "critical" if name in (readiness_failures or []) else "healthy"},
                "metrics": {"status": "healthy"},
            }
            for name in (
                "scheduler",
                "fetcher",
                "canonicalizer",
                "enrichment",
                "approval",
                "translation",
                "persistence",
                "publication",
            )
        }
        return {
            "status": "fail" if expected_active and readiness_failures else "pass",
            "expected_active": expected_active,
            "services": services,
            "unhealthy_liveness": [],
            "unhealthy_metrics": [],
            "unhealthy_readiness": readiness_failures or [],
        }

    def test_baseline_gate_fails_closed_for_missing_critical_evidence(self):
        checks = [
            {"name": "failed_systemd_units", "status": "healthy"},
            {"name": "kernel_alignment", "status": "healthy"},
            {"name": "root_disk_pressure", "status": "unknown"},
            {"name": "root_inode_pressure", "status": "healthy"},
            {"name": "service_ssh", "status": "healthy"},
            {"name": "service_ufw", "status": "healthy"},
            {"name": "service_caddy", "status": "healthy"},
            {"name": "reverse_proxy_health", "status": "healthy"},
            {"name": "public_endpoint_health", "status": "healthy"},
            {"name": "caddy_config", "status": "healthy"},
            {"name": "reboot_required", "status": "healthy"},
            {"name": "secret_presence", "status": "healthy"},
        ]
        self.assertIn({"check": "root_disk_pressure", "status": "unknown"}, safety.blockers("baseline_apply", checks))

    def test_not_configured_backup_is_reported_but_not_baseline_blocking(self):
        checks = [
            {"name": name, "status": "healthy"}
            for name in safety.CRITICAL_IF_NOT_HEALTHY["baseline_apply"]
        ]
        checks.append({"name": "backup_freshness", "status": "not_configured"})
        self.assertEqual(safety.blockers("baseline_apply", checks), [])

    def test_rabbitmq_is_post_apply_blocker_only_when_enabled(self):
        args = type("Args", (), {"profile": "baseline_apply", "phase": "post"})()
        checks = [
            {"name": "docker_health", "status": "not_configured"},
            {"name": "rabbitmq_health", "status": "not_configured"},
            {"name": "rabbitmq_network_security", "status": "not_configured"},
            {"name": "rabbitmq_drift", "status": "not_configured"},
            {"name": "rabbitmq_public_exposure", "status": "not_configured"},
        ]
        with patch.dict(os.environ, {"NUTSNEWS_BACKEND_RABBITMQ_ENABLED": "true"}, clear=True):
            self.assertEqual(
                safety.rabbitmq_post_apply_blockers(args, checks),
                [
                    {"check": "docker_health", "status": "not_configured"},
                    {"check": "rabbitmq_health", "status": "not_configured"},
                    {"check": "rabbitmq_network_security", "status": "not_configured"},
                    {"check": "rabbitmq_drift", "status": "not_configured"},
                    {"check": "rabbitmq_public_exposure", "status": "not_configured"},
                ],
            )
        pre_args = type("Args", (), {"profile": "baseline_apply", "phase": "pre"})()
        with patch.dict(os.environ, {"NUTSNEWS_BACKEND_RABBITMQ_ENABLED": "true"}, clear=True):
            self.assertEqual(safety.rabbitmq_post_apply_blockers(pre_args, checks), [])

    def test_rabbitmq_network_security_json_is_classified(self):
        fixture = evidence(rabbitmq_network_security=command('{"status":"pass","failed_checks":[]}\n'))
        check = safety.rabbitmq_network_security(fixture)
        self.assertEqual(check["status"], "healthy")
        self.assertEqual(check["summary"], "failed_checks=none")

        fixture = evidence(rabbitmq_network_security=command('{"status":"fail","failed_checks":["host_listeners"]}\n', returncode=1))
        check = safety.rabbitmq_network_security(fixture)
        self.assertEqual(check["status"], "critical")
        self.assertEqual(check["summary"], "failed_checks=host_listeners")

    def test_rabbitmq_public_exposure_detects_open_ports(self):
        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        side_effects = [ConnectionRefusedError(), FakeConnection(), TimeoutError()]
        with (
            patch.dict(os.environ, {"NUTSNEWS_BACKEND_RABBITMQ_ENABLED": "true"}, clear=True),
            patch.object(safety.socket, "create_connection", side_effect=side_effects),
        ):
            check = safety.rabbitmq_public_exposure("65.75.201.18", 1)
        self.assertEqual(check["status"], "critical")
        self.assertEqual(check["open_ports"], [15672])

    def test_rabbitmq_post_apply_blocks_network_drift_when_enabled(self):
        args = type("Args", (), {"profile": "baseline_apply", "phase": "post"})()
        checks = [
            {"name": "docker_health", "status": "healthy"},
            {"name": "rabbitmq_health", "status": "healthy"},
            {"name": "rabbitmq_network_security", "status": "critical"},
            {"name": "rabbitmq_drift", "status": "healthy"},
            {"name": "rabbitmq_public_exposure", "status": "healthy"},
        ]
        with patch.dict(os.environ, {"NUTSNEWS_BACKEND_RABBITMQ_ENABLED": "true"}, clear=True):
            self.assertEqual(safety.rabbitmq_post_apply_blockers(args, checks), [{"check": "rabbitmq_network_security", "status": "critical"}])

    def test_worker_runtime_shadow_readiness_is_visible_but_nonblocking(self):
        fixture = evidence(
            worker_runtime_observability=command(
                json.dumps(
                    self.worker_runtime_status(
                        expected_active=False,
                        readiness_failures=["scheduler", "fetcher"],
                    )
                )
            )
        )
        check = safety.worker_runtime_observability(fixture)
        self.assertEqual(check["status"], "healthy")
        self.assertIn("expected_active=false", check["summary"])
        self.assertIn("readiness_failures=2", check["summary"])

    def test_worker_runtime_production_readiness_is_critical(self):
        fixture = evidence(
            worker_runtime_observability=command(
                json.dumps(
                    self.worker_runtime_status(
                        expected_active=True,
                        readiness_failures=["scheduler"],
                    )
                ),
                returncode=1,
            )
        )
        check = safety.worker_runtime_observability(fixture)
        self.assertEqual(check["status"], "critical")

    def test_worker_runtime_is_post_apply_blocker_only_when_enabled(self):
        args = type("Args", (), {"profile": "baseline_apply", "phase": "post"})()
        checks = [{"name": "worker_runtime_observability", "status": "not_configured"}]
        with patch.dict(os.environ, {"NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED": "true"}, clear=True):
            self.assertEqual(
                safety.worker_runtime_post_apply_blockers(args, checks),
                [
                    {"check": "worker_runtime_observability", "status": "not_configured"},
                    {"check": "worker_runtime_grafana_observability", "status": "missing"},
                ],
            )
        with patch.dict(os.environ, {"NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED": "false"}, clear=True):
            self.assertEqual(safety.worker_runtime_post_apply_blockers(args, checks), [])

    def test_worker_runtime_grafana_gate_requires_exact_eight_up_fresh_and_identity(self):
        expression = safety.worker_runtime_grafana_expression()
        for token in (
            'job="nutsnews-worker-uplift"',
            'instance="backend.nutsnews.com"',
            'environment="production"',
            'deployment_environment="production"',
            'host="backend.nutsnews.com"',
            'service=~"scheduler|fetcher|canonicalizer|enrichment|approval|translation|persistence|publication"',
            'service_namespace="nutsnews"',
            'service="host"',
            "timestamp(up",
            "nutsnews_backend_worker_uplift_deployed_identity_available",
            "nutsnews_backend_worker_uplift_deployed_service_info",
            "== bool 8",
        ):
            self.assertIn(token, expression)
        args = type(
            "Args",
            (),
            {"profile": "baseline_apply", "phase": "post", "timeout": 1},
        )()
        environment = {
            "NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED": "true",
            "GRAFANA_CLOUD_PROMETHEUS_URL": "https://prom.example/api/prom/push",
            "GRAFANA_CLOUD_PROMETHEUS_USERNAME": "123",
            "GRAFANA_CLOUD_PROMETHEUS_PASSWORD": "secret",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(safety, "prometheus_scalar_query", side_effect=[3.0, 4.0]) as query,
            patch.object(safety.time, "sleep") as sleep,
        ):
            check = safety.worker_runtime_grafana_observability(args, sleeper=sleep)
        self.assertEqual(check["status"], "healthy")
        self.assertIn("passed_contract_checks=4/4", check["summary"])
        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_prometheus_query_url_is_derived_from_remote_write_url(self):
        self.assertEqual(
            safety.derive_prometheus_query_url("https://prom.example/api/prom/push"),
            "https://prom.example/api/prom/api/v1/query",
        )
        self.assertEqual(
            safety.derive_prometheus_query_url("https://prom.example/api/v1/push"),
            "https://prom.example/api/v1/query",
        )

    def test_rabbitmq_drift_json_is_classified(self):
        fixture = evidence(rabbitmq_drift=command('{"status":"pass","summary":{"high_priority_unexpected":[]}}\n'))
        check = safety.rabbitmq_drift(fixture)
        self.assertEqual(check["status"], "healthy")
        self.assertEqual(check["summary"], "high_priority_unexpected=none")

        fixture = evidence(
            rabbitmq_drift=command(
                '{"status":"fail","summary":{"high_priority_unexpected":["rabbitmq_image_digest"]}}\n',
                returncode=1,
            )
        )
        check = safety.rabbitmq_drift(fixture)
        self.assertEqual(check["status"], "critical")
        self.assertEqual(check["summary"], "high_priority_unexpected=rabbitmq_image_digest")

    def test_secret_presence_reports_names_only(self):
        with patch.dict(os.environ, {"ONE_SECRET": "present", "EMPTY_SECRET": ""}, clear=True):
            [check] = safety.secret_presence_checks(["ONE_SECRET", "EMPTY_SECRET", "MISSING_SECRET"])
        self.assertEqual(check["status"], "critical")
        self.assertEqual(check["checked_names"], ["EMPTY_SECRET", "MISSING_SECRET", "ONE_SECRET"])
        self.assertEqual(check["missing_names"], ["EMPTY_SECRET", "MISSING_SECRET"])
        self.assertNotIn("present", str(check))

    def test_caddy_config_error_blocks_baseline_apply(self):
        fixture = evidence(
            caddy_config=command("Error: adapting config using caddyfile: parsing caddyfile tokens failed\n"),
            docker_health=command("not_configured\n"),
            restore_verification=command("not_configured\n"),
        )
        args = type(
            "Args",
            (),
            {
                "public_health_url": "https://backend.nutsnews.com/healthz",
                "expected_public_health_body": "ok",
                "timeout": 1,
                "required_secret": [],
            },
        )()
        with patch.object(safety, "public_endpoint_health", return_value={"name": "public_endpoint_health", "status": "healthy", "summary": "ok"}):
            checks = safety.safety_checks(args, fixture)
        self.assertIn({"check": "caddy_config", "status": "critical"}, safety.blockers("baseline_apply", checks))

    def test_mutating_workflows_have_safety_gate_or_fixed_maintenance_runner(self):
        protected_apply = (ROOT / ".github/workflows/protected-backend-ansible-apply.yml").read_text(encoding="utf-8")
        cloudflare = (ROOT / ".github/workflows/backend-cloudflare-routing.yml").read_text(encoding="utf-8")
        maintenance = (ROOT / ".github/workflows/backend-controlled-maintenance.yml").read_text(encoding="utf-8")
        backup = (ROOT / ".github/workflows/backend-backup-maintenance.yml").read_text(encoding="utf-8")
        rabbitmq_smoke = (ROOT / ".github/workflows/backend-rabbitmq-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/backend_deployment_safety.py", protected_apply)
        self.assertIn("scripts/backend_deployment_safety.py", cloudflare)
        self.assertIn("scripts/backend_controlled_maintenance.py", maintenance)
        self.assertIn("nutsnews-backup.service", backup)
        self.assertIn("nutsnews-backup-verify.service", backup)
        self.assertIn("nutsnews-restore-drill.service", backup)
        self.assertIn("confirm_target", rabbitmq_smoke)
        self.assertIn("backend.nutsnews.com", rabbitmq_smoke)
        self.assertIn("production-backend", rabbitmq_smoke)
        self.assertIn("/usr/local/sbin/nutsnews-rabbitmq-probe smoke", rabbitmq_smoke)
        self.assertNotIn("remote_command", rabbitmq_smoke)
        self.assertIn("Reset fixed one-shot failure state", protected_apply)
        self.assertIn("sudo -n systemctl reset-failed \"$unit\"", protected_apply)
        self.assertIn("nutsnews-backup.service nutsnews-backup-verify.service nutsnews-restore-drill.service", protected_apply)
        self.assertIn("nutsnews-rabbitmq-canary.service", protected_apply)
        self.assertIn("phase=dry-run", protected_apply)
        self.assertIn("--enforce true", protected_apply)
        self.assertIn("--enforce true", cloudflare)


if __name__ == "__main__":
    unittest.main()
