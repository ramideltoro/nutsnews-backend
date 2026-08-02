#!/usr/bin/env python3
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backend_deployment_safety as safety
from tests.test_backend_controlled_maintenance import command, evidence


ROOT = Path(__file__).resolve().parents[1]


class BackendDeploymentSafetyTests(unittest.TestCase):
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
        self.assertIn("sudo -n systemctl reset-failed \"\\$unit\"", protected_apply)
        self.assertIn("nutsnews-backup.service", protected_apply)
        self.assertIn("nutsnews-backup-verify.service", protected_apply)
        self.assertIn("nutsnews-restore-drill.service", protected_apply)
        self.assertIn("nutsnews-rabbitmq-canary.service", protected_apply)
        self.assertIn('if [[ "$NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED" == "false" ]]', protected_apply)
        self.assertIn("nutsnews-supabase-sync-relay.service", protected_apply)
        self.assertIn("nutsnews-supabase-sync-relay.timer", protected_apply)
        self.assertLess(
            protected_apply.index("nutsnews-supabase-sync-relay.service"),
            protected_apply.index("- name: Run deployment safety preflight"),
        )
        self.assertIn("phase=dry-run", protected_apply)
        self.assertIn("--enforce true", protected_apply)
        self.assertIn("--enforce true", cloudflare)


if __name__ == "__main__":
    unittest.main()
