from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_lag_gate as gate


NOW = "2026-07-26T20:00:00Z"
MEASURED = "2026-07-26T19:59:45Z"


def relay_status(**overrides) -> dict:
    status = {
        "status": "pass",
        "checked_at_utc": MEASURED,
        "last_applied_at_utc": MEASURED,
        "source_label": "backend_postgres_primary",
        "target_label": "existing_production_supabase_standby",
        "contract_version": 1,
        "safe_metadata_only": True,
    }
    status.update(overrides)
    return status


def health_report(*, lag_seconds: int | None = 30, status: str = "healthy", blockers: list[str] | None = None, measured_at: str = MEASURED, relay: dict | None = None) -> dict:
    relay_check = {
        "name": "supabase_sync_relay_health",
        "status": status,
        "summary": "safe metadata only",
        "blockers": blockers or [],
        "lag_seconds": lag_seconds,
        "lag_critical_seconds": 30,
        "last_applied_at_utc": measured_at,
        "failed_table_count": 0,
        "timer_state": "active",
        "service_state": "inactive",
        "service_result": "success",
        "safe_metadata_only": True,
    }
    return {
        "version": 1,
        "last_report_run_at": measured_at,
        "checks": [relay_check],
        "ssh": {
            "commands": {
                "supabase_sync_relay_status": {
                    "stdout": json.dumps(relay if relay is not None else relay_status(last_applied_at_utc=measured_at)) + "\n"
                }
            }
        },
    }


def run_gate(report: dict | str, *extra_args: str):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        health_path = tmp / "health.json"
        output_path = tmp / "gate.json"
        summary_path = tmp / "summary.md"
        if isinstance(report, str):
            health_path.write_text(report, encoding="utf-8")
        else:
            health_path.write_text(json.dumps(report), encoding="utf-8")
        with redirect_stdout(StringIO()):
            exit_code = gate.main_args(
                [
                    "--health-report",
                    str(health_path),
                    "--failover-attempt-id",
                    "failover-20260726T200000Z",
                    "--now-utc",
                    NOW,
                    "--output",
                    str(output_path),
                    "--summary",
                    str(summary_path),
                    *extra_args,
                ]
            )
        return exit_code, json.loads(output_path.read_text(encoding="utf-8")), summary_path.read_text(encoding="utf-8")


class BackendSupabaseStandbyLagGateTests(unittest.TestCase):
    def test_boundary_29_seconds_passes(self):
        exit_code, result, _ = run_gate(health_report(lag_seconds=29))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["observed_lag_seconds"], 29)
        self.assertEqual(result["blockers"], [])

    def test_boundary_30_seconds_passes(self):
        exit_code, result, _ = run_gate(health_report(lag_seconds=30))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["observed_lag_seconds"], 30)

    def test_boundary_31_seconds_fails(self):
        exit_code, result, _ = run_gate(
            health_report(lag_seconds=31, status="critical", blockers=["relay_lag_exceeds_threshold"])
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("lag_exceeds_threshold", result["blockers"])
        self.assertIn("relay_unhealthy", result["blockers"])
        self.assertEqual(result["observed_lag_seconds"], 31)

    def test_enforce_returns_nonzero_on_failure(self):
        exit_code, result, _ = run_gate(health_report(lag_seconds=31), "--enforce")
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")

    def test_missing_health_check_fails_closed(self):
        report = health_report()
        report["checks"] = []
        _, result, _ = run_gate(report)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_health_telemetry_missing", result["blockers"])

    def test_stale_telemetry_fails_closed(self):
        _, result, _ = run_gate(health_report(lag_seconds=29, measured_at="2026-07-26T19:55:00Z"))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("telemetry_stale", result["blockers"])

    def test_malformed_health_report_fails_closed(self):
        _, result, _ = run_gate("{not-json")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["telemetry_malformed"])

    def test_mismatched_target_fails_closed_without_printing_target_label(self):
        _, result, _ = run_gate(health_report(relay=relay_status(target_label="other_standby_target")))
        text = json.dumps(result)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_fingerprint_mismatch", result["blockers"])
        self.assertNotIn("other_standby_target", text)
        self.assertNotIn("existing_production_supabase_standby", text)
        self.assertNotIn("backend_postgres_primary", text)

    def test_stopped_relay_fails_closed(self):
        report = health_report(lag_seconds=10, status="critical", blockers=["relay_timer_stopped"])
        report["checks"][0]["timer_state"] = "inactive"
        _, result, _ = run_gate(report)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_unhealthy", result["blockers"])
        self.assertEqual(result["relay_timer_state"], "inactive")

    def test_unavailable_relay_status_fails_closed(self):
        report = health_report(lag_seconds=10)
        report["ssh"]["commands"]["supabase_sync_relay_status"]["stdout"] = "not_configured\n"
        _, result, _ = run_gate(report)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_telemetry_unavailable", result["blockers"])

    def test_result_and_summary_are_safe_metadata_only(self):
        _, result, summary = run_gate(health_report(lag_seconds=30))
        text = json.dumps(result) + summary
        self.assertTrue(result["safe_metadata_only"])
        self.assertIn("sha256:", text)
        for forbidden in (
            "postgres://",
            "postgresql://",
            "PGPASSWORD",
            "db.",
            "supabase.co",
            "backend_postgres_primary",
            "existing_production_supabase_standby",
            "raw row",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
