from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_supabase_standby_relay as relay


def manifest() -> dict:
    return {
        "contract_id": "backend-supabase-standby-reconciliation",
        "version": 1,
        "issue": "ramideltoro/nutsnews#498",
        "epic": "ramideltoro/nutsnews#223",
        "source": {
            "label": "backend_postgres_primary",
            "public_5432_allowed": False,
        },
        "target": {
            "label": "existing_production_supabase_standby",
            "existing_production_supabase_project": True,
            "create_new_supabase_project": False,
            "create_nutsnews_standby_database": False,
        },
        "safety": {
            "safe_metadata_only_report": True,
            "app_worker_writes_to_supabase_before_failover": False,
        },
        "manifest_schema_fingerprint": "f" * 64,
        "tables": [
            {
                "name": "public.worker_runs",
                "primary_key": ["id"],
            },
            {
                "name": "public.runtime_feature_flags",
                "primary_key": ["key"],
            },
        ],
        "sequences": [
            {
                "name": "public.worker_runs_id_seq",
                "table": "public.worker_runs",
                "column": "id",
            }
        ],
    }


def run_main(argv: list[str], manifest_data: dict | None = None, env: dict[str, str] | None = None):
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = Path(tmpdir) / "manifest.json"
        output_path = Path(tmpdir) / "report.json"
        manifest_path.write_text(json.dumps(manifest_data or manifest()), encoding="utf-8")
        with mock.patch.dict("os.environ", env or {}, clear=True):
            with redirect_stdout(StringIO()):
                exit_code = relay.main_args([
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                    *argv,
                ])
        return exit_code, json.loads(output_path.read_text(encoding="utf-8"))


class BackendSupabaseStandbyRelayTests(unittest.TestCase):
    def test_offline_report_is_safe_metadata_only(self) -> None:
        exit_code, report = run_main(["--mode", "offline", "--enforce"])

        self.assertEqual(0, exit_code)
        self.assertEqual("skipped_with_reason", report["status"])
        self.assertTrue(report["safe_metadata_only"])
        self.assertEqual("backend_postgres_primary_private", report["source"])
        self.assertEqual("existing_production_supabase_standby", report["target"])
        self.assertFalse(report["backend_public_5432_allowed"])
        self.assertTrue(report["supports"]["insert"])
        self.assertTrue(report["supports"]["update"])
        self.assertTrue(report["supports"]["delete"])
        self.assertTrue(report["supports"]["sequence_readiness"])
        self.assertNotIn("postgresql://", json.dumps(report))

    def test_source_install_sql_captures_insert_update_delete(self) -> None:
        sql = relay.source_install_sql(manifest(), relay_schema="nutsnews_standby_relay", relay_role="nutsnews_migration_replication")

        self.assertIn("create schema if not exists \"nutsnews_standby_relay\"", sql)
        self.assertIn("create table if not exists \"nutsnews_standby_relay\".events", sql)
        self.assertIn("create or replace function \"nutsnews_standby_relay\".record_change()", sql)
        self.assertIn("create or replace function \"nutsnews_standby_relay\".identity_blockers()", sql)
        self.assertIn("create or replace function \"nutsnews_standby_relay\".schema_metadata()", sql)
        self.assertIn("after insert or update or delete on \"public\".\"worker_runs\"", sql)
        self.assertIn("'public.worker_runs', 'id'", sql)
        self.assertIn("grant execute on function \"nutsnews_standby_relay\".identity_blockers()", sql)
        self.assertIn("grant execute on function \"nutsnews_standby_relay\".schema_metadata()", sql)
        self.assertIn("grant execute on function \"nutsnews_standby_relay\".fetch_batch(integer)", sql)
        self.assertIn("grant execute on function \"nutsnews_standby_relay\".sequence_snapshot()", sql)
        self.assertNotIn("postgresql://", sql)

    def test_unsafe_manifest_without_primary_key_fails_closed(self) -> None:
        bad_manifest = manifest()
        bad_manifest["tables"][0]["primary_key"] = []

        with self.assertRaisesRegex(relay.RelayError, "missing_primary_key"):
            relay.source_install_sql(bad_manifest, relay_schema="nutsnews_standby_relay", relay_role="nutsnews_migration_replication")

    def test_build_apply_sql_supports_upsert_and_delete(self) -> None:
        events = [
            {
                "id": 1,
                "relation_name": "public.worker_runs",
                "operation": "insert",
                "primary_key": {"id": 7},
                "row_data": {"id": 7, "status": "ok"},
            },
            {
                "id": 2,
                "relation_name": "public.worker_runs",
                "operation": "delete",
                "primary_key": {"id": 6},
                "row_data": None,
            },
        ]
        with mock.patch.object(relay, "target_usable_columns", return_value=["id", "status"]):
            sql = relay.build_apply_sql(manifest(), "postgresql://target:pw@127.0.0.1/postgres", events)

        self.assertIn("jsonb_populate_record(null::\"public\".\"worker_runs\"", sql)
        self.assertIn("on conflict (\"id\") do update set", sql)
        self.assertIn("\"status\" = excluded.\"status\"", sql)
        self.assertIn("delete from \"public\".\"worker_runs\" as target", sql)
        self.assertIn("target.\"id\" is not distinct from _pk.\"id\"", sql)

    def test_target_apply_failure_does_not_ack_source_events(self) -> None:
        events = [
            {
                "id": 1,
                "relation_name": "public.worker_runs",
                "operation": "update",
                "primary_key": {"id": 7},
                "row_data": {"id": 7, "status": "ok"},
            }
        ]
        with mock.patch.object(relay, "target_schema_check", return_value=[]):
            with mock.patch.object(relay, "fetch_events", return_value=events):
                with mock.patch.object(relay, "apply_events", side_effect=relay.RelayError("target_apply_failed")):
                    with mock.patch.object(relay, "ack_events") as ack_events:
                        report = relay.run_once(
                            manifest(),
                            "postgresql://source:pw@127.0.0.1/postgres",
                            "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require",
                            "nutsnews_standby_relay",
                            100,
                        )

        self.assertEqual("blocked", report["status"])
        self.assertIn("target_apply_failed", report["blockers"])
        ack_events.assert_not_called()

    def test_schema_identity_mismatch_blocks_run(self) -> None:
        with mock.patch.object(relay, "target_schema_check", return_value=["target_unsafe_table_identity"]):
            with mock.patch.object(relay, "fetch_events") as fetch_events:
                report = relay.run_once(
                    manifest(),
                    "postgresql://source:pw@127.0.0.1/postgres",
                    "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require",
                    "nutsnews_standby_relay",
                    100,
                )

        self.assertEqual("blocked", report["status"])
        self.assertIn("target_unsafe_table_identity", report["blockers"])
        fetch_events.assert_not_called()

    def test_column_schema_mismatch_blocks_run(self) -> None:
        source_db_url = "postgresql://source:pw@127.0.0.1/postgres"
        target_db_url = "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require"
        source_metadata = json.dumps([
            {
                "name": "id",
                "ordinal_position": 1,
                "data_type": "integer",
                "udt_name": "int4",
                "default_md5": "same",
                "is_generated": "NEVER",
                "identity_generation": None,
            }
        ])
        source_metadata_by_relation = json.dumps({"public.worker_runs": json.loads(source_metadata)})
        target_metadata = json.dumps([
            {
                "name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "udt_name": "int8",
                "default_md5": "different",
                "is_generated": "NEVER",
                "identity_generation": None,
            }
        ])

        with mock.patch.object(
            relay,
            "run_psql_url",
            side_effect=[
                ("[]", None),
                ("[]", None),
                (source_metadata_by_relation, None),
                (target_metadata, None),
            ],
        ):
            blockers = relay.target_schema_check(manifest(), source_db_url, target_db_url, "nutsnews_standby_relay")

        self.assertEqual(["schema_mismatch"], blockers)

    def test_pooler_target_is_rejected_before_schema_checks(self) -> None:
        with mock.patch.object(relay, "target_schema_check") as target_schema_check:
            report = relay.run_once(
                manifest(),
                "postgresql://source:pw@127.0.0.1:5432/postgres",
                "postgresql://target:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require",
                "nutsnews_standby_relay",
                100,
            )

        self.assertEqual("blocked", report["status"])
        self.assertIn("target_database_url_is_pooler", report["blockers"])
        self.assertIn("target_database_not_direct_5432", report["blockers"])
        target_schema_check.assert_not_called()

    def test_sequence_advance_uses_safe_counts(self) -> None:
        snapshot = [
            {
                "name": "public.worker_runs_id_seq",
                "table": "public.worker_runs",
                "column": "id",
                "last_value": 20,
                "is_called": True,
                "increment_by": 1,
                "max_id": 20,
            }
        ]
        target_state = json.dumps({"last_value": 10, "is_called": True, "increment_by": 1, "max_id": 10})
        captured_sql: list[str] = []

        def fake_psql(_db_url: str, sql: str, **_kwargs):
            captured_sql.append(sql)
            if "pg_sequences" in sql:
                return target_state, None
            return "", None

        with mock.patch.object(relay, "run_psql_url", side_effect=fake_psql):
            result = relay.advance_sequences("postgresql://target:pw@127.0.0.1/postgres", snapshot)

        self.assertEqual({"checked": 1, "advanced": 1}, result)
        self.assertIn("setval('public.worker_runs_id_seq'::regclass, 21, false)", captured_sql[-1])
        self.assertNotIn("postgresql://", json.dumps(result))

    def test_status_report_omits_connection_material(self) -> None:
        with mock.patch.object(relay, "target_schema_check", return_value=[]):
            with mock.patch.object(relay, "relay_status", return_value={"pending_events": 0, "oldest_event_age_seconds": 0}):
                report = relay.run_status(
                    manifest(),
                    "postgresql://source:pw@127.0.0.1/postgres",
                    "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require",
                    "nutsnews_standby_relay",
                )

        text = json.dumps(report)
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["safe_metadata_only"])
        self.assertNotIn("postgresql://", text)
        self.assertNotIn("password", text.lower())


if __name__ == "__main__":
    unittest.main()
