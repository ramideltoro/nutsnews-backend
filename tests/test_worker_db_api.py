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

    def test_app_smoke_does_not_touch_database(self) -> None:
        store = FakeStore()

        result = worker_db_api.handle_app_operation(
            "app-provider-smoke",
            {"providerMode": "backend_postgres_shadow"},
            store,
        )

        self.assertEqual({"ok": True, "provider": "backend_postgres", "writesEnabled": False}, result)
        self.assertEqual([], store.fetches)
        self.assertEqual([], store.executes)

    def test_app_public_feed_read_is_bounded_and_allowlisted(self) -> None:
        store = FakeStore()

        worker_db_api.handle_app_operation(
            "load-public-feed-snapshot",
            {
                "providerMode": "backend_postgres_shadow",
                "category": "science",
                "limit": 2,
                "offset": 3,
            },
            store,
        )

        query, params = store.fetches[0]
        self.assertIn("from public.public_feed_snapshot", query)
        self.assertIn("category ilike %s", query)
        self.assertIn("order by snapshot_rank asc", query)
        self.assertEqual(("%science%", 2, 3), params)

    def test_app_shadow_write_is_rejected_before_database_call(self) -> None:
        store = FakeStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_app_operation(
                "record-quota-usage-event",
                {
                    "providerMode": "backend_postgres_shadow",
                    "eventType": "email_send",
                    "eventSource": "contact",
                },
                store,
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_app_primary_write_requires_deployment_guardrail(self) -> None:
        store = FakeStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_app_operation(
                "record-quota-usage-event",
                {
                    "providerMode": "backend_postgres_primary",
                    "eventType": "email_send",
                    "eventSource": "contact",
                },
                store,
            )

        self.assertEqual(403, error.exception.status)
        self.assertEqual([], store.executes)

    def test_app_quota_write_uses_allowlisted_insert(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        result = worker_db_api.handle_app_operation(
            "record-quota-usage-event",
            {
                "providerMode": "backend_postgres_primary",
                "eventType": "email_send",
                "eventSource": "contact",
                "provider": "resend",
                "quantity": 2,
                "metadata": {"ok": True, "unexpected_column": "blocked"},
            },
            store,
        )

        query, params = store.executes[0]
        self.assertEqual({"ok": True}, result)
        self.assertIn("insert into public.quota_usage_events", query)
        self.assertNotIn("unexpected_column", query)
        self.assertEqual(("email_send", "contact", "resend", 2, '{"ok":true,"unexpected_column":"blocked"}'), params)

    def test_app_engagement_write_uses_database_function(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        worker_db_api.handle_app_operation(
            "record-article-engagement-event",
            {
                "providerMode": "backend_postgres_primary",
                "eventType": "outbound_click",
                "articleId": "00000000-0000-0000-0000-000000000001",
                "source": "Example",
                "category": "Science",
                "quantity": 1,
            },
            store,
        )

        query, params = store.fetches[0]
        self.assertIn("public.record_article_engagement_event", query)
        self.assertEqual(
            ("outbound_click", "00000000-0000-0000-0000-000000000001", "Example", "Science", 1),
            params,
        )


if __name__ == "__main__":
    unittest.main()
