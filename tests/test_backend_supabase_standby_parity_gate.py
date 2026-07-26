from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_parity_gate as gate


NOW = "2026-07-26T20:20:00Z"
MEASURED = "2026-07-26T20:19:45Z"


def contract() -> dict:
    return {
        "contract_id": "backend-supabase-sync-relay",
        "version": 1,
        "source_manifest": {"schema_fingerprint": "manifest-sha"},
        "source": {"label": "backend_postgres_primary"},
        "target": {
            "label": "existing_production_supabase_standby",
            "existing_production_supabase_project": True,
            "create_new_supabase_project": False,
            "create_nutsnews_standby_database": False,
        },
        "safety": {
            "backend_postgresql_remains_primary": True,
            "app_worker_writes_to_supabase_before_failover": False,
        },
        "tables": [
            {"name": "public.articles", "primary_key": ["id"]},
            {"name": "public.rss_feeds", "primary_key": ["id"]},
        ],
    }


def table_check(name: str, **overrides) -> dict:
    check = {
        "id": f"table.{name}",
        "name": name,
        "status": "pass",
        "source_count": 10,
        "target_count": 10,
        "source_row_checksum": f"{name}-digest",
        "target_row_checksum": f"{name}-digest",
        "target_lag_rows": 0,
        "checksum_source_error": None,
        "checksum_target_error": None,
    }
    check.update(overrides)
    return check


def relay_report(*, checks: list[dict] | None = None, **overrides) -> dict:
    report = {
        "status": "pass",
        "checked_at_utc": MEASURED,
        "source_label": "backend_postgres_primary",
        "target_label": "existing_production_supabase_standby",
        "safe_metadata_only": True,
        "post_sync": {
            "status": "pass",
            "failed_required_checks": [],
            "checks": checks
            if checks is not None
            else [
                table_check("public.articles"),
                table_check("public.rss_feeds"),
                {"id": "sequence.public.rss_feeds_id_seq", "status": "pass"},
            ],
        },
    }
    report.update(overrides)
    return report


def health_report(*, measured_at: str = MEASURED, relay: dict | None = None) -> dict:
    return {
        "version": 1,
        "last_report_run_at": measured_at,
        "ssh": {
            "commands": {
                "supabase_sync_relay_status": {
                    "stdout": json.dumps(relay if relay is not None else relay_report()) + "\n"
                }
            }
        },
    }


def run_gate(report: dict | str, contract_data: dict | None = None, *extra_args: str):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        contract_path = tmp / "contract.json"
        health_path = tmp / "health.json"
        output_path = tmp / "gate.json"
        summary_path = tmp / "summary.md"
        contract_path.write_text(json.dumps(contract_data or contract()), encoding="utf-8")
        if isinstance(report, str):
            health_path.write_text(report, encoding="utf-8")
        else:
            health_path.write_text(json.dumps(report), encoding="utf-8")
        with redirect_stdout(StringIO()):
            exit_code = gate.main_args(
                [
                    "--health-report",
                    str(health_path),
                    "--contract",
                    str(contract_path),
                    "--failover-attempt-id",
                    "failover-20260726T202000Z",
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


class BackendSupabaseStandbyParityGateTests(unittest.TestCase):
    def test_exact_required_table_parity_passes(self):
        exit_code, result, _ = run_gate(health_report())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["required_table_count"], 2)
        self.assertEqual(result["passed_table_count"], 2)
        self.assertEqual(result["failed_table_count"], 0)
        self.assertEqual(result["blockers"], [])

    def test_sequence_only_post_sync_failure_does_not_block_table_parity(self):
        relay = relay_report()
        relay["post_sync"]["status"] = "fail"
        relay["post_sync"]["failed_required_checks"] = ["sequence.public.rss_feeds_id_seq"]
        relay["post_sync"]["checks"].append(
            {"id": "sequence.public.rss_feeds_id_seq", "status": "fail", "reasons": ["sequence_gate_separate"]}
        )

        _, result, _ = run_gate(health_report(relay=relay))

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["post_sync_status"], "fail")
        self.assertEqual(result["failed_table_count"], 0)

    def test_added_row_fixture_fails_count_mismatch(self):
        checks = [
            table_check("public.articles", source_count=11, target_count=10, target_lag_rows=1),
            table_check("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("table_parity_failed", result["blockers"])
        articles = next(table for table in result["tables"] if table["name"] == "public.articles")
        self.assertIn("row_count_mismatch", articles["blockers"])
        self.assertIn("target_lag_rows_nonzero", articles["blockers"])

    def test_deleted_row_fixture_fails_count_mismatch(self):
        checks = [
            table_check("public.articles", source_count=9, target_count=10, target_lag_rows=-1),
            table_check("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=checks)))
        articles = next(table for table in result["tables"] if table["name"] == "public.articles")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("row_count_mismatch", articles["blockers"])

    def test_changed_row_fixture_fails_checksum_mismatch(self):
        checks = [
            table_check("public.articles", source_row_checksum="source-digest", target_row_checksum="target-digest"),
            table_check("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=checks)))
        articles = next(table for table in result["tables"] if table["name"] == "public.articles")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("row_checksum_mismatch", articles["blockers"])

    def test_missing_required_table_fails(self):
        checks = [table_check("public.articles")]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        rss = next(table for table in result["tables"] if table["name"] == "public.rss_feeds")
        self.assertIn("table_comparison_missing", rss["blockers"])

    def test_incomplete_comparison_fails(self):
        checks = [
            table_check("public.articles", source_count=None, target_row_checksum=None),
            table_check("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=checks)))
        articles = next(table for table in result["tables"] if table["name"] == "public.articles")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("table_count_missing", articles["blockers"])
        self.assertIn("table_checksum_missing", articles["blockers"])

    def test_stale_telemetry_fails_closed(self):
        _, result, _ = run_gate(health_report(measured_at="2026-07-26T20:00:00Z"))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("telemetry_stale", result["blockers"])

    def test_malformed_telemetry_fails_closed(self):
        _, result, _ = run_gate("{not-json")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["telemetry_malformed"])

    def test_mismatched_target_fails_without_printing_target_label(self):
        _, result, _ = run_gate(health_report(relay=relay_report(target_label="other_standby_target")))
        text = json.dumps(result)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_fingerprint_mismatch", result["blockers"])
        self.assertNotIn("other_standby_target", text)
        self.assertNotIn("existing_production_supabase_standby", text)
        self.assertNotIn("backend_postgres_primary", text)

    def test_unavailable_relay_status_fails_closed(self):
        report = health_report()
        report["ssh"]["commands"]["supabase_sync_relay_status"]["stdout"] = "not_configured\n"
        _, result, _ = run_gate(report)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_telemetry_unavailable", result["blockers"])

    def test_enforce_returns_nonzero_on_failure(self):
        checks = [table_check("public.articles", target_count=9), table_check("public.rss_feeds")]
        exit_code, result, _ = run_gate(health_report(relay=relay_report(checks=checks)), None, "--enforce")
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")

    def test_artifact_and_summary_are_safe_metadata_only(self):
        _, result, summary = run_gate(health_report())
        text = json.dumps(result) + summary
        self.assertTrue(result["safe_metadata_only"])
        self.assertIn("sha256:", text)
        for forbidden in (
            "postgres://",
            "postgresql://",
            "PGPASSWORD",
            "db.",
            "supabase.co",
            "raw row",
            "other_standby_target",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
