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

    def __init__(self, *, fetch_all_results: list[list[dict]] | None = None) -> None:
        self.fetches: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.fetch_all_results = list(fetch_all_results or [])

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if self.fetch_all_results:
            return self.fetch_all_results.pop(0)
        return [{"source": "Example", "url": "https://example.com/rss", "is_positive_source": True}]

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        return {"article_count": 7, "enabled": True}

    def execute(self, query: str, params: tuple = ()) -> None:
        self.executes.append((query, params))


def edge_article(overrides: dict | None = None) -> dict:
    row = {
        "id": "article-1",
        "source": "Example",
        "title": "English edge title",
        "original_url": "https://example.com/article-1",
        "image_url": "https://example.com/image.jpg",
        "published_at": "2026-07-16T00:00:00Z",
        "published_on_site_at": "2026-07-16T01:00:00Z",
        "ai_summary": "English edge summary.",
        "category": "Science",
        "positivity_score": 0.91,
    }
    if overrides:
        row.update(overrides)
    return row


class EdgeSnapshotStore(FakeStore):
    def __init__(self, summaries: list[dict] | None = None) -> None:
        super().__init__()
        self.snapshot_rows = [edge_article()]
        self.summary_rows = summaries or []

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.public_feed_snapshot" in query:
            return self.snapshot_rows
        if "from public.article_summaries" in query:
            return self.summary_rows
        return super().fetch_all(query, params)


class ProductionReadinessStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.recent_articles = [
            {
                "id": "article-1",
                "original_url": "https://example.com/article-1",
                "image_url": "https://example.com/image-1.jpg",
                "published_on_site_at": "2026-07-22T11:00:00Z",
                "created_at": "2026-07-22T10:00:00Z",
            },
            {
                "id": "article-2",
                "original_url": "https://example.com/article-2",
                "image_url": None,
                "published_on_site_at": "2026-07-21T11:00:00Z",
                "created_at": "2026-07-21T10:00:00Z",
            },
            {
                "id": "article-3",
                "original_url": "https://example.com/article-3",
                "image_url": "https://example.com/image-3.jpg",
                "published_on_site_at": "2026-07-20T11:00:00Z",
                "created_at": "2026-07-20T10:00:00Z",
            },
        ]
        self.summary_rows = [
            {"original_url": "https://example.com/article-1", "language_code": "fr"},
            {"original_url": "https://example.com/article-2", "language_code": "ja"},
        ]
        self.worker_run = {
            "id": 42,
            "run_started_at": "2026-07-22T09:00:00Z",
            "run_completed_at": "2026-07-22T09:02:00Z",
            "success": True,
            "error_name": None,
            "error_message": None,
            "feed_count": 12,
            "fetched_count": 30,
            "candidate_count": 18,
            "accepted_count": 9,
            "rejected_count": 4,
            "duration_ms": 120000,
        }

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.articles" in query and "select id, original_url" in query:
            return self.recent_articles[: params[0]]
        if "from public.article_summaries" in query:
            return self.summary_rows
        return super().fetch_all(query, params)

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "from public.articles" in query and "created_at >= now()" in query:
            return {"article_count": 2 if params == (24,) else 7}
        if "from public.articles" in query and "count(*)::bigint as article_count" in query:
            return {"article_count": 123}
        if "from public.public_feed_snapshot" in query:
            return {"public_feed_snapshot_count": 87}
        if "from public.worker_runs" in query:
            return self.worker_run
        return super().fetch_one(query, params)


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

    def test_worker_edge_snapshot_rows_default_to_english_metadata(self) -> None:
        store = EdgeSnapshotStore()

        result = worker_db_api.handle_operation(
            "load-public-feed-snapshot-rows",
            {"providerMode": "backend_postgres_shadow", "limit": 1},
            store,
        )

        self.assertEqual("English edge title", result[0]["title"])
        self.assertEqual("English edge summary.", result[0]["ai_summary"])
        self.assertEqual("en", result[0]["language_code"])
        self.assertEqual("en", result[0]["requested_language_code"])
        self.assertIs(result[0]["translation_available"], True)
        self.assertEqual(1, len(store.fetches))
        query, params = store.fetches[0]
        self.assertIn("from public.public_feed_snapshot p", query)
        self.assertIn("order by p.snapshot_rank asc", query)
        self.assertEqual((1,), params)

    def test_worker_edge_snapshot_rows_localize_requested_language(self) -> None:
        store = EdgeSnapshotStore(
            summaries=[
                {
                    "original_url": "https://example.com/article-1",
                    "language_code": "fr",
                    "title": "Titre de bord francais",
                    "summary": "Resume de bord francais.",
                }
            ]
        )

        result = worker_db_api.handle_operation(
            "load-public-feed-snapshot-rows",
            {"providerMode": "backend_postgres_shadow", "limit": 1, "languageCode": "fr"},
            store,
        )

        self.assertEqual("Titre de bord francais", result[0]["title"])
        self.assertEqual("Resume de bord francais.", result[0]["ai_summary"])
        self.assertEqual("fr", result[0]["language_code"])
        self.assertEqual("fr", result[0]["requested_language_code"])
        self.assertIs(result[0]["translation_available"], True)
        self.assertEqual(2, len(store.fetches))
        summary_query, summary_params = store.fetches[1]
        self.assertIn("from public.article_summaries", summary_query)
        self.assertEqual((["https://example.com/article-1"], "fr", 1), summary_params)

    def test_worker_edge_snapshot_rows_mark_missing_translation_as_english_fallback(self) -> None:
        store = EdgeSnapshotStore()

        result = worker_db_api.handle_operation(
            "load-public-feed-snapshot-rows",
            {"providerMode": "backend_postgres_shadow", "limit": 1, "languageCode": "fr"},
            store,
        )

        self.assertEqual("English edge title", result[0]["title"])
        self.assertEqual("English edge summary.", result[0]["ai_summary"])
        self.assertEqual("en", result[0]["language_code"])
        self.assertEqual("fr", result[0]["requested_language_code"])
        self.assertIs(result[0]["translation_available"], False)

    def test_app_public_feed_snapshot_localizes_requested_language(self) -> None:
        store = EdgeSnapshotStore(
            summaries=[
                {
                    "original_url": "https://example.com/article-1",
                    "language_code": "fr",
                    "title": "Titre public francais",
                    "summary": "Resume public francais.",
                }
            ]
        )

        result = worker_db_api.handle_app_operation(
            "load-public-feed-snapshot",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 1,
                "offset": 0,
                "requestedLanguageCode": "fr",
            },
            store,
        )

        self.assertEqual("Titre public francais", result[0]["title"])
        self.assertEqual("Resume public francais.", result[0]["ai_summary"])
        self.assertEqual("fr", result[0]["language_code"])
        self.assertEqual("fr", result[0]["requested_language_code"])
        self.assertIs(result[0]["translation_available"], True)
        self.assertEqual(2, len(store.fetches))
        snapshot_query, snapshot_params = store.fetches[0]
        self.assertIn("from public.public_feed_snapshot", snapshot_query)
        self.assertEqual((1, 0), snapshot_params)
        summary_query, summary_params = store.fetches[1]
        self.assertIn("from public.article_summaries", summary_query)
        self.assertEqual((["https://example.com/article-1"], "fr", 1), summary_params)

    def test_app_home_feed_snapshot_marks_missing_translation_as_english_fallback(self) -> None:
        store = EdgeSnapshotStore()

        result = worker_db_api.handle_app_operation(
            "load-home-feed-snapshot",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 1,
                "offset": 0,
                "requestedLanguageCode": "fr",
            },
            store,
        )

        self.assertEqual("English edge title", result[0]["title"])
        self.assertEqual("English edge summary.", result[0]["ai_summary"])
        self.assertEqual("en", result[0]["language_code"])
        self.assertEqual("fr", result[0]["requested_language_code"])
        self.assertIs(result[0]["translation_available"], False)
        self.assertEqual(2, len(store.fetches))
        summary_query, summary_params = store.fetches[1]
        self.assertIn("from public.article_summaries", summary_query)
        self.assertEqual((["https://example.com/article-1"], "fr", 1), summary_params)

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

    def test_save_accepted_articles_rejects_already_published_rows(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "save-accepted-articles-batch",
                {
                    "providerMode": "backend_postgres_primary",
                    "articles": [
                        {
                            "source": "Example",
                            "title": "Published without translations",
                            "original_url": "https://example.com/published",
                            "status": "published",
                        }
                    ],
                },
                store,
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_save_accepted_articles_allows_translation_pending_rows(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        result = worker_db_api.handle_operation(
            "save-accepted-articles-batch",
            {
                "providerMode": "backend_postgres_primary",
                "articles": [
                    {
                        "source": "Example",
                        "title": "Pending translations",
                        "original_url": "https://example.com/pending",
                        "status": "translation_pending",
                    }
                ],
            },
            store,
        )

        query, params = store.executes[0]
        self.assertEqual({"ok": True}, result)
        self.assertIn("insert into public.articles", query)
        self.assertEqual("translation_pending", params[-1])

    def test_publish_requires_enabled_translation_languages(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "publish-articles-batch",
                {
                    "providerMode": "backend_postgres_primary",
                    "originalUrls": ["https://example.com/pending"],
                    "status": "published",
                },
                store,
            )

        self.assertEqual(400, error.exception.status)
        self.assertEqual([], store.fetches)
        self.assertEqual([], store.executes)

    def test_publish_blocks_articles_above_translation_budget_until_gaps_recover(self) -> None:
        store = FakeStore(
            fetch_all_results=[
                [
                    {"original_url": "https://example.com/over-budget", "language_code": "de"},
                    {"original_url": "https://example.com/over-budget", "language_code": "el"},
                ],
                [],
                [{"original_url": "https://example.com/over-budget"}],
            ]
        )
        store.writes_enabled = True

        blocked = worker_db_api.handle_operation(
            "publish-articles-batch",
            {
                "providerMode": "backend_postgres_primary",
                "originalUrls": ["https://example.com/over-budget"],
                "languageCodes": ["fr", "ja", "de-CH", "de", "el"],
                "status": "published",
            },
            store,
        )
        recovered = worker_db_api.handle_operation(
            "publish-articles-batch",
            {
                "providerMode": "backend_postgres_primary",
                "originalUrls": ["https://example.com/over-budget"],
                "languageCodes": ["fr", "ja", "de-CH", "de", "el"],
                "status": "published",
            },
            store,
        )

        self.assertFalse(blocked["ok"])
        self.assertEqual(1, blocked["blockedCount"])
        self.assertEqual(
            [
                {"original_url": "https://example.com/over-budget", "language_code": "de"},
                {"original_url": "https://example.com/over-budget", "language_code": "el"},
            ],
            blocked["missingTranslations"],
        )
        self.assertEqual(
            {"ok": True, "requestedCount": 1, "publishedCount": 1, "blockedCount": 0, "missingTranslations": []},
            recovered,
        )
        gap_query, gap_params = store.fetches[0]
        update_query, update_params = store.fetches[2]
        self.assertIn("from unnest(%s::text[])", gap_query)
        self.assertIn("left join public.article_summaries", gap_query)
        self.assertEqual((["https://example.com/over-budget"], ["fr", "ja", "de-CH", "de", "el"], 5), gap_params)
        self.assertIn("update public.articles", update_query)
        self.assertIn("returning original_url", update_query)
        self.assertEqual((["https://example.com/over-budget"],), update_params)
        self.assertEqual([], store.executes)

    def test_publish_rejects_unsupported_translation_language(self) -> None:
        store = FakeStore()
        store.writes_enabled = True

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "publish-articles-batch",
                {
                    "providerMode": "backend_postgres_primary",
                    "originalUrls": ["https://example.com/pending"],
                    "languageCodes": ["fr", "en"],
                    "status": "published",
                },
                store,
            )

        self.assertEqual(400, error.exception.status)
        self.assertEqual([], store.fetches)

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

    def test_app_admin_production_readiness_returns_dashboard_snapshot(self) -> None:
        store = ProductionReadinessStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-production-readiness",
            {
                "providerMode": "backend_postgres_primary",
                "recentArticleLimit": 3,
                "translationSampleLimit": 2,
                "targetLanguageCodes": ["fr", "ja", "en", "fr"],
                "articleGrowthWindowsHours": [24, 24 * 7],
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(123, row["articleCount"])
        self.assertEqual(87, row["publicFeedSnapshotCount"])
        self.assertEqual(store.recent_articles, row["recentArticles"])
        self.assertEqual(store.worker_run, row["workerRun"])
        self.assertEqual(2, row["articlesLast24Hours"])
        self.assertEqual(7, row["articlesLast7Days"])
        self.assertEqual(store.summary_rows, row["translationSummaries"])
        self.assertEqual(4, row["translationExpectedCount"])
        self.assertEqual([], store.executes)

        recent_query, recent_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.articles" in query and "select id, original_url" in query
        )
        self.assertIn("where status = 'published'", recent_query)
        self.assertIn("order by published_on_site_at desc nulls last", recent_query)
        self.assertEqual((3,), recent_params)

        summary_query, summary_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.article_summaries" in query
        )
        self.assertIn("original_url = any(%s)", summary_query)
        self.assertIn("language_code = any(%s)", summary_query)
        self.assertEqual(
            (
                ["https://example.com/article-1", "https://example.com/article-2"],
                ["fr", "ja"],
                4,
            ),
            summary_params,
        )

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

    def test_summary_translation_recovery_includes_published_and_pending_articles(self) -> None:
        store = FakeStore()

        worker_db_api.handle_operation(
            "load-summary-translation-recovery-articles",
            {
                "providerMode": "backend_postgres_shadow",
                "lookbackLimit": 25,
            },
            store,
        )

        query, params = store.fetches[0]
        self.assertIn("status in ('published', 'translation_pending')", query)
        self.assertIn("ai_summary is not null", query)
        self.assertEqual((25,), params)


if __name__ == "__main__":
    unittest.main()
