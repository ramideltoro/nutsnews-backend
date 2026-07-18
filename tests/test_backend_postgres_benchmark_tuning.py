#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_postgres_benchmark_tuning


class BackendPostgresBenchmarkTuningTests(unittest.TestCase):
    def test_offline_report_is_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "benchmark.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_postgres_benchmark_tuning.main_args(["--offline", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["safe_metadata_only"])
        self.assertEqual(report["status"], "skipped_with_reason")
        self.assertIn("offline_mode_no_live_benchmarks", report["blockers"])
        self.assertGreaterEqual(len(report["benchmarks"]), 8)

    def test_missing_db_url_blocks_live_benchmark(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with redirect_stdout(StringIO()):
                exit_code = backend_postgres_benchmark_tuning.main_args([])
        self.assertEqual(exit_code, 0)

    def test_benchmark_warning_when_latency_exceeds_target(self):
        explain = '[{"Execution Time": 501.2}]'
        with mock.patch.dict("os.environ", {"NUTSNEWS_BACKEND_TARGET_DB_URL": "postgresql://redacted"}, clear=True):
            with mock.patch.object(backend_postgres_benchmark_tuning, "run_psql", return_value=(explain, None)):
                with redirect_stdout(StringIO()):
                    exit_code = backend_postgres_benchmark_tuning.main_args(["--max-query-ms", "500"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
