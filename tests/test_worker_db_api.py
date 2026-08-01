#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from unittest.mock import patch
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


def uplift_metadata(overrides: dict | None = None) -> dict:
    body = {
        "providerMode": "backend_postgres_shadow",
        "idempotencyKey": "idem-uplift-1",
        "messageId": "message-uplift-1",
        "correlationId": "correlation-uplift-1",
        "pipelineRunId": "pipeline-uplift-1",
        "stageExecutionId": "stage-uplift-1",
        "sourceMessageId": "source-message-uplift-1",
        "actorService": "worker-uplift-persistence",
        "schemaVersion": 1,
        "operationVersion": 7,
        "expectedArticleVersion": 6,
    }
    if overrides:
        body.update(overrides)
    return body


def uplift_shadow_aggregate_body(overrides: dict | None = None) -> dict:
    body = uplift_metadata(
        {
            "shadowAggregate": {
                "articleIdentityHash": "article-hash-1",
                "canonicalUrlHash": "canonical-hash-1",
                "originalUrlHash": "original-hash-1",
                "aggregateVersion": 7,
                "sourceFeedUrl": "https://example.com/rss",
                "titleRef": "payload://title/1",
                "imageUrlRef": "payload://image/1",
                "category": "Science",
                "positivityScore": 8.5,
                "approvalVersion": 3,
                "translationLanguages": ["fr", "ja", "fr"],
                "publicationStatus": "ready",
                "payloadRef": "payload://aggregate/1",
                "payloadDigest": "sha256:aggregate",
                "diagnosticMetadata": {"safe": True},
            }
        }
    )
    if overrides:
        body.update(overrides)
    return body


def uplift_publication_body(overrides: dict | None = None) -> dict:
    body = uplift_metadata(
        {
            "providerMode": "backend_postgres_primary",
            "actorService": "worker-uplift-publication",
            "originalUrls": ["https://example.com/article-1"],
            "status": "published",
            "languageCodes": ["fr", "ja", "de-CH", "de", "el"],
        }
    )
    if overrides:
        body.update(overrides)
    return body


def uplift_accepted_article_body(overrides: dict | None = None) -> dict:
    body = uplift_metadata({
        "providerMode": "backend_postgres_primary",
        "articles": [{
            "source": "example.com",
            "title": "Community library opens",
            "original_url": "https://example.com/article-1",
            "image_url": "https://example.com/article-1.jpg",
            "published_at": "2026-08-01T20:00:00Z",
            "published_on_site_at": "2026-08-01T20:01:00Z",
            "original_excerpt": "Neighbors created a shared library.",
            "ai_summary": "Neighbors opened a free community library.",
            "category": "Community | Uplifting",
            "positivity_score": 91,
            "ai_provider": "local_ai",
            "ai_model": "qwen2.5:3b",
            "status": "translation_pending",
        }],
    })
    if overrides:
        body.update(overrides)
    return body


def uplift_article_summaries_body(overrides: dict | None = None) -> dict:
    body = uplift_metadata({
        "providerMode": "backend_postgres_primary",
        "summaries": [
            {
                "original_url": "https://example.com/article-1",
                "language_code": language,
                "source_language_code": "en",
                "title": f"Localized title {language}",
                "summary": f"Localized summary {language}",
                "generated_by": "local_ai",
                "model": "qwen2.5:3b",
                "updated_at": "2026-08-01T20:01:00Z",
            }
            for language in ("fr", "ja", "de-CH", "de", "el")
        ],
    })
    if overrides:
        body.update(overrides)
    return body


def worker_uplift_stage_health_row(stage: str, overrides: dict | None = None) -> dict:
    row = {
        "stage_name": stage,
        "active_ingestion_owner": "legacy_shards",
        "stage_status": "healthy",
        "stale_status": "current",
        "last_attempt_at": "2026-07-22T10:00:00Z",
        "last_success_at": "2026-07-22T10:01:00Z",
        "last_failure_at": None,
        "consecutive_failure_count": 0,
        "throughput_per_minute": "12.5",
        "latency_p50_ms": 240,
        "latency_p95_ms": 900,
        "retry_count": 1,
        "dlq_count": 0,
        "queue_age_seconds": 7,
        "active_consumers": 2,
        "deployment_version": f"{stage}-sha-123",
        "telemetry_version": 1,
        "projection_version": 3,
        "sanitized_error_code": None,
        "sanitized_error_message": None,
        "updated_at": "2026-07-22T10:01:30Z",
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


class ArticleReviewsStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.option_rows = [
            {"source": "Reuters", "category": "World|Science"},
            {"source": "TechCrunch", "category": "Science"},
        ]
        self.recent_articles = [
            {
                "id": "published-recent",
                "original_url": "https://example.com/recent-published",
                "source": "Reuters",
                "title": "Published recent",
                "image_url": "https://cdn.example.com/recent.jpg",
                "published_at": "2026-07-22T09:30:00Z",
                "published_on_site_at": "2026-07-22T09:45:00Z",
                "created_at": "2026-07-22T09:20:00Z",
                "ai_summary": "Published article summary.",
                "category": "World",
                "positivity_score": 7,
                "status": "published",
            }
        ]
        self.recent_review_rows = [
            {
                "id": 11,
                "reviewed_at": "2026-07-22T10:00:00Z",
                "original_url": "https://example.com/recent-published",
                "source": "Reuters",
                "title": "Recent review",
                "decision": "reject",
                "category": "World",
                "positivity_score": 5,
                "summary": "A concise article summary.",
                "reason": "Needs more positive framing.",
                "ai_provider": "openai",
                "ai_model": "gpt-4o-mini",
                "prompt_version": "2026-07-01",
                "model_version": "gpt-4o-mini-2026-07",
                "review_duration_ms": 1200,
            }
        ]
        self.version_rows = [
            {
                "version_window": "current",
                "version_rank": 1,
                "prompt_version": "2026-07-01",
                "model_version": "gpt-4o-mini-2026-07",
                "ai_provider": "openai",
                "ai_model": "gpt-4o-mini",
                "total_reviews": 80,
                "accepted_reviews": 50,
                "rejected_reviews": 30,
                "acceptance_rate_pct": 62.5,
                "rejection_rate_pct": 37.5,
                "average_positivity_score": 6.4,
                "previous_acceptance_rate_pct": 60,
                "previous_rejection_rate_pct": 40,
                "previous_average_positivity_score": 6.1,
                "acceptance_rate_delta_pct": 2.5,
                "rejection_rate_delta_pct": -2.5,
                "average_score_delta": 0.3,
                "first_reviewed_at": "2026-07-21T00:00:00Z",
                "latest_reviewed_at": "2026-07-22T10:00:00Z",
            }
        ]
        self.review_rows = [
            {
                "id": 10,
                "reviewed_at": "2026-07-22T10:00:00Z",
                "original_url": "https://example.com/reviewed",
                "source": "Reuters",
                "title": "Visible review",
                "decision": "reject",
                "category": "World",
                "positivity_score": 5,
                "summary": "A concise article summary.",
                "reason": "Needs more positive framing.",
                "ai_provider": "openai",
                "ai_model": "gpt-4o-mini",
                "prompt_version": "2026-07-01",
                "model_version": "gpt-4o-mini-2026-07",
                "review_duration_ms": 1200,
            }
        ]
        self.published_review_articles = [
            {
                "id": "published-visible",
                "original_url": "https://example.com/reviewed",
                "source": "Reuters",
                "title": "Published visible",
                "image_url": "https://cdn.example.com/visible.jpg",
                "published_at": "2026-07-22T09:30:00Z",
                "published_on_site_at": "2026-07-22T09:45:00Z",
                "created_at": "2026-07-22T09:20:00Z",
                "ai_summary": "Published article summary.",
                "category": "World",
                "positivity_score": 7,
                "status": "published",
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "select source, category" in query and "from public.article_ai_reviews" in query:
            return self.option_rows
        if "from public.articles" in query and "where status = 'published'" in query:
            return self.recent_articles[: params[0]]
        if "from public.article_ai_reviews" in query and "original_url = any(%s)" in query:
            return self.recent_review_rows
        if "from public.ai_decision_version_report" in query:
            return self.version_rows
        if "from public.article_ai_reviews" in query and "order by reviewed_at asc" in query:
            return self.review_rows
        if "from public.articles" in query and "where original_url = any(%s)" in query:
            return self.published_review_articles
        return super().fetch_all(query, params)

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "count(*)::bigint as total_matching_reviews" in query:
            return {"total_matching_reviews": 125}
        return super().fetch_one(query, params)


class ArticleEngagementStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.source_category_rows = [
            {
                "source": "Reuters",
                "category": "World",
                "outbound_click_count": "5",
                "category_interest_count": 3,
                "total_engagement_count": "8",
                "first_event_date": "2026-07-20",
                "latest_event_date": "2026-07-21",
                "last_updated_at": "2026-07-22T10:00:00Z",
            },
            {
                "source": "AP",
                "category": "Technology",
                "outbound_click_count": 2,
                "category_interest_count": "4",
                "total_engagement_count": 6,
                "first_event_date": "2026-07-20",
                "latest_event_date": "2026-07-21",
                "last_updated_at": None,
            },
        ]
        self.article_rows = [
            {
                "article_id": "4a225989-6ca9-4b31-a727-873ab7a6d8e0",
                "title": "Election results point to coalition talks",
                "original_url": "https://publisher.example.com/election-results",
                "source": "Reuters",
                "category": "World",
                "outbound_click_count": "5",
                "first_event_date": "2026-07-20",
                "latest_event_date": "2026-07-21",
                "last_updated_at": "2026-07-22T10:00:00Z",
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.article_engagement_source_category_summary" in query:
            return self.source_category_rows[: params[0]]
        if "from public.article_engagement_article_summary" in query:
            return self.article_rows[: params[0]]
        return super().fetch_all(query, params)


class AiUsageStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_rows = [
            {
                "id": 101,
                "created_at": "2026-07-22T09:59:00Z",
                "run_started_at": "2026-07-22T10:00:00Z",
                "run_completed_at": "2026-07-22T10:02:00Z",
                "run_source": "scheduled",
                "request_id": "req-ai-usage",
                "shard_index": 2,
                "feeds_per_shard": 12,
                "max_ai_reviews": 40,
                "feed_count": 12,
                "fetched_count": 100,
                "candidate_count": 55,
                "already_reviewed_count": 10,
                "unreviewed_count": 45,
                "eligible_for_ai_count": 30,
                "ai_reviewed_count": 20,
                "openai_model": "gpt-4o-mini",
                "openai_call_count": 8,
                "openai_prompt_tokens": 1200,
                "openai_completion_tokens": 700,
                "openai_total_tokens": 1900,
                "estimated_openai_cost_usd": "0.0031",
                "openai_review_count": 5,
                "openai_review_prompt_tokens": 800,
                "openai_review_completion_tokens": 500,
                "openai_review_total_tokens": 1300,
                "estimated_openai_review_cost_usd": "0.0020",
                "openai_translation_count": 3,
                "openai_translation_prompt_tokens": 400,
                "openai_translation_completion_tokens": 200,
                "openai_translation_total_tokens": 600,
                "estimated_openai_translation_cost_usd": "0.0011",
                "local_ai_model": "llama-3.1",
                "local_ai_call_count": 7,
                "local_ai_prompt_tokens": 900,
                "local_ai_completion_tokens": 300,
                "local_ai_total_tokens": 1200,
                "local_ai_accepted_count": 4,
                "local_ai_rejected_count": 3,
                "local_ai_review_count": 6,
                "local_ai_review_prompt_tokens": 750,
                "local_ai_review_completion_tokens": 250,
                "local_ai_review_total_tokens": 1000,
                "local_ai_translation_count": 1,
                "local_ai_translation_prompt_tokens": 150,
                "local_ai_translation_completion_tokens": 50,
                "local_ai_translation_total_tokens": 200,
                "estimated_local_ai_savings_usd": "0.0045",
                "openai_accepted_count": 9,
                "openai_rejected_count": 2,
                "published_accepted_count": 8,
                "total_rejected_count": 5,
                "no_thumbnail_rejected_count": 1,
                "locally_rejected_count": 3,
                "cost_protection_limit_reached": False,
                "spike_warning_triggered": True,
                "review_save_ok": True,
                "article_save_ok": True,
                "duration_ms": 120000,
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.ai_usage_runs" in query:
            return self.usage_rows[: params[1]]
        return super().fetch_all(query, params)


class LocalAiStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_rows = [
            {
                "id": 201,
                "run_started_at": "2026-07-22T10:00:00Z",
                "run_completed_at": "2026-07-22T10:02:00Z",
                "run_source": "scheduled",
                "shard_index": 3,
                "ai_provider": "local",
                "local_ai_model": "qwen2.5:3b",
                "local_ai_call_count": 9,
                "local_ai_prompt_tokens": 1500,
                "local_ai_completion_tokens": 600,
                "local_ai_total_tokens": 2100,
                "local_ai_accepted_count": 6,
                "local_ai_rejected_count": 3,
                "local_ai_duration_ms": 45000,
                "openai_call_count": 1,
                "ai_reviewed_count": 10,
                "duration_ms": 135000,
            }
        ]
        self.review_rows = [
            {
                "id": 44,
                "reviewed_at": "2026-07-22T10:03:00Z",
                "original_url": "https://publisher.example.com/local-ai-review",
                "source": "Reuters",
                "title": "Local AI review row",
                "decision": "accept",
                "category": "World",
                "positivity_score": 8,
                "summary": "Local model accepted this story.",
                "reason": "Constructive outcome.",
                "ai_provider": "local",
                "ai_model": "qwen2.5:3b",
                "review_duration_ms": 1800,
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.ai_usage_runs" in query:
            return self.usage_rows[: params[2]]
        if "from public.article_ai_reviews" in query:
            return self.review_rows[: params[1]]
        return super().fetch_all(query, params)


class TranslationQualityStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.article_rows = [
            {
                "id": "article-translation-1",
                "source": "Reuters",
                "title": "Helpful climate policy",
                "original_url": "https://publisher.example.com/climate-policy",
                "ai_summary": "A constructive climate policy update.",
                "category": "World",
                "published_on_site_at": "2026-07-22T10:00:00Z",
                "snapshot_rank": 1,
            },
            {
                "id": "article-translation-2",
                "source": "AP",
                "title": "Battery breakthrough",
                "original_url": "https://publisher.example.com/battery-breakthrough",
                "ai_summary": "Scientists improved battery safety.",
                "category": "Science",
                "published_on_site_at": "2026-07-22T09:00:00Z",
                "snapshot_rank": 2,
            },
        ]
        self.summary_rows = [
            {
                "original_url": "https://publisher.example.com/climate-policy",
                "language_code": "fr",
                "title": "Politique climatique utile",
                "summary": "Une mise a jour constructive sur le climat.",
                "updated_at": "2026-07-22T10:05:00Z",
                "generated_by": "openai",
                "model": "gpt-4o-mini",
            },
            {
                "original_url": "https://publisher.example.com/climate-policy",
                "language_code": "ja",
                "title": "Climate policy",
                "summary": "Constructive climate policy update.",
                "updated_at": "2026-07-22T10:06:00Z",
                "generated_by": "openai",
                "model": "gpt-4o-mini",
            },
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.public_feed_snapshot" in query:
            return self.article_rows[: params[0]]
        if "from public.article_summaries" in query:
            return self.summary_rows[: params[2]]
        return super().fetch_all(query, params)


class GuardrailsStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.ai_usage_rows = [
            {
                "run_started_at": "2026-07-22T10:00:00Z",
                "openai_call_count": 8,
                "openai_prompt_tokens": 1200,
                "openai_completion_tokens": 700,
                "openai_total_tokens": 1900,
                "estimated_openai_cost_usd": "0.0031",
                "cost_protection_limit_reached": False,
                "spike_warning_triggered": True,
                "local_ai_call_count": 7,
                "local_ai_total_tokens": 1200,
            }
        ]
        self.worker_rows = [
            {
                "run_started_at": "2026-07-22T10:00:00Z",
                "success": True,
                "shard_index": 2,
                "fetched_count": 100,
                "ai_reviewed_count": 20,
                "accepted_count": 9,
                "rejected_count": 4,
                "duration_ms": 120000,
                "cost_protection_limit_reached": False,
                "spike_warning_triggered": True,
            }
        ]
        self.quota_rows = [
            {
                "event_type": "email_send",
                "quantity": 2,
                "created_at": "2026-07-22T10:10:00Z",
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.ai_usage_runs" in query:
            return self.ai_usage_rows[: params[1]]
        if "from public.worker_runs" in query:
            return self.worker_rows[: params[1]]
        if "from public.quota_usage_events" in query:
            return self.quota_rows[: params[1]]
        return super().fetch_all(query, params)

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "from public.articles" in query:
            return {"article_count": 123}
        if "from public.article_summaries" in query:
            return {"summary_count": 456}
        if "from public.rss_feeds" in query:
            return {"feed_count": 12}
        return super().fetch_one(query, params)


class WorkerShardsStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.worker_rows = [
            {
                "id": 301,
                "created_at": "2026-07-22T09:59:00Z",
                "run_started_at": "2026-07-22T10:00:00Z",
                "run_completed_at": "2026-07-22T10:02:00Z",
                "run_source": "scheduled",
                "request_id": "req-worker-shards",
                "shard_index": 2,
                "feeds_per_shard": 12,
                "max_ai_reviews": 40,
                "success": True,
                "error_name": None,
                "error_message": None,
                "feed_count": 12,
                "fetched_count": 100,
                "candidate_count": 55,
                "already_reviewed_count": 10,
                "unreviewed_count": 45,
                "eligible_for_ai_count": 30,
                "ai_reviewed_count": 20,
                "accepted_count": 9,
                "rejected_count": 4,
                "no_thumbnail_rejected_count": 1,
                "locally_rejected_count": 3,
                "image_hydration_lookup_count": 8,
                "image_hydration_found_count": 6,
                "review_save_ok": True,
                "article_save_ok": True,
                "ai_usage_save_ok": True,
                "cost_protection_limit_reached": False,
                "spike_warning_triggered": True,
                "duration_ms": 120000,
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.worker_runs" in query:
            return self.worker_rows[: params[0]]
        return super().fetch_all(query, params)


class WorkerUpliftHealthStore(WorkerShardsStore):
    def __init__(self, stage_rows: list[dict] | None = None) -> None:
        super().__init__()
        self.stage_rows = stage_rows if stage_rows is not None else [
            worker_uplift_stage_health_row(stage)
            for stage in worker_db_api.WORKER_UPLIFT_STAGES
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from worker_uplift_final.stage_health_projections" in query:
            stage_names, limit = params
            selected = [row for row in self.stage_rows if row.get("stage_name") in stage_names]
            return selected[:limit]
        if "from public.worker_runs" in query:
            return self.worker_rows[: params[0]]
        return FakeStore.fetch_all(self, query, params)


class PartialWorkerUpliftHealthStore(WorkerShardsStore):
    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from worker_uplift_final.stage_health_projections" in query:
            raise RuntimeError("projection unavailable password=do-not-leak RABBITMQ_URL=amqps://secret")
        if "from public.worker_runs" in query:
            return self.worker_rows[: params[0]]
        return FakeStore.fetch_all(self, query, params)


class RssFeedHealthStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.rss_feed_rows = [
            {
                "source": "Reuters",
                "url": "https://publisher.example.com/reuters/rss",
                "is_positive_source": True,
                "is_active": True,
            },
            {
                "source": "Legacy",
                "url": "https://publisher.example.com/legacy/rss",
                "is_positive_source": False,
                "is_active": False,
            },
        ]
        self.feed_health_rows = [
            {
                "id": 401,
                "source": "Reuters",
                "feed_url": "https://publisher.example.com/reuters/rss",
                "last_checked_at": "2026-07-22T10:00:00Z",
                "last_success_at": "2026-07-22T10:00:00Z",
                "last_failure_at": "2026-07-21T10:00:00Z",
                "last_status": 200,
                "last_error_message": None,
                "last_article_count": 12,
                "last_image_count": 10,
                "last_accepted_count": 6,
                "last_rejected_count": 4,
                "consecutive_failure_count": 0,
                "total_fetch_count": 20,
                "total_success_count": 18,
                "total_failure_count": 2,
                "total_article_count": 200,
                "total_image_count": 150,
                "total_accepted_count": 80,
                "total_rejected_count": 40,
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-22T10:01:00Z",
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.rss_feeds" in query:
            return self.rss_feed_rows[: params[0]]
        if "from public.feed_health" in query:
            return self.feed_health_rows[: params[0]]
        return super().fetch_all(query, params)


class FeedManagementStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.feed_quality_rows = [
            {
                "feed_id": 501,
                "source": "Reuters",
                "feed_url": "https://publisher.example.com/reuters/rss",
                "is_active": True,
                "is_positive_source": True,
                "source_trust_tier": "trusted",
                "publisher_allowlist_status": "allowlisted",
                "recommended_trust_tier": "trusted",
                "tier_recommendation_reason": "Consistently high quality.",
                "feed_health_id": 401,
                "last_checked_at": "2026-07-22T10:00:00Z",
                "last_success_at": "2026-07-22T10:00:00Z",
                "last_failure_at": "2026-07-21T10:00:00Z",
                "last_status": 200,
                "last_error_message": None,
                "last_article_count": 12,
                "last_image_count": 10,
                "last_accepted_count": 6,
                "last_rejected_count": 4,
                "consecutive_failure_count": 0,
                "total_fetch_count": 20,
                "total_success_count": 18,
                "total_failure_count": 2,
                "total_article_count": 200,
                "total_image_count": 150,
                "total_accepted_count": 80,
                "total_rejected_count": 40,
                "unique_reviewed_url_count": 120,
                "unique_published_url_count": 75,
                "success_rate_pct": "90",
                "thumbnail_rate_pct": "75",
                "accepted_rate_pct": "66.67",
                "failure_rate_pct": "10",
                "duplicate_rate_pct": "5",
                "quality_score": "91",
                "quality_grade": "excellent",
                "quality_reason": "Strong acceptance and image rates.",
                "updated_at": "2026-07-22T10:01:00Z",
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.feed_quality_scores" in query:
            return self.feed_quality_rows[: params[0]]
        return super().fetch_all(query, params)


class AuditLogStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.audit_rows = [
            {
                "id": "9f3f4286-e859-406c-bc39-4f3356ab3a00",
                "created_at": "2026-07-22T10:00:00Z",
                "actor_email": "admin@example.com",
                "action": "rss_feed.trust_tier_update",
                "target_type": "rss_feed",
                "target_id": "501",
                "target_label": "Reuters",
                "before_values": {
                    "source_trust_tier": "experimental",
                    "publisher_allowlist_status": "candidate",
                },
                "after_values": {
                    "source_trust_tier": "trusted",
                    "publisher_allowlist_status": "allowlisted",
                },
                "metadata": {"reason": "Consistently high quality."},
            }
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.admin_audit_events" in query:
            return self.audit_rows[: params[0]]
        return super().fetch_all(query, params)


class RuntimeFeatureFlagsStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.feature_flag_rows = [
            {
                "key": "reader_archive_search",
                "enabled": True,
                "created_at": "2026-07-12T16:00:00Z",
                "updated_at": "2026-07-22T10:00:00Z",
            },
            {
                "key": "worker_public_feed_edge_snapshot_publish",
                "enabled": False,
                "created_at": "2026-07-12T16:00:00Z",
                "updated_at": "2026-07-22T10:01:00Z",
            },
        ]

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        self.fetches.append((query, params))
        if "from public.runtime_feature_flags" in query:
            limit, offset = params
            return self.feature_flag_rows[offset : offset + limit]
        return super().fetch_all(query, params)


class ReceiptStore(FakeStore):
    def __init__(self, receipt: dict) -> None:
        super().__init__()
        self.receipt = receipt

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "from worker_uplift_final.api_command_receipts" in query:
            return self.receipt
        return None


class StaleAggregateStore(FakeStore):
    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "from worker_uplift_final.api_command_receipts" in query:
            return None
        if "from worker_uplift_final.article_shadow_aggregates" in query:
            return {"aggregate_version": 7}
        return None


class ActiveCutoverStore(FakeStore):
    writes_enabled = True
    worker_uplift_cutover_state = "cutover-approved"
    worker_uplift_production_writes_enabled = True
    worker_uplift_expected_candidate_sha256 = "a" * 64
    worker_uplift_expected_watermark_sha256 = "b" * 64

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        self.fetches.append((query, params))
        if "from worker_uplift_final.api_command_receipts" in query:
            return None
        if "from worker_uplift_final.cutover_control" in query:
            return {
                "state": "cutover_active",
                "active_ingestion_owner": "worker_uplift",
                "legacy_dispatch_enabled": False,
                "uplift_scheduler_enabled": True,
                "uplift_production_writes_enabled": True,
                "publication_write_mode": "production",
                "candidate_sha256": self.worker_uplift_expected_candidate_sha256,
                "watermark_sha256": self.worker_uplift_expected_watermark_sha256,
            }
        return None


class WorkerDbApiTests(unittest.TestCase):
    def test_internal_error_payload_uses_safe_exception_metadata_only(self) -> None:
        class FakeDiag:
            sqlstate = "42P01"

        class FakeDatabaseError(Exception):
            pgcode = "42P01"
            diag = FakeDiag()

        payload = worker_db_api.internal_error_payload(FakeDatabaseError("secret db detail"))

        self.assertEqual("internal database compatibility API error", payload["error"])
        self.assertEqual("FakeDatabaseError", payload["errorClass"])
        self.assertEqual("42P01", payload["pgcode"])
        self.assertEqual("42P01", payload["sqlstate"])
        self.assertTrue(payload["safeMetadataOnly"])
        self.assertNotIn("secret db detail", str(payload))

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

    def test_uplift_shadow_aggregate_records_receipt_without_public_write(self) -> None:
        store = FakeStore()

        result = worker_db_api.handle_operation(
            "uplift-record-shadow-aggregate",
            uplift_shadow_aggregate_body(),
            store,
            auth_scope="worker-uplift-persistence",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("shadow", result["mode"])
        self.assertIs(result["productionSideEffect"], False)
        self.assertEqual(2, len(store.executes))
        aggregate_query, aggregate_params = store.executes[0]
        receipt_query, receipt_params = store.executes[1]
        self.assertIn("insert into worker_uplift_final.article_shadow_aggregates", aggregate_query)
        self.assertIn("on conflict (article_identity_hash, aggregate_version)", aggregate_query)
        self.assertNotIn("public.articles", aggregate_query)
        self.assertEqual("article-hash-1", aggregate_params[0])
        self.assertEqual(["fr", "ja"], aggregate_params[10])
        self.assertEqual("ready", aggregate_params[11])
        self.assertIn("insert into worker_uplift_final.api_command_receipts", receipt_query)
        self.assertEqual("idem-uplift-1", receipt_params[0])
        self.assertEqual("worker-uplift-persistence", receipt_params[5])
        self.assertEqual("recorded_success", receipt_params[15])

    def test_uplift_shadow_aggregate_rejects_stale_version(self) -> None:
        store = StaleAggregateStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "uplift-record-shadow-aggregate",
                uplift_shadow_aggregate_body(),
                store,
                auth_scope="worker-uplift-persistence",
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_uplift_shadow_command_records_receipt_without_delegate_write(self) -> None:
        store = FakeStore()

        result = worker_db_api.handle_operation(
            "uplift-save-worker-run",
            uplift_metadata({"run": {"request_id": "run-1", "duration_ms": 10}}),
            store,
            auth_scope="worker-uplift-persistence",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(store.executes))
        query, _params = store.executes[0]
        self.assertIn("worker_uplift_final.api_command_receipts", query)
        self.assertNotIn("public.worker_runs", query)

    def test_uplift_duplicate_request_returns_recorded_response(self) -> None:
        body = uplift_metadata()
        digest = worker_db_api.uplift_payload_digest("uplift-save-worker-run", body)
        store = ReceiptStore(
            {
                "operation": "uplift-save-worker-run",
                "payload_digest": digest,
                "response_json": {"ok": True, "recorded": True},
            }
        )

        result = worker_db_api.handle_operation(
            "uplift-save-worker-run",
            body,
            store,
            auth_scope="worker-uplift-persistence",
        )

        self.assertEqual({"ok": True, "recorded": True, "duplicate": True}, result)
        self.assertEqual([], store.executes)

    def test_uplift_idempotency_reuse_with_different_payload_conflicts(self) -> None:
        store = ReceiptStore(
            {
                "operation": "uplift-save-worker-run",
                "payload_digest": "different-digest",
                "response_json": {"ok": True},
            }
        )

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "uplift-save-worker-run",
                uplift_metadata(),
                store,
                auth_scope="worker-uplift-persistence",
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_uplift_scoped_token_denies_unrelated_operations(self) -> None:
        store = FakeStore()

        with self.assertRaises(worker_db_api.ApiError) as publication_error:
            worker_db_api.handle_operation(
                "uplift-save-accepted-articles-batch",
                uplift_metadata({"actorService": "worker-uplift-publication"}),
                store,
                auth_scope="worker-uplift-publication",
            )
        with self.assertRaises(worker_db_api.ApiError) as legacy_error:
            worker_db_api.handle_operation(
                "uplift-save-worker-run",
                uplift_metadata(),
                store,
                auth_scope=worker_db_api.LEGACY_WORKER_API_SCOPE,
            )
        with self.assertRaises(worker_db_api.ApiError) as scoped_legacy_error:
            worker_db_api.handle_operation(
                "save-worker-run",
                {"providerMode": "backend_postgres_primary", "run": {"request_id": "x"}},
                store,
                auth_scope="worker-uplift-persistence",
            )

        self.assertEqual(403, publication_error.exception.status)
        self.assertEqual(403, legacy_error.exception.status)
        self.assertEqual(403, scoped_legacy_error.exception.status)
        self.assertEqual([], store.executes)

    def test_uplift_primary_command_requires_cutover_state(self) -> None:
        store = FakeStore()
        store.writes_enabled = True
        store.worker_uplift_cutover_state = "shadow"
        store.worker_uplift_production_writes_enabled = True

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "uplift-save-worker-run",
                uplift_metadata({"providerMode": "backend_postgres_primary", "run": {"request_id": "run-1"}}),
                store,
                auth_scope="worker-uplift-persistence",
            )

        self.assertEqual(403, error.exception.status)
        self.assertEqual([], store.executes)

    def test_uplift_primary_command_requires_matching_database_single_writer_control(self) -> None:
        digest = "a" * 64
        watermark = "b" * 64
        store = FakeStore()
        store.writes_enabled = True
        store.worker_uplift_cutover_state = "cutover-approved"
        store.worker_uplift_production_writes_enabled = True
        store.worker_uplift_expected_candidate_sha256 = digest
        store.worker_uplift_expected_watermark_sha256 = watermark
        store.fetch_one = lambda _query, _params=(): {
            "state": "cutover_active",
            "active_ingestion_owner": "worker_uplift",
            "legacy_dispatch_enabled": False,
            "uplift_scheduler_enabled": True,
            "uplift_production_writes_enabled": True,
            "publication_write_mode": "production",
            "candidate_sha256": digest,
            "watermark_sha256": watermark,
        }

        worker_db_api.assert_worker_uplift_production_allowed(store)

        store.worker_uplift_expected_watermark_sha256 = "c" * 64
        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.assert_worker_uplift_production_allowed(store)
        self.assertEqual(403, error.exception.status)

    def test_uplift_primary_command_fails_closed_when_control_read_fails(self) -> None:
        store = FakeStore()
        store.writes_enabled = True
        store.worker_uplift_cutover_state = "cutover-approved"
        store.worker_uplift_production_writes_enabled = True
        store.worker_uplift_expected_candidate_sha256 = "a" * 64
        store.worker_uplift_expected_watermark_sha256 = "b" * 64

        def failed_read(_query, _params=()):
            raise RuntimeError("redacted")

        store.fetch_one = failed_read
        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.assert_worker_uplift_production_allowed(store)
        self.assertEqual(403, error.exception.status)

    def test_uplift_publication_requires_exact_single_article_scope(self) -> None:
        invalid_bodies = [
            uplift_publication_body({"originalUrls": []}),
            uplift_publication_body({
                "originalUrls": [
                    "https://example.com/article-1",
                    "https://example.com/article-2",
                ]
            }),
            uplift_publication_body({"status": "translation_pending"}),
            uplift_publication_body({"languageCodes": ["fr", "ja"]}),
            uplift_publication_body({"originalUrls": ["shadow://article/article-1"]}),
        ]

        for body in invalid_bodies:
            store = FakeStore()
            with self.subTest(body=body):
                with self.assertRaises(worker_db_api.ApiError) as error:
                    worker_db_api.handle_operation(
                        "uplift-publish-articles-batch",
                        body,
                        store,
                        auth_scope="worker-uplift-publication",
                    )
                self.assertEqual(400, error.exception.status)
                self.assertEqual([], store.fetches)
                self.assertEqual([], store.executes)

    def test_uplift_publication_confirms_one_published_article_before_receipt(self) -> None:
        store = ActiveCutoverStore(
            fetch_all_results=[
                [],
                [{"original_url": "https://example.com/article-1"}],
            ]
        )

        result = worker_db_api.handle_operation(
            "uplift-publish-articles-batch",
            uplift_publication_body(),
            store,
            auth_scope="worker-uplift-publication",
        )

        self.assertEqual(
            {
                "ok": True,
                "requestedCount": 1,
                "publishedCount": 1,
                "blockedCount": 0,
                "missingTranslations": [],
            },
            result,
        )
        self.assertEqual(1, len(store.executes))
        receipt_query, receipt_params = store.executes[0]
        self.assertIn("worker_uplift_final.api_command_receipts", receipt_query)
        self.assertEqual("worker-uplift-publication", receipt_params[5])
        self.assertEqual("applied_success", receipt_params[15])

    def test_uplift_persistence_accepts_only_one_article_and_exact_five_summary_rows(self) -> None:
        article_store = ActiveCutoverStore()
        article_result = worker_db_api.handle_operation(
            "uplift-save-accepted-articles-batch",
            uplift_accepted_article_body(),
            article_store,
            auth_scope="worker-uplift-persistence",
        )
        self.assertEqual({"ok": True, "articleCount": 1}, article_result)
        self.assertIn("insert into public.articles", article_store.executes[0][0])

        summary_store = ActiveCutoverStore()
        summary_result = worker_db_api.handle_operation(
            "uplift-save-article-summaries-batch",
            uplift_article_summaries_body(),
            summary_store,
            auth_scope="worker-uplift-persistence",
        )
        self.assertEqual({"ok": True, "summaryCount": 5}, summary_result)
        self.assertIn("insert into public.article_summaries", summary_store.executes[0][0])

    def test_uplift_persistence_rejects_non_http_and_non_exact_materialization_scope(self) -> None:
        invalid_cases = [
            ("uplift-save-accepted-articles-batch", uplift_accepted_article_body({"articles": []})),
            ("uplift-save-accepted-articles-batch", uplift_accepted_article_body({
                "articles": [{**uplift_accepted_article_body()["articles"][0], "original_url": "shadow://article/1"}],
            })),
            ("uplift-save-article-summaries-batch", uplift_article_summaries_body({"summaries": []})),
            ("uplift-save-article-summaries-batch", uplift_article_summaries_body({
                "summaries": list(reversed(uplift_article_summaries_body()["summaries"])),
            })),
        ]
        for operation, body in invalid_cases:
            with self.subTest(operation=operation):
                store = ActiveCutoverStore()
                with self.assertRaises(worker_db_api.ApiError) as error:
                    worker_db_api.handle_operation(
                        operation,
                        body,
                        store,
                        auth_scope="worker-uplift-persistence",
                    )
                self.assertEqual(400, error.exception.status)
                self.assertEqual([], store.executes)

    def test_uplift_publication_retry_ignores_delivery_metadata_in_business_digest(self) -> None:
        first_body = uplift_publication_body()
        retry_body = uplift_publication_body({
            "messageId": "message-retry",
            "correlationId": "correlation-retry",
            "pipelineRunId": "pipeline-retry",
            "stageExecutionId": "stage-retry",
            "sourceMessageId": "source-retry",
        })
        existing_response = {
            "ok": True,
            "requestedCount": 1,
            "publishedCount": 1,
            "blockedCount": 0,
            "missingTranslations": [],
        }
        store = ReceiptStore({
            "operation": "uplift-publish-articles-batch",
            "payload_digest": worker_db_api.uplift_payload_digest(
                "uplift-publish-articles-batch",
                first_body,
            ),
            "response_json": existing_response,
        })

        result = worker_db_api.handle_operation(
            "uplift-publish-articles-batch",
            retry_body,
            store,
            auth_scope="worker-uplift-publication",
        )

        self.assertEqual({**existing_response, "duplicate": True}, result)
        self.assertEqual([], store.executes)

    def test_uplift_publication_retry_rejects_changed_business_scope(self) -> None:
        first_body = uplift_publication_body()
        store = ReceiptStore({
            "operation": "uplift-publish-articles-batch",
            "payload_digest": worker_db_api.uplift_payload_digest(
                "uplift-publish-articles-batch",
                first_body,
            ),
            "response_json": {"ok": True},
        })

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "uplift-publish-articles-batch",
                uplift_publication_body({"originalUrls": ["https://example.com/article-2"]}),
                store,
                auth_scope="worker-uplift-publication",
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_uplift_publication_rejects_missing_domain_row_without_receipt(self) -> None:
        store = ActiveCutoverStore(fetch_all_results=[[], []])

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_operation(
                "uplift-publish-articles-batch",
                uplift_publication_body(),
                store,
                auth_scope="worker-uplift-publication",
            )

        self.assertEqual(409, error.exception.status)
        self.assertEqual([], store.executes)

    def test_scoped_tokens_are_distinct_and_resolve_to_scope(self) -> None:
        with patch.dict(
            worker_db_api.os.environ,
            {
                "NUTSNEWS_BACKEND_API_TOKEN": "legacy-token",
                "NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN": "persistence-token",
                "NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN": "publication-token",
            },
            clear=False,
        ):
            self.assertEqual(
                "worker-uplift-persistence",
                worker_db_api.authenticate_database_api_token("Bearer persistence-token"),
            )

        with patch.dict(
            worker_db_api.os.environ,
            {
                "NUTSNEWS_BACKEND_API_TOKEN": "same-token",
                "NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN": "same-token",
            },
            clear=True,
        ):
            with self.assertRaises(worker_db_api.ApiError) as error:
                worker_db_api.authenticate_database_api_token("Bearer same-token")
            self.assertEqual(503, error.exception.status)

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

    def test_app_admin_article_reviews_returns_dashboard_snapshot(self) -> None:
        store = ArticleReviewsStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-article-reviews",
            {
                "providerMode": "backend_postgres_primary",
                "filters": {
                    "decision": "reject",
                    "source": "Reuters",
                    "category": "World",
                    "minScore": 4,
                    "maxScore": 8,
                    "page": 1,
                    "sort": "oldest",
                },
                "pageSize": 2,
                "recentPublishedArticleLimit": 1,
                "aiDecisionVersionReportLimit": 1,
                "maxOptionRows": 3,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(["Reuters", "TechCrunch"], row["sourceOptions"])
        self.assertEqual(["Science", "World"], row["categoryOptions"])
        self.assertEqual(store.recent_articles, row["recentPublishedArticleRows"])
        self.assertEqual(store.recent_review_rows, row["recentPublishedReviewRows"])
        self.assertEqual(store.version_rows, row["versionReportRows"])
        self.assertIsNone(row["versionReportError"])
        self.assertEqual(store.review_rows, row["reviewRows"])
        self.assertEqual(store.published_review_articles, row["publishedArticlesForReviews"])
        self.assertEqual(125, row["totalMatchingReviews"])
        self.assertIsNone(row["reviewError"])
        self.assertEqual([], store.executes)

        count_query, count_params = next(
            (query, params)
            for query, params in store.fetches
            if "count(*)::bigint as total_matching_reviews" in query
        )
        self.assertIn("decision = %s", count_query)
        self.assertIn("source = %s", count_query)
        self.assertIn("category ilike %s", count_query)
        self.assertIn("positivity_score >= %s", count_query)
        self.assertIn("positivity_score <= %s", count_query)
        self.assertEqual(("reject", "Reuters", "%World%", 4, 8), count_params)

        review_query, review_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.article_ai_reviews" in query and "order by reviewed_at asc" in query
        )
        self.assertIn("prompt_version", review_query)
        self.assertIn("model_version", review_query)
        self.assertIn("limit %s offset %s", review_query)
        self.assertEqual(("reject", "Reuters", "%World%", 4, 8, 2, 2), review_params)

        recent_query, recent_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.articles" in query and "where status = 'published'" in query
        )
        self.assertIn("published_on_site_at desc nulls last", recent_query)
        self.assertEqual((1,), recent_params)

    def test_app_admin_article_engagement_returns_dashboard_snapshot(self) -> None:
        store = ArticleEngagementStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-article-engagement",
            {
                "providerMode": "backend_postgres_primary",
                "sourceCategoryLimit": 2,
                "articleLimit": 1,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.source_category_rows, row["sourceCategoryRows"])
        self.assertIsNone(row["sourceCategoryError"])
        self.assertEqual(store.article_rows, row["articleRows"])
        self.assertIsNone(row["articleError"])
        self.assertEqual([], store.executes)

        source_query, source_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.article_engagement_source_category_summary" in query
        )
        self.assertIn("outbound_click_count", source_query)
        self.assertIn("category_interest_count", source_query)
        self.assertIn("order by total_engagement_count desc", source_query)
        self.assertIn("latest_event_date desc nulls last", source_query)
        self.assertEqual((2,), source_params)

        article_query, article_params = next(
            (query, params)
            for query, params in store.fetches
            if "from public.article_engagement_article_summary" in query
        )
        self.assertIn("article_id", article_query)
        self.assertIn("original_url", article_query)
        self.assertIn("order by outbound_click_count desc", article_query)
        self.assertEqual((1,), article_params)

    def test_app_admin_ai_usage_returns_dashboard_snapshot(self) -> None:
        store = AiUsageStore()
        since = "2026-06-22T00:00:00.000Z"

        result = worker_db_api.handle_app_operation(
            "load-admin-ai-usage",
            {
                "providerMode": "backend_postgres_primary",
                "since": since,
                "limit": 2,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.usage_rows, row["usageRunRows"])
        self.assertEqual([], store.executes)

        query, params = store.fetches[0]
        self.assertIn("from public.ai_usage_runs", query)
        self.assertIn("created_at", query)
        self.assertIn("openai_review_count", query)
        self.assertIn("openai_translation_total_tokens", query)
        self.assertIn("local_ai_review_count", query)
        self.assertIn("local_ai_translation_total_tokens", query)
        self.assertIn("estimated_local_ai_savings_usd", query)
        self.assertIn("where run_started_at >= %s::timestamptz", query)
        self.assertIn("order by run_started_at desc nulls last, id desc", query)
        self.assertEqual((since, 2), params)

    def test_app_admin_local_ai_returns_dashboard_snapshot(self) -> None:
        store = LocalAiStore()
        since = "2026-06-22T00:00:00.000Z"

        result = worker_db_api.handle_app_operation(
            "load-admin-local-ai",
            {
                "providerMode": "backend_postgres_primary",
                "since": since,
                "runLimit": 2,
                "reviewLimit": 1,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.usage_rows, row["usageRunRows"])
        self.assertEqual(store.review_rows, row["recentReviewRows"])
        self.assertEqual([], store.executes)

        run_query, run_params = store.fetches[0]
        self.assertIn("from public.ai_usage_runs", run_query)
        self.assertIn("ai_provider", run_query)
        self.assertIn("local_ai_model", run_query)
        self.assertIn("local_ai_duration_ms", run_query)
        self.assertIn("openai_call_count", run_query)
        self.assertIn("(ai_provider = %s or local_ai_call_count > 0)", run_query)
        self.assertIn("run_started_at >= %s::timestamptz", run_query)
        self.assertIn("order by run_started_at desc nulls last, id desc", run_query)
        self.assertEqual(("local", since, 2), run_params)

        review_query, review_params = store.fetches[1]
        self.assertIn("from public.article_ai_reviews", review_query)
        self.assertIn("original_url", review_query)
        self.assertIn("review_duration_ms", review_query)
        self.assertIn("where ai_provider = %s", review_query)
        self.assertIn("order by reviewed_at desc nulls last, id desc", review_query)
        self.assertEqual(("local", 1), review_params)

    def test_app_admin_translation_quality_returns_dashboard_snapshot(self) -> None:
        store = TranslationQualityStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-translation-quality",
            {
                "providerMode": "backend_postgres_primary",
                "auditLimit": 2,
                "summaryLookupLimit": 3,
                "targetLanguageCodes": ["fr", "ja", "en", "fr"],
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.article_rows, row["articleRows"])
        self.assertEqual(store.summary_rows, row["summaryRows"])
        self.assertEqual([], store.executes)

        article_query, article_params = store.fetches[0]
        self.assertIn("from public.public_feed_snapshot", article_query)
        self.assertIn("snapshot_rank", article_query)
        self.assertIn("original_url", article_query)
        self.assertIn("ai_summary", article_query)
        self.assertIn("order by snapshot_rank asc", article_query)
        self.assertEqual((2,), article_params)

        summary_query, summary_params = store.fetches[1]
        self.assertIn("from public.article_summaries", summary_query)
        self.assertIn("language_code", summary_query)
        self.assertIn("generated_by", summary_query)
        self.assertIn("model", summary_query)
        self.assertIn("original_url = any(%s)", summary_query)
        self.assertIn("language_code = any(%s)", summary_query)
        self.assertEqual(
            (
                [
                    "https://publisher.example.com/climate-policy",
                    "https://publisher.example.com/battery-breakthrough",
                ],
                ["fr", "ja"],
                3,
            ),
            summary_params,
        )

    def test_app_admin_guardrails_returns_dashboard_snapshot(self) -> None:
        store = GuardrailsStore()
        since = "2026-06-22T00:00:00.000Z"

        result = worker_db_api.handle_app_operation(
            "load-admin-guardrails",
            {
                "providerMode": "backend_postgres_primary",
                "since": since,
                "limit": 2,
                "countTables": ["articles", "article_summaries", "rss_feeds"],
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.ai_usage_rows, row["aiUsageRunRows"])
        self.assertEqual(store.worker_rows, row["workerRunRows"])
        self.assertEqual(store.quota_rows, row["quotaUsageEventRows"])
        self.assertEqual(123, row["articleCount"])
        self.assertEqual(456, row["summaryCount"])
        self.assertEqual(12, row["feedCount"])
        self.assertEqual([], row["partialErrors"])
        self.assertEqual([], store.executes)

        ai_query, ai_params = store.fetches[0]
        self.assertIn("from public.ai_usage_runs", ai_query)
        self.assertIn("openai_call_count", ai_query)
        self.assertIn("estimated_openai_cost_usd", ai_query)
        self.assertIn("local_ai_total_tokens", ai_query)
        self.assertIn("where run_started_at >= %s::timestamptz", ai_query)
        self.assertIn("order by run_started_at desc nulls last, id desc", ai_query)
        self.assertEqual((since, 2), ai_params)

        worker_query, worker_params = store.fetches[1]
        self.assertIn("from public.worker_runs", worker_query)
        self.assertIn("success", worker_query)
        self.assertIn("accepted_count", worker_query)
        self.assertIn("cost_protection_limit_reached", worker_query)
        self.assertIn("where run_started_at >= %s::timestamptz", worker_query)
        self.assertIn("order by run_started_at desc nulls last, id desc", worker_query)
        self.assertEqual((since, 2), worker_params)

        quota_query, quota_params = store.fetches[2]
        self.assertIn("from public.quota_usage_events", quota_query)
        self.assertIn("event_type", quota_query)
        self.assertIn("quantity", quota_query)
        self.assertIn("where created_at >= %s::timestamptz", quota_query)
        self.assertIn("order by created_at desc nulls last, id desc", quota_query)
        self.assertEqual((since, 2), quota_params)

        self.assertTrue(any("from public.articles" in query for query, _ in store.fetches))
        self.assertTrue(any("from public.article_summaries" in query for query, _ in store.fetches))
        self.assertTrue(any("from public.rss_feeds" in query for query, _ in store.fetches))

    def test_app_admin_guardrails_rejects_unsupported_count_table(self) -> None:
        store = GuardrailsStore()

        with self.assertRaises(worker_db_api.ApiError) as error:
            worker_db_api.handle_app_operation(
                "load-admin-guardrails",
                {
                    "providerMode": "backend_postgres_primary",
                    "since": "2026-06-22T00:00:00.000Z",
                    "countTables": ["articles", "auth.users"],
                },
                store,
            )

        self.assertEqual(400, error.exception.status)
        self.assertEqual([], store.fetches)
        self.assertEqual([], store.executes)

    def test_app_admin_worker_shards_returns_dashboard_snapshot(self) -> None:
        store = WorkerShardsStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-shards",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 2,
                "shardCount": 25,
                "staleAfterMinutes": 180,
                "slowRunMs": 15000,
                "dailyWindowDays": 7,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.worker_rows, row["workerRunRows"])
        self.assertEqual([], store.executes)

        query, params = store.fetches[0]
        self.assertIn("from public.worker_runs", query)
        self.assertIn("id", query)
        self.assertIn("created_at", query)
        self.assertIn("run_started_at", query)
        self.assertIn("run_completed_at", query)
        self.assertIn("request_id", query)
        self.assertIn("shard_index", query)
        self.assertIn("feeds_per_shard", query)
        self.assertIn("max_ai_reviews", query)
        self.assertIn("success", query)
        self.assertIn("error_name", query)
        self.assertIn("error_message", query)
        self.assertIn("already_reviewed_count", query)
        self.assertIn("unreviewed_count", query)
        self.assertIn("eligible_for_ai_count", query)
        self.assertIn("no_thumbnail_rejected_count", query)
        self.assertIn("locally_rejected_count", query)
        self.assertIn("image_hydration_lookup_count", query)
        self.assertIn("image_hydration_found_count", query)
        self.assertIn("review_save_ok", query)
        self.assertIn("article_save_ok", query)
        self.assertIn("ai_usage_save_ok", query)
        self.assertIn("cost_protection_limit_reached", query)
        self.assertIn("spike_warning_triggered", query)
        self.assertIn("duration_ms", query)
        self.assertIn("order by run_started_at desc nulls last, id desc", query)
        self.assertEqual((2,), params)

        health = row["workerUpliftHealth"]
        self.assertEqual(1, health["schemaVersion"])
        self.assertEqual("legacy_shards", health["activeIngestionOwner"])
        self.assertFalse(health["grafanaDependency"])
        self.assertEqual("legacy_only", health["overallStatus"])

    def test_app_admin_worker_uplift_health_returns_coexistence_projection(self) -> None:
        store = WorkerUpliftHealthStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {
                "providerMode": "backend_postgres_primary",
                "activeIngestionOwner": "legacy_shards",
                "stageLimit": 8,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        health = result["rows"][0]["workerUpliftHealth"]
        self.assertEqual("backend_postgres_durable_projection", health["source"])
        self.assertEqual("legacy_shards", health["activeIngestionOwner"])
        self.assertEqual("healthy", health["overallStatus"])
        self.assertFalse(health["grafanaDependency"])
        self.assertEqual([], health["partialErrors"])
        self.assertEqual(8, len(health["stageRows"]))
        fetcher = next(row for row in health["stageRows"] if row["stage"] == "fetcher")
        self.assertEqual("healthy", fetcher["stageStatus"])
        self.assertEqual(2, fetcher["activeConsumers"])
        self.assertEqual(7, fetcher["queueAgeSeconds"])
        self.assertEqual("fetcher-sha-123", fetcher["deploymentVersion"])
        query, params = store.fetches[0]
        self.assertIn("from worker_uplift_final.stage_health_projections", query)
        self.assertIn("stage_name = any(%s)", query)
        self.assertEqual((list(worker_db_api.WORKER_UPLIFT_STAGES), 8), params)

    def test_app_admin_worker_uplift_health_handles_legacy_only_state(self) -> None:
        store = WorkerUpliftHealthStore(stage_rows=[])

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {
                "providerMode": "backend_postgres_primary",
                "activeIngestionOwner": "legacy_shards",
            },
            store,
        )

        health = result["rows"][0]["workerUpliftHealth"]
        self.assertEqual("legacy_only", health["overallStatus"])
        self.assertTrue(all(row["stageStatus"] == "legacy_only" for row in health["stageRows"]))

    def test_app_admin_worker_uplift_health_handles_uplift_primary_state(self) -> None:
        store = WorkerUpliftHealthStore(
            [
                worker_uplift_stage_health_row(
                    stage,
                    {"active_ingestion_owner": "worker_uplift"},
                )
                for stage in worker_db_api.WORKER_UPLIFT_STAGES
            ]
        )
        store.worker_uplift_cutover_state = "cutover-approved"
        store.worker_uplift_production_writes_enabled = True

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {"providerMode": "backend_postgres_primary"},
            store,
        )

        health = result["rows"][0]["workerUpliftHealth"]
        self.assertEqual("worker_uplift", health["activeIngestionOwner"])
        self.assertEqual("cutover-approved", health["cutoverState"])
        self.assertTrue(health["productionWritesEnabled"])
        self.assertEqual("healthy", health["overallStatus"])

    def test_app_admin_worker_uplift_health_identifies_stalled_stage_and_dlq(self) -> None:
        store = WorkerUpliftHealthStore(
            [
                worker_uplift_stage_health_row(stage)
                for stage in worker_db_api.WORKER_UPLIFT_STAGES
            ]
        )
        store.stage_rows[2] = worker_uplift_stage_health_row(
            "canonicalizer",
            {
                "stage_status": "healthy",
                "stale_status": "stale",
                "consecutive_failure_count": 3,
                "retry_count": 9,
                "dlq_count": 2,
                "queue_age_seconds": 1800,
                "sanitized_error_code": "canonicalizer_timeout",
                "sanitized_error_message": "canonicalizer timed out after retry budget",
            },
        )
        store.stage_rows[3] = worker_uplift_stage_health_row(
            "enrichment",
            {
                "stage_status": "healthy",
                "consecutive_failure_count": 1,
                "dlq_count": 0,
            },
        )

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {"providerMode": "backend_postgres_primary"},
            store,
        )

        health = result["rows"][0]["workerUpliftHealth"]
        canonicalizer = next(row for row in health["stageRows"] if row["stage"] == "canonicalizer")
        self.assertEqual("degraded", canonicalizer["stageStatus"])
        self.assertEqual("stale", canonicalizer["staleStatus"])
        self.assertEqual(2, canonicalizer["dlqCount"])
        self.assertEqual(1800, canonicalizer["queueAgeSeconds"])
        self.assertEqual("canonicalizer_timeout", canonicalizer["errorClass"])
        enrichment = next(row for row in health["stageRows"] if row["stage"] == "enrichment")
        self.assertEqual("degraded", enrichment["stageStatus"])
        self.assertEqual("degraded", health["overallStatus"])

    def test_app_admin_worker_uplift_health_redacts_partial_telemetry_errors(self) -> None:
        store = PartialWorkerUpliftHealthStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {"providerMode": "backend_postgres_primary"},
            store,
        )

        health = result["rows"][0]["workerUpliftHealth"]
        self.assertEqual("partial", health["overallStatus"])
        self.assertEqual(
            [
                {
                    "source": "worker_uplift_final.stage_health_projections",
                    "errorClass": "RuntimeError",
                    "redacted": True,
                }
            ],
            health["partialErrors"],
        )
        self.assertNotIn("do-not-leak", str(health))
        self.assertNotIn("amqps://secret", str(health))

    def test_app_admin_worker_uplift_health_handles_rollback_state(self) -> None:
        store = WorkerUpliftHealthStore(stage_rows=[])
        store.worker_uplift_cutover_state = "rollback-active"

        result = worker_db_api.handle_app_operation(
            "load-admin-worker-uplift-health",
            {"providerMode": "backend_postgres_primary"},
            store,
        )

        health = result["rows"][0]["workerUpliftHealth"]
        self.assertEqual("rollback", health["activeIngestionOwner"])
        self.assertEqual("rollback", health["overallStatus"])
        self.assertTrue(all(row["stageStatus"] == "rollback" for row in health["stageRows"]))

    def test_app_admin_rss_feed_health_returns_dashboard_snapshot(self) -> None:
        store = RssFeedHealthStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-rss-feed-health",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 2,
                "staleAfterHours": 24,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.rss_feed_rows, row["rssFeedRows"])
        self.assertEqual(store.feed_health_rows, row["feedHealthRows"])
        self.assertEqual([], store.executes)

        rss_query, rss_params = store.fetches[0]
        self.assertIn("from public.rss_feeds", rss_query)
        self.assertIn("source", rss_query)
        self.assertIn("url", rss_query)
        self.assertIn("is_positive_source", rss_query)
        self.assertIn("is_active", rss_query)
        self.assertIn("order by id asc", rss_query)
        self.assertEqual((2,), rss_params)

        health_query, health_params = store.fetches[1]
        self.assertIn("from public.feed_health", health_query)
        self.assertIn("last_checked_at", health_query)
        self.assertIn("last_success_at", health_query)
        self.assertIn("last_failure_at", health_query)
        self.assertIn("last_status", health_query)
        self.assertIn("last_error_message", health_query)
        self.assertIn("last_article_count", health_query)
        self.assertIn("last_image_count", health_query)
        self.assertIn("last_accepted_count", health_query)
        self.assertIn("last_rejected_count", health_query)
        self.assertIn("consecutive_failure_count", health_query)
        self.assertIn("total_fetch_count", health_query)
        self.assertIn("total_success_count", health_query)
        self.assertIn("total_failure_count", health_query)
        self.assertIn("total_article_count", health_query)
        self.assertIn("total_image_count", health_query)
        self.assertIn("total_accepted_count", health_query)
        self.assertIn("total_rejected_count", health_query)
        self.assertIn("created_at", health_query)
        self.assertIn("updated_at", health_query)
        self.assertIn("order by total_accepted_count desc nulls last, id desc", health_query)
        self.assertEqual((2,), health_params)

    def test_app_admin_feed_management_returns_dashboard_snapshot(self) -> None:
        store = FeedManagementStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-feed-management",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 2,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.feed_quality_rows, row["feedQualityRows"])
        self.assertEqual([], store.executes)

        query, params = store.fetches[0]
        self.assertIn("from public.feed_quality_scores", query)
        self.assertIn("feed_id", query)
        self.assertIn("source", query)
        self.assertIn("feed_url", query)
        self.assertIn("is_active", query)
        self.assertIn("is_positive_source", query)
        self.assertIn("source_trust_tier", query)
        self.assertIn("publisher_allowlist_status", query)
        self.assertIn("recommended_trust_tier", query)
        self.assertIn("tier_recommendation_reason", query)
        self.assertIn("feed_health_id", query)
        self.assertIn("last_checked_at", query)
        self.assertIn("last_success_at", query)
        self.assertIn("last_failure_at", query)
        self.assertIn("last_status", query)
        self.assertIn("last_error_message", query)
        self.assertIn("last_article_count", query)
        self.assertIn("last_image_count", query)
        self.assertIn("last_accepted_count", query)
        self.assertIn("last_rejected_count", query)
        self.assertIn("consecutive_failure_count", query)
        self.assertIn("total_fetch_count", query)
        self.assertIn("total_success_count", query)
        self.assertIn("total_failure_count", query)
        self.assertIn("total_article_count", query)
        self.assertIn("total_image_count", query)
        self.assertIn("total_accepted_count", query)
        self.assertIn("total_rejected_count", query)
        self.assertIn("unique_reviewed_url_count", query)
        self.assertIn("unique_published_url_count", query)
        self.assertIn("success_rate_pct", query)
        self.assertIn("thumbnail_rate_pct", query)
        self.assertIn("accepted_rate_pct", query)
        self.assertIn("failure_rate_pct", query)
        self.assertIn("duplicate_rate_pct", query)
        self.assertIn("quality_score", query)
        self.assertIn("quality_grade", query)
        self.assertIn("quality_reason", query)
        self.assertIn("updated_at", query)
        self.assertIn("order by quality_score asc nulls first", query)
        self.assertIn("total_accepted_count desc nulls last", query)
        self.assertEqual((2,), params)

    def test_app_admin_audit_log_returns_dashboard_snapshot(self) -> None:
        store = AuditLogStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-audit-log",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 2,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        row = result["rows"][0]
        self.assertEqual(store.audit_rows, row["auditEventRows"])
        self.assertEqual([], store.executes)

        query, params = store.fetches[0]
        self.assertIn("from public.admin_audit_events", query)
        self.assertIn("id", query)
        self.assertIn("created_at", query)
        self.assertIn("actor_email", query)
        self.assertIn("action", query)
        self.assertIn("target_type", query)
        self.assertIn("target_id", query)
        self.assertIn("target_label", query)
        self.assertIn("before_values", query)
        self.assertIn("after_values", query)
        self.assertIn("metadata", query)
        self.assertIn("order by created_at desc nulls last, id desc", query)
        self.assertEqual((2,), params)

    def test_app_admin_runtime_feature_flags_returns_rows_envelope(self) -> None:
        store = RuntimeFeatureFlagsStore()

        result = worker_db_api.handle_app_operation(
            "load-admin-runtime-feature-flags",
            {
                "providerMode": "backend_postgres_primary",
                "limit": 1,
                "offset": 1,
            },
            store,
        )

        self.assertEqual(1, result["rowCount"])
        self.assertIn("generatedAt", result)
        self.assertEqual(store.feature_flag_rows[1:], result["rows"])
        self.assertEqual([], store.executes)

        query, params = store.fetches[0]
        self.assertIn("from public.runtime_feature_flags", query)
        self.assertIn("key", query)
        self.assertIn("enabled", query)
        self.assertIn("created_at", query)
        self.assertIn("updated_at", query)
        self.assertIn("order by key asc", query)
        self.assertEqual((1, 1), params)

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
