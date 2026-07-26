from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_postgres_write_position as position


def contract() -> dict:
    return {
        "contract_id": "backend-supabase-sync-relay",
        "version": 1,
        "source": {"label": "backend_postgres_primary"},
        "target": {"label": "existing_production_supabase_standby"},
        "source_manifest": {"schema_fingerprint": "f" * 64},
        "tables": [
            {"name": "public.worker_runs", "primary_key": ["id"]},
        ],
    }


class BackendPostgresWritePositionTests(unittest.TestCase):
    def test_snapshot_uses_safe_hashes_without_printing_sql_or_rows(self):
        queries: list[str] = []

        def fake_query(_db_url: str, query: str):
            queries.append(query)
            if "count(*)" in query:
                return "2", None
            if "information_schema.columns" in query:
                return json.dumps(
                    [
                        {"name": "id", "is_generated": "NEVER"},
                        {"name": "created_at", "is_generated": "NEVER"},
                    ]
                ), None
            return "row-md5-digest", None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            contract_path = root / "contract.json"
            output_path = root / "position.json"
            contract_path.write_text(json.dumps(contract()), encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"NUTSNEWS_BACKEND_PRIMARY_DB_URL": "postgresql://user:pw@127.0.0.1:5432/db"}),
                mock.patch.object(position.reconcile, "query_value", side_effect=fake_query),
                redirect_stdout(StringIO()) as stdout,
            ):
                exit_code = position.main_args(["--contract", str(contract_path), "--output", str(output_path), "--enforce"])

            report = json.loads(output_path.read_text(encoding="utf-8"))
            printed = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["required_table_count"], 1)
        self.assertEqual(report["passed_table_count"], 1)
        self.assertEqual(report["failed_table_count"], 0)
        self.assertTrue(report["safe_metadata_only"])
        self.assertIn("row_checksum_sha256", report["tables"][0])
        self.assertNotIn("postgresql://user:pw", printed)
        self.assertNotIn("select ", printed.lower())
        self.assertNotIn("row-md5-digest", printed)
        self.assertTrue(any("row_to_json" in query for query in queries))

    def test_missing_database_url_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            contract_path = root / "contract.json"
            output_path = root / "position.json"
            contract_path.write_text(json.dumps(contract()), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(StringIO()):
                exit_code = position.main_args(["--contract", str(contract_path), "--output", str(output_path), "--enforce"])
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn("source_db_url_missing", report["blockers"])


if __name__ == "__main__":
    unittest.main()
