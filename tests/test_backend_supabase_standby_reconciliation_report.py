from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_reconciliation_report as report


ATTEMPT_ID = "reconcile-issue501"
REVISION = "a" * 40
SOURCE_FP = "sha256:source"
TARGET_FP = "sha256:target"
MANIFEST_FP = "sha256:manifest"
RELAY_FP = "sha256:relay"
MEASURED_AT = "2026-07-27T05:00:00Z"
EXPIRES_AT = "2026-07-27T05:05:00Z"
NOW = "2026-07-27T05:01:00Z"


def base_gate(issue: str) -> dict:
    return {
        "status": "PASS",
        "issue": issue,
        "epic": report.GATE_EPIC,
        "failover_attempt_id": ATTEMPT_ID,
        "measured_at_utc": MEASURED_AT,
        "expires_at_utc": EXPIRES_AT,
        "source_fingerprint": SOURCE_FP,
        "target_fingerprint": TARGET_FP,
        "manifest_fingerprint": MANIFEST_FP,
        "relay_contract_fingerprint": RELAY_FP,
        "blockers": [],
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }


def passing_parity() -> dict:
    gate = base_gate("ramideltoro/nutsnews#523")
    gate.update(
        {
            "required_table_count": 2,
            "passed_table_count": 2,
            "failed_table_count": 0,
            "tables": [
                {
                    "name": "public.articles",
                    "status": "PASS",
                    "source_count": 12,
                    "target_count": 12,
                    "target_lag_rows": 0,
                    "blockers": [],
                },
                {
                    "name": "public.rss_feeds",
                    "status": "PASS",
                    "source_count": 3,
                    "target_count": 3,
                    "target_lag_rows": 0,
                    "blockers": [],
                },
            ],
        }
    )
    return gate


def passing_schema() -> dict:
    gate = base_gate("ramideltoro/nutsnews#524")
    gate.update(
        {
            "candidate_application_revision": REVISION,
            "repository_revision": REVISION,
            "required_table_count": 2,
            "required_sequence_count": 1,
            "passed_identity_count": 2,
            "failed_identity_count": 0,
            "passed_sequence_binding_count": 1,
            "failed_sequence_binding_count": 0,
            "schema": {
                "status": "PASS",
                "blockers": [],
            },
        }
    )
    return gate


def passing_sequence() -> dict:
    gate = base_gate("ramideltoro/nutsnews#525")
    gate.update(
        {
            "repository_revision": REVISION,
            "required_sequence_count": 1,
            "passed_sequence_count": 1,
            "failed_sequence_count": 0,
        }
    )
    return gate


def run_report(parity: dict, schema: dict, sequence: dict, extra_args: list[str] | None = None):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        parity_path = root / "parity.json"
        schema_path = root / "schema.json"
        sequence_path = root / "sequence.json"
        output_path = root / "report.json"
        parity_path.write_text(json.dumps(parity), encoding="utf-8")
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        sequence_path.write_text(json.dumps(sequence), encoding="utf-8")
        argv = [
            "--parity-gate",
            str(parity_path),
            "--schema-gate",
            str(schema_path),
            "--sequence-gate",
            str(sequence_path),
            "--reconciliation-attempt-id",
            ATTEMPT_ID,
            "--repository-revision",
            REVISION,
            "--now-utc",
            NOW,
            "--output",
            str(output_path),
            *(extra_args or []),
        ]
        with redirect_stdout(StringIO()):
            exit_code = report.main_args(argv)
        return exit_code, json.loads(output_path.read_text(encoding="utf-8"))


class BackendSupabaseStandbyReconciliationReportTests(unittest.TestCase):
    def test_passing_gates_emit_pass_report(self) -> None:
        exit_code, result = run_report(passing_parity(), passing_schema(), passing_sequence())

        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", result["status"])
        self.assertEqual([], result["failed_required_checks"])
        self.assertEqual([], result["blockers"])
        self.assertEqual(
            [
                "ramideltoro/nutsnews#523",
                "ramideltoro/nutsnews#524",
                "ramideltoro/nutsnews#525",
            ],
            result["consumed_gate_issues"],
        )
        self.assertTrue(result["target_is_existing_production_supabase"])
        self.assertFalse(result["create_new_supabase_project"])
        self.assertFalse(result["create_nutsnews_standby_database"])
        self.assertFalse(result["app_worker_writes_to_supabase_before_failover"])
        self.assertTrue(result["safe_metadata_only"])

    def test_failed_required_table_blocks_reconciliation(self) -> None:
        parity = passing_parity()
        parity["status"] = "FAIL"
        parity["failed_table_count"] = 1
        parity["blockers"] = ["table_parity_failed"]
        parity["tables"][0]["status"] = "FAIL"
        parity["tables"][0]["blockers"] = ["row_checksum_mismatch"]

        exit_code, result = run_report(parity, passing_schema(), passing_sequence())

        self.assertEqual(0, exit_code)
        self.assertEqual("FAIL", result["status"])
        self.assertIn("parity_gate_failed", result["blockers"])
        self.assertIn("required_reconciliation_check_failed", result["blockers"])
        self.assertIn("required-table-row-count-parity", result["failed_required_checks"])
        self.assertIn("required-table-row-checksum-parity", result["failed_required_checks"])
        failed_table = result["gates"]["parity"]["tables"][0]
        self.assertEqual(["row_checksum_mismatch"], failed_table["blockers"])

    def test_mismatched_attempt_fails_closed(self) -> None:
        schema = passing_schema()
        schema["failover_attempt_id"] = "other-attempt"

        _, result = run_report(passing_parity(), schema, passing_sequence())

        self.assertEqual("FAIL", result["status"])
        self.assertIn("schema_gate_attempt_mismatch", result["blockers"])

    def test_expired_gate_fails_closed(self) -> None:
        sequence = passing_sequence()
        sequence["expires_at_utc"] = "2026-07-27T04:59:00Z"

        _, result = run_report(passing_parity(), passing_schema(), sequence)

        self.assertEqual("FAIL", result["status"])
        self.assertIn("sequence_gate_expired", result["blockers"])

    def test_enforce_returns_nonzero_on_failure(self) -> None:
        parity = passing_parity()
        parity["safe_metadata_only"] = False

        exit_code, result = run_report(parity, passing_schema(), passing_sequence(), ["--enforce"])

        self.assertEqual(1, exit_code)
        self.assertEqual("FAIL", result["status"])
        self.assertIn("parity_gate_not_safe_metadata", result["blockers"])


if __name__ == "__main__":
    unittest.main()
