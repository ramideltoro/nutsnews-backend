#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERY_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_recovery.py"
RECOVERY_WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-recovery.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
SPEC = importlib.util.spec_from_file_location("nutsnews_rabbitmq_recovery", RECOVERY_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load recovery module from {RECOVERY_PATH}")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RabbitMQRecoveryTests(unittest.TestCase):
    def test_sanitize_definitions_redacts_password_hashes(self):
        sanitized, removed = recovery.sanitize_definitions(
            {
                "users": [
                    {
                        "name": "worker",
                        "password_hash": "hash-secret",
                        "password_hashing_algorithm": "rabbit_password_hashing_sha256",
                        "tags": [],
                    }
                ],
                "queues": [],
            }
        )
        self.assertEqual(removed, 2)
        text = json.dumps(sanitized)
        self.assertNotIn("hash-secret", text)
        self.assertIn("<redacted>", text)
        self.assertFalse(sanitized["x_nutsnews_sanitized"]["raw_export_retained"])

    def test_topology_counts_match_worker_uplift_definition_shape(self):
        definition = json.loads(
            (ROOT / "ansible/roles/backend_rabbitmq/templates/worker-uplift-topology.json.j2")
            .read_text(encoding="utf-8")
            .replace("{{ backend_rabbitmq_vhost }}", "nutsnews-worker-uplift")
        )
        self.assertEqual(
            recovery.topology_counts(definition),
            {"exchanges": 3, "queues": 35, "retry_queues": 21, "routes": 7, "users": 16},
        )

    def test_generated_drill_environment_uses_throwaway_secret_values(self):
        definition = {
            "vhost": "nutsnews-worker-uplift",
            "users": [
                {
                    "id": "break_glass_admin",
                    "username_variable": "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME",
                    "password_variable": "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
                },
                {
                    "id": "fetcher_consumer",
                    "username_variable": "RABBITMQ_FETCHER_CONSUMER_USERNAME",
                    "password_variable": "RABBITMQ_FETCHER_CONSUMER_PASSWORD",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            env_path, credentials_path, admin_username, _ = recovery.generated_drill_environment(definition, Path(temp))
            env_values = recovery.parse_env(env_path)
            credential_values = recovery.parse_env(credentials_path)
        self.assertEqual(admin_username, "nutsnews_recovery_admin")
        self.assertEqual(credential_values["RABBITMQ_BREAK_GLASS_ADMIN_USERNAME"], admin_username)
        self.assertEqual(credential_values["RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD"], env_values["RABBITMQ_DEFAULT_PASS"])
        self.assertTrue(credential_values["RABBITMQ_FETCHER_CONSUMER_USERNAME"].startswith("drill_"))
        self.assertNotEqual(credential_values["RABBITMQ_FETCHER_CONSUMER_PASSWORD"], env_values["RABBITMQ_DEFAULT_PASS"])

    def test_status_reports_missing_evidence_as_not_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            args = type("Args", (), {"state_dir": Path(temp)})()
            status = recovery.action_status(args)
        self.assertEqual(status["definition_export"]["status"], "not_configured")
        self.assertEqual(status["clean_rebuild_drill"]["status"], "not_configured")
        self.assertEqual(status["stopped_volume_restore_drill"]["status"], "not_configured")
        self.assertIn("live /var/lib/nutsnews/rabbitmq", status["message_store_policy"])

    def test_drill_container_uses_env_file_instead_of_secret_process_args(self):
        source = RECOVERY_PATH.read_text(encoding="utf-8")
        start_block = source.split("def start_drill_container", 1)[1].split("def remove_container", 1)[0]
        self.assertIn('"--env-file"', start_block)
        self.assertNotIn('f"RABBITMQ_DEFAULT_PASS=', start_block)
        self.assertNotIn('f"RABBITMQ_ERLANG_COOKIE=', start_block)

    def test_recovery_workflow_has_fixed_actions_and_safe_artifacts(self):
        workflow = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
        for action in ("status", "export-definitions", "clean-rebuild-drill", "stopped-volume-restore-drill", "scheduled-check"):
            self.assertIn(f"- {action}", workflow)
        self.assertIn("confirm_target", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertIn("environment: production-backend", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery '$ACTION'", workflow)
        self.assertIn("backend-rabbitmq-recovery-report.json", workflow)
        self.assertIn("backend-rabbitmq-recovery-status.json", workflow)
        self.assertNotIn("definitions.sanitized.json", workflow)
        self.assertNotIn("definitions.raw.json", workflow)
        for forbidden in ("remote_command", "shell_command", "script_body", "ansible_tags"):
            self.assertNotIn(forbidden, workflow)

    def test_backend_checks_runs_recovery_validator(self):
        checks = BACKEND_CHECKS.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/validate_worker_uplift_rabbitmq_recovery.py", checks)


if __name__ == "__main__":
    unittest.main()
