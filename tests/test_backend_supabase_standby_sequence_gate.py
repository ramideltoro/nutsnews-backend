from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_sequence_gate as gate


NOW = "2026-07-26T21:20:00Z"
MEASURED = "2026-07-26T21:19:45Z"
MANIFEST_FINGERPRINT = "f" * 64


def contract() -> dict:
    return {
        "contract_id": "backend-supabase-sync-relay",
        "version": 1,
        "source_manifest": {"schema_fingerprint": MANIFEST_FINGERPRINT},
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
        "sequences": [
            {"name": "public.rss_feeds_id_seq", "table": "public.rss_feeds", "column": "id"},
        ],
    }


def sequence_check(**overrides) -> dict:
    check = {
        "id": "sequence.public.rss_feeds_id_seq",
        "name": "public.rss_feeds_id_seq",
        "table": "public.rss_feeds",
        "column": "id",
        "status": "pass",
        "reasons": [],
        "source_last_value": 12,
        "source_next_value": 13,
        "source_is_called": True,
        "source_increment_by": 1,
        "source_sequence_min_value": 1,
        "source_sequence_max_value": 9223372036854775807,
        "source_sequence_cycle": False,
        "source_owned_by_table": "public.rss_feeds",
        "source_owned_by_column": "id",
        "source_owned_by_count": 1,
        "source_expected_binding_matches": True,
        "source_max_id": 12,
        "target_last_value": 12,
        "target_next_value": 13,
        "target_is_called": True,
        "target_increment_by": 1,
        "target_sequence_min_value": 1,
        "target_sequence_max_value": 9223372036854775807,
        "target_sequence_cycle": False,
        "target_owned_by_table": "public.rss_feeds",
        "target_owned_by_column": "id",
        "target_owned_by_count": 1,
        "target_expected_binding_matches": True,
        "target_max_id": 12,
        "sensitivity": "sequence_metadata_only",
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
            "checks": checks if checks is not None else [sequence_check()],
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
                    "--repository-revision",
                    "b" * 40,
                    "--failover-attempt-id",
                    "failover-20260726T212000Z",
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


class BackendSupabaseStandbySequenceGateTests(unittest.TestCase):
    def test_safe_sequence_fixtures_pass_without_nextval_or_mutation(self):
        exit_code, result, _ = run_gate(health_report())
        script = Path(gate.__file__).read_text(encoding="utf-8").lower()

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["required_sequence_count"], 1)
        self.assertEqual(result["passed_sequence_count"], 1)
        self.assertEqual(result["failed_sequence_count"], 0)
        self.assertEqual(result["blockers"], [])
        self.assertNotIn("nextval", script)
        self.assertNotIn("setval", script)

    def test_behind_max_id_fixture_fails(self):
        check = sequence_check(target_next_value=12, target_max_id=12, status="fail", reasons=["target_next_value_not_above_target_max_id"])
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("sequence_safety_failed", result["blockers"])
        self.assertIn("target_next_value_not_above_target_max_id", result["sequences"][0]["blockers"])

    def test_behind_source_next_fixture_fails(self):
        check = sequence_check(
            source_last_value=20,
            source_next_value=21,
            target_last_value=19,
            target_next_value=20,
            status="fail",
            reasons=["target_next_value_lt_source_next_value"],
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_next_value_lt_source_next_value", result["sequences"][0]["blockers"])

    def test_missing_sequence_fails(self):
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("sequence_check_missing", result["sequences"][0]["blockers"])

    def test_misbound_sequence_fails(self):
        check = sequence_check(
            target_owned_by_table="public.other_table",
            target_expected_binding_matches=False,
            status="fail",
            reasons=["target_sequence_misbound"],
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_sequence_misbound", result["sequences"][0]["blockers"])

    def test_unowned_sequence_fails(self):
        check = sequence_check(
            target_owned_by_count=0,
            target_owned_by_table=None,
            target_owned_by_column=None,
            target_expected_binding_matches=False,
            status="fail",
            reasons=["target_sequence_unowned"],
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_sequence_unowned", result["sequences"][0]["blockers"])

    def test_cycled_sequence_fails(self):
        check = sequence_check(target_sequence_cycle=True, status="fail", reasons=["target_sequence_cycle_enabled"])
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_sequence_cycle_enabled", result["sequences"][0]["blockers"])

    def test_exhausted_sequence_fails(self):
        check = sequence_check(
            target_next_value=13,
            target_sequence_max_value=12,
            status="fail",
            reasons=["target_sequence_exhausted"],
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_sequence_exhausted", result["sequences"][0]["blockers"])

    def test_incomplete_report_fixture_fails(self):
        check = sequence_check()
        del check["target_next_value"]
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_next_value_missing", result["sequences"][0]["blockers"])

    def test_empty_table_sequence_semantics_pass(self):
        check = sequence_check(
            source_last_value=1,
            source_next_value=1,
            source_is_called=False,
            source_max_id=0,
            target_last_value=1,
            target_next_value=1,
            target_is_called=False,
            target_max_id=0,
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sequences"][0]["target_next_value"], 1)

    def test_never_called_sequence_semantics_fail_when_next_collides(self):
        check = sequence_check(
            target_last_value=1,
            target_next_value=1,
            target_is_called=False,
            target_max_id=1,
            status="fail",
            reasons=["target_next_value_not_above_target_max_id"],
        )
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_next_value_not_above_target_max_id", result["sequences"][0]["blockers"])

    def test_unexpected_increment_configuration_fails(self):
        check = sequence_check(target_increment_by=5, status="fail", reasons=["target_sequence_increment_not_one"])
        _, result, _ = run_gate(health_report(relay=relay_report(checks=[check])))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_sequence_increment_not_one", result["sequences"][0]["blockers"])
        self.assertIn("sequence_increment_mismatch", result["sequences"][0]["blockers"])

    def test_stale_telemetry_fails_closed(self):
        _, result, _ = run_gate(health_report(measured_at="2026-07-26T21:00:00Z"))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("telemetry_stale", result["blockers"])

    def test_malformed_telemetry_fails_closed(self):
        _, result, _ = run_gate("{not-json")

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["telemetry_malformed"])

    def test_mismatched_target_fails_without_printing_target_label(self):
        relay = relay_report(target_label="unexpected_target")
        _, result, _ = run_gate(health_report(relay=relay))
        text = json.dumps(result)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_fingerprint_mismatch", result["blockers"])
        self.assertNotIn("existing_production_supabase_standby", text)
        self.assertNotIn("unexpected_target", text)

    def test_unavailable_relay_status_fails_closed(self):
        report = health_report()
        report["ssh"]["commands"]["supabase_sync_relay_status"]["stdout"] = "not_configured\n"
        _, result, _ = run_gate(report)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_telemetry_unavailable", result["blockers"])

    def test_enforce_returns_nonzero_on_failure(self):
        exit_code, result, _ = run_gate(health_report(relay=relay_report(checks=[])), None, "--enforce")

        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")

    def test_artifact_and_summary_are_safe_metadata_only(self):
        _, result, summary = run_gate(health_report())
        combined = json.dumps(result) + summary

        forbidden = [
            "postgres://",
            "postgresql://",
            "password=",
            "PGPASSWORD",
            "service_role",
            "sb_secret_",
            "sb_publishable_",
            "raw row",
        ]
        for token in forbidden:
            self.assertNotIn(token, combined)
        self.assertTrue(result["safe_metadata_only"])
        self.assertIn("safe_metadata_only", combined)


if __name__ == "__main__":
    unittest.main()
