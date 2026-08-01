from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import backend_health_audit_state


class BackendHealthAuditStateTests(unittest.TestCase):
    def test_success_state_is_bounded_and_resets_failure_count(self):
        report = {
            "last_report_run_at": "2026-08-01T12:00:00Z",
            "last_report_success_at": "2026-08-01T12:00:00Z",
            "workflow": {
                "conclusion": "success",
                "critical_check_count": 0,
                "last_success_at": "2026-08-01T12:00:00Z",
                "consecutive_failure_count": 0,
            },
            "checks": [{"name": "secret-table-name", "status": "healthy"}],
        }
        state = backend_health_audit_state.build_state(
            report,
            {},
            step_outcome="success",
            generated_at="2026-08-01T12:00:01Z",
        )
        self.assertTrue(state["available"])
        self.assertEqual("success", state["conclusion"])
        self.assertEqual(0, state["consecutive_failures"])
        self.assertEqual(86400, state["expected_interval_seconds"])
        self.assertEqual("health_report_step", state["conclusion_scope"])
        rendered = json.dumps(state)
        self.assertNotIn("checks", state)
        self.assertNotIn("secret-table-name", rendered)

    def test_missing_current_report_preserves_success_and_increments_failure(self):
        previous = {
            "last_report_success_at": "2026-07-31T12:00:00Z",
            "workflow": {
                "last_success_at": "2026-07-31T12:00:00Z",
                "consecutive_failure_count": 1,
            },
        }
        state = backend_health_audit_state.build_state(
            {},
            previous,
            step_outcome="failure",
            generated_at="2026-08-01T12:00:00Z",
        )
        self.assertFalse(state["available"])
        self.assertEqual("failure", state["conclusion"])
        self.assertEqual("2026-07-31T12:00:00Z", state["last_success_at_utc"])
        self.assertEqual(2, state["consecutive_failures"])
        self.assertEqual("2026-08-01T12:00:00Z", state["last_run_at_utc"])

    def test_failed_workflow_step_overrides_a_partial_success_report(self):
        state = backend_health_audit_state.build_state(
            {
                "last_report_run_at": "2026-08-01T12:00:00Z",
                "workflow": {
                    "conclusion": "success",
                    "critical_check_count": 0,
                    "consecutive_failure_count": 0,
                },
            },
            {"workflow": {"consecutive_failure_count": 2}},
            step_outcome="failure",
            generated_at="2026-08-01T12:00:01Z",
        )
        self.assertEqual("failure", state["conclusion"])
        self.assertEqual(3, state["consecutive_failures"])

    def test_host_state_preserves_prior_success_when_artifact_is_unavailable(self):
        state = backend_health_audit_state.build_state(
            {},
            {},
            {
                "schema_version": 1,
                "safe_metadata_only": True,
                "source": "github_actions",
                "last_success_at_utc": "2026-07-30T12:00:00Z",
                "consecutive_failures": 6,
                "critical_checks": 2,
            },
            step_outcome="failure",
            generated_at="2026-08-01T12:00:00Z",
        )
        self.assertEqual("2026-07-30T12:00:00Z", state["last_success_at_utc"])
        self.assertEqual(7, state["consecutive_failures"])
        self.assertEqual(2, state["critical_checks"])

    def test_untrusted_host_state_is_not_used_as_history(self):
        state = backend_health_audit_state.build_state(
            {},
            {},
            {
                "last_success_at_utc": "2026-07-30T12:00:00Z",
                "consecutive_failures": 6,
            },
            step_outcome="failure",
            generated_at="2026-08-01T12:00:00Z",
        )
        self.assertIsNone(state["last_success_at_utc"])
        self.assertEqual(1, state["consecutive_failures"])

    def test_main_writes_minimal_state_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "report.json"
            output_path = root / "state.json"
            report_path.write_text(
                json.dumps(
                    {
                        "last_report_run_at": "2026-08-01T12:00:00Z",
                        "workflow": {
                            "conclusion": "failure",
                            "critical_check_count": 3,
                            "last_success_at": None,
                            "consecutive_failure_count": 4,
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_parse_args = backend_health_audit_state.parse_args
            try:
                backend_health_audit_state.parse_args = lambda: type(
                    "Args",
                    (),
                    {
                        "report": report_path,
                        "previous_report": None,
                        "previous_state": None,
                        "step_outcome": "failure",
                        "generated_at": "2026-08-01T12:00:01Z",
                        "output": output_path,
                    },
                )()
                with mock.patch("builtins.print"):
                    self.assertEqual(0, backend_health_audit_state.main())
            finally:
                backend_health_audit_state.parse_args = original_parse_args
            state = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(3, state["critical_checks"])
        self.assertEqual(4, state["consecutive_failures"])
        self.assertTrue(state["safe_metadata_only"])

    def test_workflow_publishes_state_even_when_report_step_fails(self):
        workflow = Path(".github/workflows/backend-health-report.yml").read_text(encoding="utf-8")
        self.assertIn("id: health_report", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("HEALTH_REPORT_STEP_OUTCOME: ${{ steps.health_report.outcome }}", workflow)
        self.assertNotIn("HEALTH_REPORT_STEP_OUTCOME: ${{ job.status }}", workflow)
        self.assertIn("--previous-state", workflow)
        self.assertIn("previous-host-health-audit-state.json", workflow)
        self.assertIn("refusing to overwrite it", workflow)
        self.assertNotIn("printf '{}\\n' > \"$previous_host_state\"", workflow)
        self.assertIn("scripts/backend_health_audit_state.py", workflow)
        self.assertIn("/var/lib/nutsnews/health-audit/last-run.json", workflow)
        self.assertIn("sudo -n install", workflow)
        self.assertIn("mktemp /var/lib/nutsnews/health-audit/.last-run.XXXXXXXX.json", workflow)
        self.assertIn("sudo -n mv -T", workflow)
        self.assertIn("--fail-on-critical", workflow)


if __name__ == "__main__":
    unittest.main()
