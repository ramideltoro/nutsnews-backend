#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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
            status = runner.action_status(args)
        self.assertEqual(status["backup"]["status"], "not_configured")
        self.assertEqual(status["verification"]["status"], "not_configured")
        self.assertEqual(status["restore_drill"]["status"], "not_configured")

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


if __name__ == "__main__":
    unittest.main()
