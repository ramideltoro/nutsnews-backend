#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import provision_grafana_metrics


METRICS_PATH = Path("ansible/roles/backend_baseline/files/nutsnews_metrics_textfile.py")
GRAFANA_SPEC = Path("grafana/backend-metrics/dashboards.json")


def load_metrics_module():
    spec = importlib.util.spec_from_file_location("nutsnews_metrics_textfile", METRICS_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackendMetricsTests(unittest.TestCase):
    def test_grafana_dashboard_spec_passes_guardrails(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        self.assertEqual(provision_grafana_metrics.validate_spec(spec), [])
        self.assertEqual(spec["folder"]["uid"], "nutsnews-backend-ops")
        self.assertGreaterEqual(len(spec["dashboards"]), 8)

    def test_textfile_metric_label_escaping(self):
        metrics = load_metrics_module()
        rendered = metrics.metric("nutsnews_test", 1, {"unit": 'a"b\\c'})
        self.assertEqual(rendered, 'nutsnews_test{unit="a\\"b\\\\c"} 1')

    def test_textfile_exporter_writes_backup_metrics_without_secret_content(self):
        metrics = load_metrics_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "backups"
            state_dir.mkdir()
            (state_dir / "last-backup.json").write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "latest_snapshot_verified_at_utc": "2026-07-17T00:16:22Z",
                        "quota": {"status": "not_configured"},
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "last-verification.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            (state_dir / "last-restore-verification.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            with mock.patch.object(metrics, "BACKUP_STATE_DIR", state_dir), mock.patch.object(metrics, "shell", return_value="0"), mock.patch.object(metrics, "service_active", return_value=1):
                lines = metrics.collect()
        output = "\n".join(lines)
        self.assertIn('nutsnews_backend_backup_stage_healthy{stage="backup"} 1', output)
        self.assertIn("nutsnews_backend_backup_latest_snapshot_verified 1", output)
        self.assertNotIn("password", output.lower())


if __name__ == "__main__":
    unittest.main()
