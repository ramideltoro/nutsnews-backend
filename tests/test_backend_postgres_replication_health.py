#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_postgres_replication_health


class BackendPostgresReplicationHealthTests(unittest.TestCase):
    def test_offline_report_is_safe_and_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "health.json"
            textfile = Path(tmpdir) / "health.prom"
            status = Path(tmpdir) / "status.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_postgres_replication_health.main_args(
                    [
                        "--offline",
                        "--output",
                        str(output),
                        "--textfile-output",
                        str(textfile),
                        "--status-output",
                        str(status),
                    ]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
            merged = json.loads(status.read_text(encoding="utf-8"))
            textfile_output = textfile.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["safe_metadata_only"])
        self.assertEqual(report["status"], "skipped_with_reason")
        self.assertIn("nutsnews_backend_postgres_replication_blocker_count 0", textfile_output)
        self.assertEqual(merged["replication"]["lag_status"], "not_configured")

    def test_lag_failure_is_enforced(self):
        payload = json.dumps(
            [
                {
                    "subscription": "nutsnews_backend_migration_sub",
                    "pid_present": True,
                    "received_lsn_present": True,
                    "latest_end_lsn_present": True,
                    "lag_seconds": 301,
                }
            ]
        )
        with mock.patch.dict("os.environ", {"NUTSNEWS_BACKEND_TARGET_DB_URL": "postgresql://redacted"}, clear=True):
            with mock.patch.object(backend_postgres_replication_health, "run_psql", return_value=(payload, None)):
                with redirect_stdout(StringIO()):
                    exit_code = backend_postgres_replication_health.main_args(["--max-lag-seconds", "300", "--enforce"])
        self.assertEqual(exit_code, 1)

    def test_subscription_without_source_slot_check_is_blocked(self):
        payload = json.dumps(
            [
                {
                    "subscription": "nutsnews_backend_migration_sub",
                    "pid_present": True,
                    "received_lsn_present": True,
                    "latest_end_lsn_present": True,
                    "lag_seconds": 10,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "health.json"
            with mock.patch.dict("os.environ", {"NUTSNEWS_BACKEND_TARGET_DB_URL": "postgresql://redacted"}, clear=True):
                with mock.patch.object(backend_postgres_replication_health, "run_psql", return_value=(payload, None)):
                    with redirect_stdout(StringIO()):
                        exit_code = backend_postgres_replication_health.main_args(
                            ["--output", str(output), "--enforce"]
                        )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("source_slot_check_skipped_missing_source_db_url", report["blockers"])

    def test_simulated_broken_mode_fails_with_clear_blocker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "health.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_postgres_replication_health.main_args(
                    ["--simulate-broken", "--enforce", "--output", str(output)]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("simulated_broken_replication", report["blockers"])
        self.assertEqual(report["replication"]["slot_status"], "inactive")


if __name__ == "__main__":
    unittest.main()
