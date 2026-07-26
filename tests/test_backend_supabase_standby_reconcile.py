from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_supabase_standby_reconcile as reconcile


COLUMN_METADATA = json.dumps(
    [
        {
            "name": "id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "udt_name": "int8",
            "is_nullable": "NO",
            "is_generated": "NEVER",
            "identity_generation": None,
            "default_md5": "default-digest",
        },
        {
            "name": "updated_at",
            "ordinal_position": 2,
            "data_type": "timestamp with time zone",
            "udt_name": "timestamptz",
            "is_nullable": "YES",
            "is_generated": "NEVER",
            "identity_generation": None,
            "default_md5": "empty-digest",
        },
    ],
    separators=(",", ":"),
)


def manifest() -> dict:
    return {
        "version": 1,
        "manifest_schema_fingerprint": "f" * 64,
        "tables": [
            {
                "name": "public.worker_runs",
                "primary_key": ["id"],
            }
        ],
        "sequences": [
            {
                "name": "public.worker_runs_id_seq",
                "table": "public.worker_runs",
                "column": "id",
            }
        ],
        "backfill": {
            "table_order": ["public.worker_runs"],
        },
    }


def run_with_manifest(manifest_data: dict, argv: list[str], env: dict[str, str], side_effect: list[tuple[str | None, str | None]]):
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = Path(tmpdir) / "standby.json"
        output_path = Path(tmpdir) / "report.json"
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch.object(reconcile, "run_psql", side_effect=side_effect):
                with redirect_stdout(StringIO()):
                    exit_code = reconcile.main_args(
                        [
                            "--manifest",
                            str(manifest_path),
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


def passing_psql_side_effect() -> list[tuple[str | None, str | None]]:
    return [
        ("schema-json", None),
        ("schema-json", None),
        ("contract-json", None),
        ("contract-json", None),
        ("12", None),
        ("12", None),
        (COLUMN_METADATA, None),
        (COLUMN_METADATA, None),
        ("row-checksum", None),
        ("row-checksum", None),
        (json.dumps({"last_value": 12, "is_called": True, "increment_by": 1}), None),
        (json.dumps({"last_value": 12, "is_called": True, "increment_by": 1}), None),
        ("12", None),
        ("12", None),
    ]


class BackendSupabaseStandbyReconcileTests(unittest.TestCase):
    def test_offline_report_is_safe_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "standby.json"
            output_path = Path(tmpdir) / "report.json"
            manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                with redirect_stdout(StringIO()):
                    exit_code = reconcile.main_args(
                        [
                            "--manifest",
                            str(manifest_path),
                            "--output",
                            str(output_path),
                            "--offline",
                            "--enforce",
                        ]
                    )
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertEqual("skipped_with_reason", report["status"])
        self.assertTrue(report["safe_metadata_only"])
        self.assertFalse(report["create_new_supabase_project"])
        self.assertFalse(report["create_nutsnews_standby_database"])
        self.assertFalse(report["app_worker_writes_to_supabase_before_failover"])

    def test_matching_counts_checksums_schema_and_sequence_pass(self) -> None:
        exit_code, report = run_with_manifest(
            manifest(),
            ["--enforce"],
            {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            passing_psql_side_effect(),
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", report["status"])
        self.assertEqual([], report["post_reconciliation"]["failed_required_checks"])
        self.assertEqual("not_required", report["backfill"]["status"])

    def test_row_checksum_mismatch_fails_without_raw_row_data(self) -> None:
        effects = passing_psql_side_effect()
        effects[9] = ("different-row-checksum", None)
        exit_code, report = run_with_manifest(
            manifest(),
            ["--enforce"],
            {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            effects,
        )

        table_check = next(check for check in report["post_reconciliation"]["checks"] if check["id"] == "table.public.worker_runs")
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", report["status"])
        self.assertIn("row_checksum_mismatch", table_check["reasons"])
        self.assertNotIn("raw row", json.dumps(report).lower())

    def test_sequence_next_value_not_above_source_max_id_fails(self) -> None:
        effects = passing_psql_side_effect()
        effects[11] = (json.dumps({"last_value": 12, "is_called": True, "increment_by": 1}), None)
        effects[12] = ("14", None)
        exit_code, report = run_with_manifest(
            manifest(),
            ["--enforce"],
            {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
            effects,
        )

        sequence_check = next(check for check in report["post_reconciliation"]["checks"] if check["id"] == "sequence.public.worker_runs_id_seq")
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", sequence_check["status"])
        self.assertIn("target_next_value_not_above_source_max_id", sequence_check["reasons"])

    def test_apply_backfill_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "standby.json"
            output_path = Path(tmpdir) / "report.json"
            manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"SRC": "postgresql://source:pw@127.0.0.1/postgres", "TGT": "postgresql://target:pw@127.0.0.1/postgres"},
                clear=True,
            ):
                with mock.patch.object(reconcile, "validate_standby", return_value={"status": "fail", "failed_required_checks": ["table.public.worker_runs"]}):
                    with redirect_stdout(StringIO()):
                        exit_code = reconcile.main_args(
                            [
                                "--manifest",
                                str(manifest_path),
                                "--source-db-url-env",
                                "SRC",
                                "--target-db-url-env",
                                "TGT",
                                "--output",
                                str(output_path),
                                "--mode",
                                "apply-backfill",
                                "--enforce",
                            ]
                        )
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("apply_confirmation_missing", report["error"])

    def test_apply_backfill_records_safe_metadata_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "standby.json"
            output_path = Path(tmpdir) / "report.json"
            manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {
                    "SRC": "postgresql://source:pw@127.0.0.1/postgres",
                    "TGT": "postgresql://target:pw@127.0.0.1/postgres",
                    "NUTSNEWS_STANDBY_RECONCILE_CONFIRMATION": reconcile.APPLY_CONFIRMATION,
                },
                clear=True,
            ):
                with mock.patch.object(
                    reconcile,
                    "validate_standby",
                    side_effect=[
                        {"status": "fail", "failed_required_checks": ["table.public.worker_runs"]},
                        {"status": "pass", "failed_required_checks": []},
                    ],
                ):
                    with mock.patch.object(
                        reconcile,
                        "apply_backfill",
                        return_value={"status": "applied", "table_count": 1, "sequence_count": 1, "safe_metadata_only": True},
                    ) as apply_backfill:
                        with redirect_stdout(StringIO()):
                            exit_code = reconcile.main_args(
                                [
                                    "--manifest",
                                    str(manifest_path),
                                    "--source-db-url-env",
                                    "SRC",
                                    "--target-db-url-env",
                                    "TGT",
                                    "--output",
                                    str(output_path),
                                    "--mode",
                                    "apply-backfill",
                                    "--enforce",
                                ]
                            )
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", report["status"])
        self.assertEqual("applied", report["backfill"]["status"])
        apply_backfill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
