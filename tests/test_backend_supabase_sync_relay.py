from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_supabase_sync_relay as relay


SCHEMA_JSON = json.dumps({"columns": [], "constraints": [], "indexes": []})
CONTRACT_JSON = json.dumps(
    [
        {
            "legacy_schema_version": "1",
            "migration_head": "abc",
            "expected_schema_fingerprint": "fingerprint",
        }
    ]
)
IDENTITY_JSON = json.dumps(
    {
        "relation_kind": "r",
        "replica_identity": "d",
        "primary_key": ["id"],
    }
)


def contract() -> dict:
    return {
        "version": 1,
        "manifest_schema_fingerprint": "f" * 64,
        "tables": [
            {
                "name": "public.worker_runs",
                "primary_key": ["id"],
                "replica_identity": {"type": "primary_key", "columns": ["id"]},
            }
        ],
        "sequences": [
            {
                "name": "public.worker_runs_id_seq",
                "table": "public.worker_runs",
                "column": "id",
            }
        ],
        "apply_order": ["public.worker_runs"],
    }


def run_with_contract(
    contract_data: dict,
    argv: list[str],
    env: dict[str, str],
    *,
    previous_report: dict | None = None,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        contract_path = Path(tmpdir) / "relay.json"
        output_path = Path(tmpdir) / "report.json"
        contract_path.write_text(json.dumps(contract_data), encoding="utf-8")
        if previous_report is not None:
            output_path.write_text(json.dumps(previous_report), encoding="utf-8")
        with mock.patch.dict("os.environ", env, clear=True):
            with redirect_stdout(StringIO()):
                exit_code = relay.main_args(
                    [
                        "--contract",
                        str(contract_path),
                        "--source-db-url-env",
                        "SRC",
                        "--target-db-url-env",
                        "TGT",
                        "--output",
                        str(output_path),
                        *argv,
                    ]
                )
        return exit_code, json.loads(output_path.read_text(encoding="utf-8"))


class BackendSupabaseSyncRelayTests(unittest.TestCase):
    def test_offline_report_is_safe_metadata_only(self) -> None:
        exit_code, report = run_with_contract(contract(), ["--offline", "--enforce"], {})

        self.assertEqual(0, exit_code)
        self.assertEqual("skipped_with_reason", report["status"])
        self.assertTrue(report["safe_metadata_only"])
        self.assertTrue(report["backend_postgresql_remains_primary"])
        self.assertFalse(report["backend_postgres_public_5432_allowed"])
        self.assertFalse(report["create_new_supabase_project"])
        self.assertFalse(report["create_nutsnews_standby_database"])
        self.assertFalse(report["app_worker_writes_to_supabase_before_failover"])
        self.assertFalse(report["app_worker_supabase_write_credentials_injected"])
        self.assertNotIn("postgresql://", json.dumps(report).lower())

    def test_output_report_is_world_readable_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            contract_path = Path(tmpdir) / "relay.json"
            output_path = Path(tmpdir) / "report.json"
            contract_path.write_text(json.dumps(contract()), encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                with redirect_stdout(StringIO()):
                    exit_code = relay.main_args(
                        [
                            "--contract",
                            str(contract_path),
                            "--output",
                            str(output_path),
                            "--offline",
                            "--enforce",
                        ]
                    )
                mode = output_path.stat().st_mode & 0o777

        self.assertEqual(0, exit_code)
        self.assertEqual(0o644, mode)

    def test_dry_run_passes_when_schema_and_identity_pass(self) -> None:
        side_effect = [
            (SCHEMA_JSON, None),
            (SCHEMA_JSON, None),
            (CONTRACT_JSON, None),
            (CONTRACT_JSON, None),
            (IDENTITY_JSON, None),
            (IDENTITY_JSON, None),
        ]
        with mock.patch.object(relay.reconcile, "run_psql", side_effect=side_effect):
            exit_code, report = run_with_contract(
                contract(),
                ["--mode", "dry-run", "--enforce"],
                {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", report["status"])
        self.assertEqual("pass", report["preflight"]["status"])
        self.assertEqual("not_run", report["sync"]["status"])
        schema_check = next(check for check in report["preflight"]["checks"] if check["id"] == "schema-fingerprint")
        self.assertEqual("f" * 64, schema_check["manifest_schema_fingerprint"])

    def test_sync_once_blocks_before_mutation_when_schema_preflight_fails(self) -> None:
        side_effect = [
            ("source-schema", None),
            ("target-schema", None),
            (CONTRACT_JSON, None),
            (CONTRACT_JSON, None),
            (IDENTITY_JSON, None),
            (IDENTITY_JSON, None),
        ]
        with mock.patch.object(relay.reconcile, "run_psql", side_effect=side_effect):
            with mock.patch.object(relay.reconcile, "apply_table_backfill") as apply_table_backfill:
                exit_code, report = run_with_contract(
                    contract(),
                    ["--mode", "sync-once", "--enforce"],
                    {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
                )

        self.assertEqual(1, exit_code)
        self.assertEqual("fail", report["status"])
        self.assertEqual("blocked", report["sync"]["status"])
        self.assertEqual("preflight_failed", report["sync"]["reason"])
        apply_table_backfill.assert_not_called()

    def test_sync_once_applies_tables_and_sequences_then_validates_parity(self) -> None:
        with mock.patch.object(relay, "relay_preflight", return_value={"status": "pass", "failed_required_checks": [], "checks": []}):
            with mock.patch.object(
                relay.reconcile,
                "apply_table_backfill",
                return_value={"table": "public.worker_runs", "status": "applied", "rows_seen": 2, "sensitivity": "counts_only"},
            ) as apply_table_backfill:
                with mock.patch.object(
                    relay.reconcile,
                    "apply_sequence_safety",
                    return_value={"sequence": "public.worker_runs_id_seq", "status": "set", "sensitivity": "sequence_metadata_only"},
                ) as apply_sequence_safety:
                    with mock.patch.object(
                        relay.reconcile,
                        "validate_standby",
                        return_value={
                            "status": "pass",
                            "failed_required_checks": [],
                            "checks": [
                                {
                                    "id": "table.public.worker_runs",
                                    "status": "pass",
                                    "target_lag_rows": 0,
                                }
                            ],
                        },
                    ):
                        exit_code, report = run_with_contract(
                            contract(),
                            ["--mode", "sync-once", "--enforce"],
                            {
                                "SRC": "postgresql://source:pw@127.0.0.1/postgres",
                                "TGT": "postgresql://target:pw@127.0.0.1/postgres",
                            },
                        )

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", report["status"])
        self.assertEqual(2, report["schema_version"])
        self.assertEqual("applied", report["sync"]["status"])
        self.assertIn("completed_at_utc", report)
        self.assertIn("last_applied_at_utc", report)
        self.assertEqual(report["completed_at_utc"], report["last_success_at_utc"])
        self.assertEqual(
            {
                "complete": True,
                "expected_table_count": 1,
                "failed_table_count": 0,
                "max_table_lag_rows": 0,
                "safe_metadata_only": True,
                "validated_table_count": 1,
            },
            report["validation_summary"],
        )
        self.assertEqual(["insert", "update", "delete", "sequence-readiness"], report["sync"]["supported_change_types"])
        apply_table_backfill.assert_called_once()
        apply_sequence_safety.assert_called_once()

    def test_failed_run_preserves_last_success_and_last_applied_history(self) -> None:
        previous = {
            "schema_version": 2,
            "status": "pass",
            "mode": "sync-once",
            "safe_metadata_only": True,
            "checked_at_utc": "2026-08-01T10:00:00Z",
            "completed_at_utc": "2026-08-01T10:00:01Z",
            "finished_at_utc": "2026-08-01T10:00:01Z",
            "last_success_at_utc": "2026-08-01T10:00:01Z",
            "last_applied_at_utc": "2026-08-01T10:00:01Z",
            "sync": {"status": "applied"},
            "post_sync": {"status": "pass"},
        }
        with mock.patch.object(
            relay,
            "relay_preflight",
            return_value={"status": "fail", "failed_required_checks": ["schema-fingerprint"], "checks": []},
        ):
            exit_code, report = run_with_contract(
                contract(),
                ["--mode", "sync-once", "--enforce"],
                {"SRC": "postgresql://source", "TGT": "postgresql://target"},
                previous_report=previous,
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("fail", report["status"])
        self.assertEqual(previous["last_success_at_utc"], report["last_success_at_utc"])
        self.assertEqual(previous["last_applied_at_utc"], report["last_applied_at_utc"])
        self.assertIn("finished_at_utc", report)

    def test_passing_dry_run_does_not_invent_relay_success_history(self) -> None:
        with mock.patch.object(
            relay,
            "relay_preflight",
            return_value={"status": "pass", "failed_required_checks": [], "checks": []},
        ):
            exit_code, report = run_with_contract(
                contract(),
                ["--mode", "dry-run", "--enforce"],
                {"SRC": "postgresql://source", "TGT": "postgresql://target"},
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", report["status"])
        self.assertEqual("not_run", report["sync"]["status"])
        self.assertNotIn("last_success_at_utc", report)
        self.assertNotIn("last_applied_at_utc", report)
        self.assertFalse(report["validation_summary"]["complete"])

    def test_skipped_post_sync_checks_do_not_count_as_failed_tables(self) -> None:
        summary = relay.relay_validation_summary(
            {
                "status": "skipped_with_reason",
                "checks": [
                    {
                        "id": "table.public.worker_runs",
                        "status": "skipped_with_reason",
                    }
                ],
            },
            1,
        )

        self.assertFalse(summary["complete"])
        self.assertIsNone(summary["failed_table_count"])
        self.assertIsNone(summary["max_table_lag_rows"])

    def test_manifest_without_primary_key_or_primary_replica_identity_fails_closed(self) -> None:
        relay_contract = contract()
        relay_contract["tables"][0]["primary_key"] = []
        relay_contract["tables"][0]["replica_identity"] = {"type": "full"}
        side_effect = [
            (SCHEMA_JSON, None),
            (SCHEMA_JSON, None),
            (CONTRACT_JSON, None),
            (CONTRACT_JSON, None),
        ]
        with mock.patch.object(relay.reconcile, "run_psql", side_effect=side_effect):
            exit_code, report = run_with_contract(
                relay_contract,
                ["--mode", "sync-once", "--enforce"],
                {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            )

        self.assertEqual(1, exit_code)
        self.assertIn("manifest-identity.public.worker_runs", report["preflight"]["failed_required_checks"])
        identity_check = next(check for check in report["preflight"]["checks"] if check["id"] == "manifest-identity.public.worker_runs")
        self.assertIn("manifest_table_primary_key_missing", identity_check["reasons"])
        self.assertIn("manifest_replica_identity_not_primary_key", identity_check["reasons"])

    def test_live_primary_key_mismatch_fails_closed(self) -> None:
        target_identity = json.dumps({"relation_kind": "r", "replica_identity": "d", "primary_key": ["other_id"]})
        side_effect = [
            (SCHEMA_JSON, None),
            (SCHEMA_JSON, None),
            (CONTRACT_JSON, None),
            (CONTRACT_JSON, None),
            (IDENTITY_JSON, None),
            (target_identity, None),
        ]
        with mock.patch.object(relay.reconcile, "run_psql", side_effect=side_effect):
            exit_code, report = run_with_contract(
                contract(),
                ["--mode", "sync-once", "--enforce"],
                {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            )

        self.assertEqual(1, exit_code)
        identity_check = next(check for check in report["preflight"]["checks"] if check["id"] == "live-identity.public.worker_runs")
        self.assertEqual("fail", identity_check["status"])
        self.assertIn("target_primary_key_mismatch", identity_check["reasons"])


if __name__ == "__main__":
    unittest.main()
