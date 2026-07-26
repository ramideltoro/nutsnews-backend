from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_supabase_sync_relay_smoke as smoke


TARGET_DB_URL = "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require"
TARGET_DB_URL_WITHOUT_SSLMODE = "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres"


class BackendSupabaseSyncRelaySmokeTests(unittest.TestCase):
    def test_rejects_invalid_namespace(self) -> None:
        with self.assertRaisesRegex(smoke.SmokeError, "namespace_invalid"):
            smoke.prove_relay(
                source_database="nutsnews_primary_shadow",
                target_db_url=TARGET_DB_URL,
                namespace="unsafe",
                timeout_seconds=30,
                poll_interval_seconds=1,
            )

    def test_target_query_does_not_place_database_url_in_argv(self) -> None:
        captured_argv: list[str] = []

        def fake_run(argv, **_kwargs):
            captured_argv.extend(argv)
            return mock.Mock(returncode=0, stdout=b"ok\n", stderr=b"")

        with mock.patch.object(smoke.shutil, "which", return_value="/usr/bin/psql"):
            with mock.patch.object(smoke.subprocess, "run", side_effect=fake_run):
                self.assertEqual("ok", smoke.run_target_query(TARGET_DB_URL, "select 1"))

        self.assertNotIn(TARGET_DB_URL, captured_argv)
        self.assertNotIn("pw", " ".join(captured_argv))
        self.assertEqual("/usr/bin/psql", captured_argv[0])

    def test_target_url_without_sslmode_still_forces_pgsslmode_require(self) -> None:
        env = smoke.target_psql_env(TARGET_DB_URL_WITHOUT_SSLMODE)

        self.assertEqual("require", env["PGSSLMODE"])
        self.assertEqual("db.abcdefghijklmnopqrst.supabase.co", env["PGHOST"])
        self.assertEqual("5432", env["PGPORT"])

    def test_rejects_explicit_non_required_sslmode(self) -> None:
        with self.assertRaisesRegex(smoke.SmokeError, "target_database_sslmode_required"):
            smoke.target_psql_env(
                "postgresql://target:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=prefer"
            )

    def test_proves_insert_update_delete_catchup_with_safe_metadata(self) -> None:
        observed_source_sql: list[str] = []
        namespace = "nutsnews-test-relay-123456-1"
        first_user_id = str(smoke.uuid.uuid5(smoke.uuid.NAMESPACE_DNS, f"{namespace}:insert"))
        second_user_id = str(smoke.uuid.uuid5(smoke.uuid.NAMESPACE_DNS, f"{namespace}:update"))
        target_state = {"user_id": "", "count": "0"}

        def fake_source_query(_database: str, sql: str) -> str:
            observed_source_sql.append(sql)
            if "delete from public.staging_fixture_runs" in sql:
                target_state["count"] = "0"
                target_state["user_id"] = ""
            elif "set user_id = excluded.user_id" in sql:
                target_state["user_id"] = first_user_id if not target_state["user_id"] else second_user_id
                target_state["count"] = "2"
            return ""

        def fake_target_query(_db_url: str, sql: str) -> str:
            if "count(*)" in sql:
                return target_state["count"]
            return target_state["user_id"]

        with mock.patch.object(smoke, "run_source_query", side_effect=fake_source_query):
            with mock.patch.object(smoke, "run_target_query", side_effect=fake_target_query):
                with mock.patch.object(smoke, "backend_public_5432_allowed", return_value=False):
                    report = smoke.prove_relay(
                        source_database="nutsnews_primary_shadow",
                        target_db_url=TARGET_DB_URL,
                        namespace=namespace,
                        timeout_seconds=30,
                        poll_interval_seconds=1,
                    )

        self.assertEqual("pass", report["status"])
        self.assertEqual("pass", report["operations"]["insert"]["status"])
        self.assertEqual("pass", report["operations"]["update"]["status"])
        self.assertEqual("pass", report["operations"]["delete"]["status"])
        self.assertFalse(report["backend_postgres_public_5432_allowed"])
        self.assertTrue(report["safe_metadata_only"])
        self.assertNotIn("postgresql://", json.dumps(report).lower())
        self.assertEqual(3, len(observed_source_sql))

    def test_main_output_omits_database_url_when_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            with mock.patch.dict("os.environ", {"NUTSNEWS_PRODUCTION_SUPABASE_DB_URL": TARGET_DB_URL}, clear=True):
                with mock.patch.object(smoke, "prove_relay", side_effect=smoke.SmokeError("insert_catchup_timeout")):
                    with redirect_stdout(StringIO()):
                        exit_code = smoke.main_args(
                            [
                                "--source-db-name",
                                "nutsnews_primary_shadow",
                                "--namespace",
                                "nutsnews-test-relay-123456-1",
                                "--timeout-seconds",
                                "30",
                                "--poll-interval-seconds",
                                "1",
                                "--output",
                                str(output_path),
                                "--enforce",
                            ]
                        )

            report = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(1, exit_code)
        self.assertEqual(["insert_catchup_timeout"], report["blockers"])
        self.assertNotIn("postgresql://", json.dumps(report).lower())
        self.assertNotIn("pw", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
