#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RUNNER_PATH = Path("ansible/roles/backend_baseline/files/nutsnews_backup.py")
MATRIX_PATH = Path("docs/backend-backup-service-matrix.json")


def load_runner():
    spec = importlib.util.spec_from_file_location("nutsnews_backup", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackendBackupTests(unittest.TestCase):
    def test_service_matrix_has_required_alert_kinds_and_secret_exclusion(self):
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        self.assertIn("backup_failure", matrix["alert_kinds"])
        self.assertIn("stale_backup", matrix["alert_kinds"])
        self.assertIn("unverified_latest_snapshot", matrix["alert_kinds"])
        self.assertIn("storage_quota_warning", matrix["alert_kinds"])
        secrets = next(item for item in matrix["services"] if item["id"] == "runtime_secrets")
        self.assertEqual(secrets["backup_method"], "excluded_from_restic")
        self.assertIn("Secret values", secrets["exclusion_rationale"])
        rabbitmq = next(item for item in matrix["services"] if item["id"] == "rabbitmq_broker_state")
        self.assertNotIn("/var/lib/nutsnews/rabbitmq", rabbitmq["data_sources"])
        self.assertIn("/var/lib/nutsnews/rabbitmq-recovery", rabbitmq["data_sources"])
        self.assertIn("live_message_store_excluded", rabbitmq["backup_method"])
        self.assertIn("running-node message-store copies can be inconsistent", rabbitmq["exclusion_rationale"])

    def test_existing_backup_paths_only_returns_existing_restic_sources(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = Path(tmpdir) / "existing"
            missing = Path(tmpdir) / "missing"
            existing.mkdir()
            matrix = {
                "services": [
                    {"backup_method": "restic_paths", "data_sources": [str(existing), str(missing)]},
                    {"backup_method": "excluded_from_restic", "data_sources": ["/definitely-not-used"]},
                ]
            }
            paths = runner.existing_backup_paths(matrix)
        self.assertIn(str(existing), paths)
        self.assertNotIn(str(missing), paths)
        self.assertNotIn("/definitely-not-used", paths)

    def test_status_action_reports_missing_status_files_as_not_configured(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            args = type("Args", (), {"state_dir": Path(tmpdir)})()
            with mock.patch.object(runner, "RABBITMQ_RECOVERY_STATE_DIR", Path(tmpdir) / "rabbitmq-recovery"):
                status = runner.action_status(args)
        self.assertEqual(status["backup"]["status"], "not_configured")
        self.assertEqual(status["verification"]["status"], "not_configured")
        self.assertEqual(status["restore_drill"]["status"], "not_configured")
        self.assertEqual(status["rabbitmq_recovery"]["definition_export"]["status"], "not_configured")

    def test_restic_env_prefixes_s3_https_repository_without_mutating_parent_env(self):
        runner = load_runner()
        env = {
            "RESTIC_REPOSITORY": "https://s3.example.test/nutsnews-backup",
            "NUTSNEWS_BACKUP_RESTIC_PROVIDER": "s3",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            child_env = runner.restic_env()
            metadata = runner.repository_normalization_metadata()
            self.assertEqual(os.environ["RESTIC_REPOSITORY"], "https://s3.example.test/nutsnews-backup")
        self.assertEqual(child_env["RESTIC_REPOSITORY"], "s3:https://s3.example.test/nutsnews-backup")
        self.assertEqual(metadata["status"], "applied")

    def test_restic_status_redacts_repository_urls_from_stderr(self):
        runner = load_runner()
        text = "Fatal: create repository at https://s3.example.test/bucket/path failed"
        self.assertNotIn("s3.example.test", runner.redact_restic_text(text))
        self.assertIn("URL_REDACTED", runner.redact_restic_text(text))

    def test_snapshot_ids_match_short_and_full_restic_ids(self):
        runner = load_runner()
        full_id = "1a999564872b8b31d3ef4a7159316f3541708e6b99f9ba5fa78d53bce7af0c51"
        self.assertTrue(runner.snapshot_ids_match(full_id, "1a999564"))
        self.assertTrue(runner.snapshot_ids_match("1a999564", full_id))
        self.assertFalse(runner.snapshot_ids_match(full_id, "abcdef12"))

    def test_latest_snapshot_uses_supported_bounded_restic_flag(self):
        runner = load_runner()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps([{"short_id": "1a999564"}]),
                "stderr": "",
            },
        )()

        with mock.patch.object(runner, "run_restic", return_value=completed) as run_restic:
            snapshot_id = runner.latest_snapshot_id()

        run_restic.assert_called_once_with(["snapshots", "--json", "--latest", "1"], timeout=600)
        self.assertEqual(snapshot_id, "1a999564")

    def test_mark_backup_verified_removes_unverified_latest_snapshot_alert(self):
        runner = load_runner()
        full_id = "1a999564872b8b31d3ef4a7159316f3541708e6b99f9ba5fa78d53bce7af0c51"
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            backup_path = state_dir / runner.STATUS_FILES["backup"]
            backup_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "backup",
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "snapshot_id": full_id,
                        "finished_at_utc": "2026-07-17T00:05:00Z",
                        "last_run_at_utc": "2026-07-17T00:05:00Z",
                        "last_success_at_utc": None,
                        "alerts": [
                            {"kind": "unverified_latest_snapshot", "status": "warning"},
                            {"kind": "storage_quota_warning", "status": "not_configured"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runner.mark_backup_verified(state_dir, "1a999564", "2026-07-17T00:08:40Z")
            updated = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["latest_snapshot_verified_at_utc"], "2026-07-17T00:08:40Z")
        self.assertEqual(updated["last_success_at_utc"], "2026-07-17T00:05:00Z")
        self.assertEqual(updated["last_verified_success_at_utc"], "2026-07-17T00:08:40Z")
        self.assertEqual(updated["alerts"], [{"kind": "storage_quota_warning", "status": "not_configured"}])

    def test_verify_falls_back_to_last_healthy_backup_snapshot_from_state(self):
        runner = load_runner()
        full_id = "2fb6c729787cf16c8d2d02662e5e6723a9be6a66d000cb4b1c7596d140f53e2e"
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            backup_path = state_dir / runner.STATUS_FILES["backup"]
            backup_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "backup",
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "snapshot_id": full_id,
                        "finished_at_utc": "2026-07-17T00:05:00Z",
                        "last_run_at_utc": "2026-07-17T00:05:00Z",
                        "last_success_at_utc": None,
                        "alerts": [{"kind": "unverified_latest_snapshot", "status": "warning"}],
                    }
                ),
                encoding="utf-8",
            )
            args = type("Args", (), {"state_dir": state_dir, "read_data_subset": "1%"})()
            with mock.patch.object(runner, "latest_snapshot_id", return_value=None), mock.patch.object(
                runner, "run_restic", return_value=completed
            ) as run_restic:
                status = runner.action_verify(args)
            updated = json.loads(backup_path.read_text(encoding="utf-8"))

        run_restic.assert_called_once_with(["check", "--read-data-subset", "1%"], timeout=7200)
        self.assertEqual(status["status"], "healthy")
        self.assertEqual(status["snapshot_id"], full_id)
        self.assertEqual(status["snapshot_source"], "backup_status")
        self.assertEqual(updated["alerts"], [])
        self.assertIn("latest_snapshot_verified_at_utc", updated)
        self.assertEqual(updated["last_success_at_utc"], "2026-07-17T00:05:00Z")

    def test_first_backup_failure_records_run_without_inventing_success(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            args = type(
                "Args",
                (),
                {
                    "state_dir": Path(tmpdir),
                    "matrix": MATRIX_PATH,
                    "init_if_missing": "true",
                    "quota_warn_bytes": 0,
                },
            )()
            with (
                mock.patch.object(runner, "require_restic_env", return_value=["RESTIC_PASSWORD"]),
                mock.patch.object(runner, "existing_backup_paths", return_value=[]),
                mock.patch.object(
                    runner,
                    "utc_now",
                    side_effect=["2026-07-17T01:00:00Z", "2026-07-17T01:00:05Z"],
                ),
            ):
                status = runner.action_backup(args)

        self.assertEqual("critical", status["status"])
        self.assertEqual("2026-07-17T01:00:05Z", status["last_run_at_utc"])
        self.assertIsNone(status["last_success_at_utc"])

    def test_failed_backup_preserves_verified_success_and_advances_last_run(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            backup_path = state_dir / runner.STATUS_FILES["backup"]
            backup_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "backup",
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "snapshot_id": "1a999564",
                        "finished_at_utc": "2026-07-16T00:00:00Z",
                        "last_run_at_utc": "2026-07-16T00:00:00Z",
                        "last_success_at_utc": "2026-07-16T00:00:00Z",
                        "latest_snapshot_verified_at_utc": "2026-07-16T01:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {
                    "state_dir": state_dir,
                    "matrix": MATRIX_PATH,
                    "init_if_missing": "true",
                    "quota_warn_bytes": 0,
                },
            )()
            with (
                mock.patch.object(runner, "require_restic_env", return_value=["RESTIC_PASSWORD"]),
                mock.patch.object(runner, "existing_backup_paths", return_value=[]),
                mock.patch.object(
                    runner,
                    "utc_now",
                    side_effect=["2026-07-17T01:00:00Z", "2026-07-17T01:00:05Z"],
                ),
            ):
                status = runner.action_backup(args)
            written = json.loads(backup_path.read_text(encoding="utf-8"))

        self.assertEqual("critical", status["status"])
        self.assertEqual("2026-07-17T01:00:05Z", written["last_run_at_utc"])
        self.assertEqual("2026-07-16T00:00:00Z", written["last_success_at_utc"])

    def test_corrupt_previous_backup_state_cannot_create_stale_success(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / runner.STATUS_FILES["backup"]).write_text("not-json", encoding="utf-8")
            args = type(
                "Args",
                (),
                {
                    "state_dir": state_dir,
                    "matrix": MATRIX_PATH,
                    "init_if_missing": "true",
                    "quota_warn_bytes": 0,
                },
            )()
            with (
                mock.patch.object(runner, "require_restic_env", return_value=["RESTIC_PASSWORD"]),
                mock.patch.object(runner, "existing_backup_paths", return_value=[]),
                mock.patch.object(
                    runner,
                    "utc_now",
                    side_effect=["2026-07-17T01:00:00Z", "2026-07-17T01:00:05Z"],
                ),
            ):
                status = runner.action_backup(args)

        self.assertEqual("2026-07-17T01:00:05Z", status["last_run_at_utc"])
        self.assertIsNone(status["last_success_at_utc"])

    def test_verify_reports_unavailable_snapshot_when_restic_and_state_have_no_snapshot(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            args = type("Args", (), {"state_dir": Path(tmpdir), "read_data_subset": "1%"})()
            with mock.patch.object(runner, "latest_snapshot_id", return_value=None), mock.patch.object(
                runner, "run_restic"
            ) as run_restic:
                status = runner.action_verify(args)

        run_restic.assert_not_called()
        self.assertEqual(status["status"], "critical")
        self.assertIsNone(status["snapshot_id"])
        self.assertEqual(status["snapshot_source"], "unavailable")

    def test_restore_drill_falls_back_to_fresh_state_and_restores_the_exact_snapshot(self):
        runner = load_runner()
        full_id = "2fb6c729787cf16c8d2d02662e5e6723a9be6a66d000cb4b1c7596d140f53e2e"

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            state_dir.mkdir()
            selected = Path(tmpdir) / "selected-file"
            selected.write_text("restore fixture", encoding="utf-8")
            (state_dir / runner.STATUS_FILES["backup"]).write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "snapshot_id": full_id,
                    }
                ),
                encoding="utf-8",
            )
            args = type("Args", (), {"state_dir": state_dir})()

            def restore(command, timeout=3600):
                target = Path(command[command.index("--target") + 1])
                restored = target / str(selected).removeprefix("/")
                restored.parent.mkdir(parents=True)
                restored.write_text("restore fixture", encoding="utf-8")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with mock.patch.object(runner, "RESTORE_DRILL_CANDIDATES", (str(selected),)), mock.patch.object(
                runner, "latest_snapshot_id", return_value=None
            ), mock.patch.object(runner, "run_restic", side_effect=restore) as run_restic:
                status = runner.action_restore_drill(args)

        command = run_restic.call_args.args[0]
        self.assertEqual(command[:2], ["restore", full_id])
        self.assertEqual(status["status"], "healthy")
        self.assertEqual(status["snapshot_source"], "backup_status")

    def test_backup_workflow_has_only_fixed_actions(self):
        workflow = Path(".github/workflows/backend-backup-maintenance.yml").read_text(encoding="utf-8")
        self.assertIn("- status", workflow)
        self.assertIn("- backup", workflow)
        self.assertIn("- verify", workflow)
        self.assertIn("- restore-drill", workflow)
        self.assertIn("confirm_target", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertNotIn("remote_command", workflow)
        self.assertNotIn("command_input", workflow)

    def test_ansible_backup_env_installs_restic_provider(self):
        task = Path("ansible/roles/backend_baseline/tasks/backup.yml").read_text(encoding="utf-8")
        self.assertIn("NUTSNEWS_BACKUP_RESTIC_PROVIDER={{ backend_backup_restic_provider | quote }}", task)


if __name__ == "__main__":
    unittest.main()
