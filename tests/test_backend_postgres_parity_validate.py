#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_postgres_parity_validate


def manifest(item: dict) -> dict:
    return {
        "version": 1,
        "required_objects": [item],
    }


def table_item(*, live_tolerance: bool) -> dict:
    validation = {
        "method": "row_count_latest_timestamp",
        "query": "select count(*)::bigint from public.worker_runs",
        "sensitivity": "aggregate_only",
    }
    if live_tolerance:
        validation["live_replication_tolerance"] = {
            "enabled": True,
            "method": "source_count_watermark",
        }
    return {
        "id": "table.public.worker_runs",
        "object_type": "table",
        "validation": validation,
    }


class BackendPostgresParityValidateTests(unittest.TestCase):
    def test_exact_row_count_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            output = Path(tmpdir) / "parity.json"
            manifest_path.write_text(json.dumps(manifest(table_item(live_tolerance=False))), encoding="utf-8")
            with mock.patch.dict("os.environ", {"SRC": "postgresql://source", "TGT": "postgresql://target"}, clear=True):
                with mock.patch.object(
                    backend_postgres_parity_validate,
                    "run_psql",
                    side_effect=[("100", None), ("99", None)],
                ):
                    with redirect_stdout(StringIO()):
                        exit_code = backend_postgres_parity_validate.main_args(
                            [
                                "--manifest",
                                str(manifest_path),
                                "--source-db-url-env",
                                "SRC",
                                "--target-db-url-env",
                                "TGT",
                                "--output",
                                str(output),
                                "--enforce",
                            ]
                        )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["checks"][0]["status"], "fail")

    def test_live_replication_watermark_passes_after_target_reaches_source_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            output = Path(tmpdir) / "parity.json"
            manifest_path.write_text(json.dumps(manifest(table_item(live_tolerance=True))), encoding="utf-8")
            with mock.patch.dict("os.environ", {"SRC": "postgresql://source", "TGT": "postgresql://target"}, clear=True):
                with mock.patch.object(
                    backend_postgres_parity_validate,
                    "run_psql",
                    side_effect=[
                        ("100", None),
                        ("98", None),
                        ("102", None),
                        ("100", None),
                        ("103", None),
                    ],
                ):
                    with redirect_stdout(StringIO()):
                        exit_code = backend_postgres_parity_validate.main_args(
                            [
                                "--manifest",
                                str(manifest_path),
                                "--source-db-url-env",
                                "SRC",
                                "--target-db-url-env",
                                "TGT",
                                "--output",
                                str(output),
                                "--live-replication-wait-seconds",
                                "1",
                                "--live-replication-poll-interval-seconds",
                                "0",
                                "--enforce",
                            ]
                        )
            report = json.loads(output.read_text(encoding="utf-8"))
        check = report["checks"][0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["source_baseline_value"], "100")
        self.assertEqual(check["target_value"], "100")
        self.assertEqual(check["validation_method"], "row_count_live_replication_watermark")


if __name__ == "__main__":
    unittest.main()
