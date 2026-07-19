#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "ansible/roles/backend_baseline/files/nutsnews_worker_db_api.py"

spec = importlib.util.spec_from_file_location("nutsnews_worker_db_api", API_PATH)
worker_db_api = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker_db_api)


class FakeStore:
    writes_enabled = False
    max_limit = 10000

    def __init__(self) -> None:
        self.fetches: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        return [{"source": "Example", "url": "https://example.com/rss", "is_positive_source": True}]

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        return {"article_count": 7, "enabled": True}

    def execute(self, query: str, params: tuple = ()) -> None:
        self.executes.append((query, params))


class WorkerDbApiTests(unittest.TestCase):
    def test_shadow_feed_read_uses_bounded_select(self) -> None:
        store = FakeStore()
        result = worker_db_api.handle_operation(
            "load-feeds-for-shard",
            {"providerMode": "backend_postgres_shadow", "feedsPerShard": 1, "offset": 0},
            store,
        )

        self.assertEqual("https://example.com/rss", result[0]["url"])
        query, params = store.fetches[0]
        self.assertIn("from public.rss_feeds", query)
        self.assertEqual((1, 0), params)

    def test_shadow_write_is_rejected_before_database_call(self) -> None:
        store = FakeStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "save-worker-run",
                {"providerMode": "backend_postgres_shadow", "run": {"request_id": "x"}},
                store,
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_primary_write_requires_deployment_guardrail(self) -> None:
        store = FakeStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "save-worker-run",
                {"providerMode": "backend_postgres_primary", "run": {"request_id": "x"}},
                store,
            )

        self.assertEqual(403, error.exception.status)
        self.assertEqual([], store.executes)

    def test_run_log_insert_uses_allowlisted_columns(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        worker_db_api.handle_operation(
            "save-worker-run",
            {
                "providerMode": "backend_postgres_primary",
                "run": {
                    "request_id": "x",
                    "duration_ms": 1,
                    "malicious_column) values (1); --": "blocked",
                },
            },
            store,
        )

        query, params = store.executes[0]
        self.assertIn("insert into public.worker_runs", query)
        self.assertIn("request_id", query)
        self.assertIn("duration_ms", query)
        self.assertNotIn("malicious", query)
        self.assertEqual(len(params), len(worker_db_api.WRITE_TABLE_COLUMNS["worker_runs"]))


if __name__ == "__main__":
    unittest.main()
