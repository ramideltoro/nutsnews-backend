#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_newrelic_observability_check
from scripts import provision_newrelic_dashboards
from scripts import validate_newrelic_observability


ROOT = Path(__file__).resolve().parents[1]


class NewRelicObservabilityTests(unittest.TestCase):
    def test_taxonomy_and_dashboard_catalog_validate(self):
        self.assertEqual(validate_newrelic_observability.main(), 0)

    def test_dashboard_check_mode_needs_no_credentials(self):
        with redirect_stdout(StringIO()) as stdout:
            exit_code = provision_newrelic_dashboards.main_args(["--check"])
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "pass")
        self.assertGreaterEqual(report["dashboard_count"], 1)

    def test_dashboard_definitions_use_24h_queries_and_no_secret_terms(self):
        dashboards = provision_newrelic_dashboards.load_dashboard_files()
        provision_newrelic_dashboards.validate_catalog(dashboards)
        self.assertGreaterEqual(len(dashboards), 5)
        self.assertTrue(all(isinstance(dashboard.get("issue"), int) for dashboard in dashboards))
        catalog_text = json.dumps(dashboards).lower()
        self.assertIn("since 24 hours ago", catalog_text)
        for forbidden in provision_newrelic_dashboards.FORBIDDEN_QUERY_TERMS:
            self.assertNotIn(forbidden, catalog_text)

    def test_log_policy_documents_correlation_and_drop_rules(self):
        policy = json.loads((ROOT / "docs" / "newrelic-log-policy.json").read_text(encoding="utf-8"))
        fields = {field["name"] for field in policy["required_fields"]}
        self.assertIn("request.id", fields)
        self.assertIn("trace.id", fields)
        self.assertTrue(policy["drop_rules"])
        self.assertGreater(policy["daily_ingest_estimate_mb"]["expected"], 0)

    def test_service_levels_define_targets_and_apdex(self):
        service_levels = json.loads((ROOT / "docs" / "newrelic-service-levels.json").read_text(encoding="utf-8"))
        sli_ids = {sli["id"] for sli in service_levels["service_levels"]}
        self.assertTrue({"availability", "latency", "error_free", "freshness"}.issubset(sli_ids))
        self.assertEqual(service_levels["apdex"]["target_seconds"], 0.5)
        self.assertTrue(all(sli["target"] for sli in service_levels["service_levels"]))

    def test_missing_new_relic_credentials_fail_closed(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with redirect_stdout(StringIO()):
                exit_code = provision_newrelic_dashboards.main_args([])
        self.assertEqual(exit_code, 1)

    def test_account_id_placeholder_renders_as_integer(self):
        rendered = provision_newrelic_dashboards.render_account_id({"accountIds": ["{{ACCOUNT_ID}}"]}, 123)
        self.assertEqual(rendered["accountIds"], [123])

    def test_dashboard_provisioning_updates_existing_dashboard(self):
        env = {
            "NEW_RELIC_USER_KEY": "nr-user-key-redacted",
            "NEW_RELIC_ACCOUNT_ID": "123",
            "NEW_RELIC_REGION": "us",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch.object(provision_newrelic_dashboards, "find_dashboard_guid", return_value="dashboard-guid"):
                with mock.patch.object(provision_newrelic_dashboards, "update_dashboard", return_value="dashboard-guid") as update:
                    with mock.patch.object(provision_newrelic_dashboards, "create_dashboard") as create:
                        with redirect_stdout(StringIO()) as stdout:
                            exit_code = provision_newrelic_dashboards.main_args([])
        self.assertEqual(exit_code, 0)
        self.assertIn('"action": "updated"', stdout.getvalue())
        update.assert_called()
        create.assert_not_called()

    def test_observability_check_offline_is_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "newrelic.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_newrelic_observability_check.main_args(["--offline", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "warn")
        self.assertTrue(report["safe_metadata_only"])
        self.assertTrue(all(check["status"] == "skipped_with_reason" for check in report["checks"]))

    def test_redaction_masks_secret_like_environment_values(self):
        with mock.patch.dict("os.environ", {"NEW_RELIC_LICENSE_KEY": "1234567890abcdef"}, clear=True):
            self.assertNotIn("1234567890abcdef", backend_newrelic_observability_check.redact("key=1234567890abcdef"))


if __name__ == "__main__":
    unittest.main()
