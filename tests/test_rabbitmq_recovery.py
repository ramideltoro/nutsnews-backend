#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RECOVERY_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_recovery.py"
RECOVERY_WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-recovery.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
PROTECTED_APPLY = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
BOOTSTRAP = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
RABBITMQ_TASKS = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "tasks" / "main.yml"
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
            {"exchanges": 4, "queues": 36, "retry_queues": 21, "routes": 7, "users": 16},
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
        self.assertEqual(status["current_candidate_reconciliation_drill"]["status"], "not_configured")
        self.assertEqual(status["stopped_volume_restore_drill"]["status"], "not_configured")
        self.assertIn("live /var/lib/nutsnews/rabbitmq", status["message_store_policy"])

    def test_drill_container_uses_env_file_instead_of_secret_process_args(self):
        source = RECOVERY_PATH.read_text(encoding="utf-8")
        start_block = source.split("def start_drill_container", 1)[1].split("def remove_container", 1)[0]
        self.assertIn('"--env-file"', start_block)
        self.assertNotIn('f"RABBITMQ_DEFAULT_PASS=', start_block)
        self.assertNotIn('f"RABBITMQ_ERLANG_COOKIE=', start_block)

    def test_candidate_environment_is_shadow_only_and_value_free(self):
        definition = {
            "vhost": "nutsnews-worker-uplift",
            "users": [
                {
                    "id": "persistence_consumer",
                    "stage": "persistence",
                    "username_variable": "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME",
                    "password_variable": "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD",
                }
            ],
        }
        credentials = {
            "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME": "throwaway-user",
            "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD": "throwaway-password",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.env"
            destination = root / "candidate.env"
            source.write_text(
                "\n".join(
                    [
                        "NUTSNEWS_PERSISTENCE_DATABASE_URL=postgresql://worker:database-secret@127.0.0.1/shadow",
                        "NUTSNEWS_PERSISTENCE_RABBITMQ_URL=amqp://live:live-secret@127.0.0.1/live",
                        "NUTSNEWS_PERSISTENCE_RECONCILIATION_TOKEN=reconciliation-secret",
                        "NUTSNEWS_PERSISTENCE_HTTP_PORT=18087",
                        "NUTSNEWS_PERSISTENCE_SHADOW_MODE=true",
                        "NUTSNEWS_PERSISTENCE_PRODUCTION_WRITES_ENABLED=false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            evidence = recovery.write_candidate_environment(
                source_path=source,
                destination_path=destination,
                definition=definition,
                credentials=credentials,
                stage="persistence",
                amqp_port="25672",
                http_port=28087,
            )
            candidate = recovery.parse_env(destination)
        self.assertIn("@127.0.0.1:25672/", candidate["NUTSNEWS_PERSISTENCE_RABBITMQ_URL"])
        self.assertEqual(candidate["NUTSNEWS_PERSISTENCE_HTTP_PORT"], "28087")
        self.assertEqual(candidate["NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_APPLY_ENABLED"], "true")
        evidence_text = json.dumps(evidence)
        for secret in ("database-secret", "live-secret", "reconciliation-secret", "throwaway-password"):
            self.assertNotIn(secret, evidence_text)
        self.assertFalse(evidence["production_writes_enabled"])

    def test_candidate_environment_rejects_production_writes(self):
        definition = {
            "vhost": "nutsnews-worker-uplift",
            "users": [
                {
                    "id": "persistence_consumer",
                    "stage": "persistence",
                    "username_variable": "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME",
                    "password_variable": "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD",
                }
            ],
        }
        credentials = {
            "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME": "throwaway-user",
            "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD": "throwaway-password",
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.env"
            source.write_text(
                "\n".join(
                    [
                        "NUTSNEWS_PERSISTENCE_DATABASE_URL=postgresql://shadow",
                        "NUTSNEWS_PERSISTENCE_SHADOW_MODE=true",
                        "NUTSNEWS_PERSISTENCE_PRODUCTION_WRITES_ENABLED=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "enables production writes"):
                recovery.write_candidate_environment(
                    source_path=source,
                    destination_path=Path(temp) / "candidate.env",
                    definition=definition,
                    credentials=credentials,
                    stage="persistence",
                    amqp_port="25672",
                    http_port=28087,
                )

    def test_reconciliation_candidate_evidence_hashes_identifiers(self):
        candidate = {
            "outbox_id": "42",
            "entity_id": "private-article",
            "idempotency_key": "private-idempotency",
            "pipeline_run_id": "private-pipeline",
            "created_at": "2026-07-30 12:00:00+00",
            "audit_count": "2",
        }
        evidence = recovery.reconciliation_candidate_evidence(candidate)
        text = json.dumps(evidence)
        self.assertNotIn("private-article", text)
        self.assertNotIn("private-idempotency", text)
        self.assertNotIn("private-pipeline", text)
        self.assertEqual(evidence["primary_key_range"], {"minimum": "42", "maximum": "42"})
        self.assertEqual(evidence["limit"], 1)

    def test_consumer_registration_waits_for_all_expected_queues(self):
        calls = 0

        def snapshot(**kwargs):
            nonlocal calls
            cycle = calls // 2
            calls += 1
            return {
                "queue": kwargs["queue"],
                "consumers": 0 if cycle == 0 else 1,
                "messages": 0,
            }

        with (
            mock.patch.object(recovery, "queue_snapshot", side_effect=snapshot),
            mock.patch.object(recovery.time, "sleep"),
        ):
            snapshots, ready = recovery.wait_for_expected_consumers(
                management_port="15672",
                admin_username="throwaway-admin",
                admin_password="throwaway-password",
                vhost="nutsnews-worker-uplift",
                stage_queues={"fetcher": "fetch", "publication": "publication"},
                timeout_seconds=1,
            )
        self.assertTrue(ready)
        self.assertEqual(calls, 4)
        self.assertEqual({stage: item["consumers"] for stage, item in snapshots.items()}, {
            "fetcher": 1,
            "publication": 1,
        })

    def test_recovery_workflow_has_fixed_actions_and_safe_artifacts(self):
        workflow = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
        for action in (
            "status",
            "export-definitions",
            "clean-rebuild-drill",
            "current-candidate-reconciliation-drill",
            "stopped-volume-restore-drill",
            "scheduled-check",
        ):
            self.assertIn(f"- {action}", workflow)
        self.assertIn("confirm_target", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertIn("environment: production-backend", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("sudo -n /usr/local/sbin/nutsnews-rabbitmq-recovery '$ACTION'", workflow)
        self.assertIn("backend-rabbitmq-recovery-report.json", workflow)
        self.assertIn("backend-rabbitmq-recovery-status.json", workflow)
        self.assertIn("backend-worker-runtime-post-recovery-status.json", workflow)
        self.assertNotIn("definitions.sanitized.json", workflow)
        self.assertNotIn("definitions.raw.json", workflow)
        for forbidden in ("remote_command", "shell_command", "script_body", "ansible_tags"):
            self.assertNotIn(forbidden, workflow)

    def test_backend_checks_runs_recovery_validator(self):
        checks = BACKEND_CHECKS.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/validate_worker_uplift_rabbitmq_recovery.py", checks)

    def test_recovery_helper_has_fixed_protected_deployment_scope(self):
        workflow = PROTECTED_APPLY.read_text(encoding="utf-8")
        bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        tasks = RABBITMQ_TASKS.read_text(encoding="utf-8")
        self.assertIn("deployment_scope:", workflow)
        self.assertIn("- rabbitmq-recovery-helper", workflow)
        self.assertIn("args+=(--tags worker_uplift_rabbitmq_recovery_helper)", workflow)
        self.assertIn(
            "if: inputs.run_mode == 'apply' && inputs.deployment_scope == 'full-baseline'",
            workflow,
        )
        ansible_step = workflow.split("- name: Run backend Ansible baseline", 1)[1].split(
            "- name: Run deployment safety postcheck",
            1,
        )[0]
        self.assertIn("DEPLOYMENT_SCOPE: ${{ inputs.deployment_scope }}", ansible_step)
        self.assertIn("worker_uplift_rabbitmq_recovery_helper", bootstrap)
        helper_block = tasks.split("- name: Install RabbitMQ recovery helper", 1)[1].split("\n- name:", 1)[0]
        self.assertIn("worker_uplift_rabbitmq_recovery_helper", helper_block)


if __name__ == "__main__":
    unittest.main()
