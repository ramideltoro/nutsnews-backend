#!/usr/bin/env python3
"""Loopback-only NutsNews database compatibility API."""

from __future__ import annotations

import hmac
import hashlib
import json
import math
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


READ_OPERATIONS = {
    "load-feeds-for-shard",
    "load-reviewed-url-rows",
    "load-published-article-url-rows",
    "load-article-count-for-backpressure",
    "load-public-feed-snapshot-rows",
    "load-existing-summary-language-rows",
    "load-summary-translation-recovery-articles",
    "load-feed-health-snapshots",
    "get-runtime-feature-flag",
}

WRITE_OPERATIONS = {
    "save-article-summaries-batch",
    "save-feed-health-batch",
    "save-article-reviews-batch",
    "save-accepted-articles-batch",
    "publish-articles-batch",
    "refresh-public-feed-snapshot",
    "save-ai-usage-run",
    "save-worker-run",
    "uplift-record-shadow-aggregate",
    "uplift-save-accepted-articles-batch",
    "uplift-save-article-summaries-batch",
    "uplift-save-article-reviews-batch",
    "uplift-save-feed-health-batch",
    "uplift-save-ai-usage-run",
    "uplift-save-worker-run",
    "uplift-publish-articles-batch",
    "uplift-refresh-public-feed-snapshot",
}

WORKER_UPLIFT_WRITE_OPERATIONS = {
    "uplift-record-shadow-aggregate",
    "uplift-save-accepted-articles-batch",
    "uplift-save-article-summaries-batch",
    "uplift-save-article-reviews-batch",
    "uplift-save-feed-health-batch",
    "uplift-save-ai-usage-run",
    "uplift-save-worker-run",
    "uplift-publish-articles-batch",
    "uplift-refresh-public-feed-snapshot",
}

WORKER_UPLIFT_DELEGATE_OPERATIONS = {
    "uplift-save-accepted-articles-batch": "save-accepted-articles-batch",
    "uplift-save-article-summaries-batch": "save-article-summaries-batch",
    "uplift-save-article-reviews-batch": "save-article-reviews-batch",
    "uplift-save-feed-health-batch": "save-feed-health-batch",
    "uplift-save-ai-usage-run": "save-ai-usage-run",
    "uplift-save-worker-run": "save-worker-run",
    "uplift-publish-articles-batch": "publish-articles-batch",
    "uplift-refresh-public-feed-snapshot": "refresh-public-feed-snapshot",
}

WORKER_UPLIFT_SCOPE_OPERATIONS = {
    "worker-uplift-persistence": {
        "uplift-record-shadow-aggregate",
        "uplift-save-accepted-articles-batch",
        "uplift-save-article-summaries-batch",
        "uplift-save-article-reviews-batch",
        "uplift-save-feed-health-batch",
        "uplift-save-ai-usage-run",
        "uplift-save-worker-run",
    },
    "worker-uplift-publication": {
        "uplift-publish-articles-batch",
        "uplift-refresh-public-feed-snapshot",
        "uplift-save-worker-run",
    },
}

LEGACY_WORKER_API_SCOPE = "legacy-worker-api"

APP_READ_OPERATIONS = {
    "app-provider-smoke",
    "get-runtime-feature-flag",
    "load-readiness-schema-contract",
    "load-public-feed-snapshot",
    "load-home-feed-snapshot",
    "load-published-articles",
    "load-published-categories",
    "load-article-detail",
    "load-recent-article-sitemap-items",
    "load-published-article-sitemap-count",
    "load-article-sitemap-items-page",
    "search-published-articles",
    "load-admin-production-readiness",
    "load-admin-article-reviews",
    "load-admin-article-engagement",
    "load-admin-ai-usage",
    "load-admin-local-ai",
    "load-admin-translation-quality",
    "load-admin-guardrails",
    "load-admin-worker-shards",
    "load-admin-worker-uplift-health",
    "load-admin-rss-feed-health",
    "load-admin-feed-management",
    "load-admin-audit-log",
    "load-admin-runtime-feature-flags",
    "load-admin-quota-usage-events",
    "load-admin-article-engagement-source-category-summary",
    "load-admin-article-engagement-article-summary",
    "load-admin-feed-health-snapshots",
    "load-admin-feed-quality-rows",
    "load-admin-worker-runs",
    "load-admin-ai-usage-runs",
    "load-admin-article-review-rows",
}

APP_WRITE_OPERATIONS = {
    "record-quota-usage-event",
    "record-article-engagement-event",
    "set-runtime-feature-flag",
}

ALLOWED_PROVIDER_MODES = {"backend_postgres_shadow", "backend_postgres_primary"}
SUMMARY_TRANSLATION_LANGUAGE_CODES = ("fr", "ja", "de-CH", "de", "el")
SUMMARY_TRANSLATION_LANGUAGE_CODE_SET = set(SUMMARY_TRANSLATION_LANGUAGE_CODES)
ARTICLE_STATUSES = {"published", "translation_pending"}
WORKER_UPLIFT_STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
WORKER_UPLIFT_STAGE_STATUS_SET = {
    "healthy",
    "degraded",
    "failed",
    "stale",
    "unknown",
    "legacy_only",
    "rollback",
}
WORKER_UPLIFT_STALE_STATUS_SET = {"current", "stale", "unknown"}
WORKER_UPLIFT_ACTIVE_INGESTION_OWNER_SET = {
    "legacy_shards",
    "coexistence",
    "worker_uplift",
    "rollback",
    "unknown",
}
WORKER_UPLIFT_ADMIN_PROJECTION_VERSION = 1

RUNTIME_FEATURE_FLAG_DEFAULTS = {
    "reader_archive_search": True,
    "worker_public_feed_edge_snapshot_publish": True,
}

DEFAULT_LANGUAGE_CODE = "en"
SUPPORTED_EDGE_SNAPSHOT_LANGUAGE_CODES = {"en", "fr", "ja", "de-CH", "de", "el"}

PUBLIC_FEED_EDGE_SNAPSHOT_COLUMNS = (
    "id",
    "source",
    "title",
    "original_url",
    "image_url",
    "published_at",
    "published_on_site_at",
    "ai_summary",
    "category",
    "positivity_score",
)

ARTICLE_COLUMNS = (
    "id",
    "source",
    "title",
    "original_url",
    "image_url",
    "published_at",
    "published_on_site_at",
    "ai_summary",
    "category",
    "positivity_score",
)

SITEMAP_ARTICLE_COLUMNS = (
    "id",
    "published_on_site_at",
    "published_at",
)

ADMIN_ARTICLE_REVIEW_COLUMNS = (
    "id",
    "reviewed_at",
    "original_url",
    "source",
    "title",
    "decision",
    "category",
    "positivity_score",
    "summary",
    "reason",
    "ai_provider",
    "ai_model",
    "prompt_version",
    "model_version",
    "review_duration_ms",
)

ADMIN_PUBLISHED_ARTICLE_COLUMNS = (
    "id",
    "original_url",
    "source",
    "title",
    "image_url",
    "published_at",
    "published_on_site_at",
    "created_at",
    "ai_summary",
    "category",
    "positivity_score",
    "status",
)

AI_DECISION_VERSION_REPORT_COLUMNS = (
    "version_window",
    "version_rank",
    "prompt_version",
    "model_version",
    "ai_provider",
    "ai_model",
    "total_reviews",
    "accepted_reviews",
    "rejected_reviews",
    "acceptance_rate_pct",
    "rejection_rate_pct",
    "average_positivity_score",
    "previous_acceptance_rate_pct",
    "previous_rejection_rate_pct",
    "previous_average_positivity_score",
    "acceptance_rate_delta_pct",
    "rejection_rate_delta_pct",
    "average_score_delta",
    "first_reviewed_at",
    "latest_reviewed_at",
)

ADMIN_ARTICLE_ENGAGEMENT_SOURCE_CATEGORY_COLUMNS = (
    "source",
    "category",
    "outbound_click_count",
    "category_interest_count",
    "total_engagement_count",
    "first_event_date",
    "latest_event_date",
    "last_updated_at",
)

ADMIN_ARTICLE_ENGAGEMENT_ARTICLE_COLUMNS = (
    "article_id",
    "title",
    "original_url",
    "source",
    "category",
    "outbound_click_count",
    "first_event_date",
    "latest_event_date",
    "last_updated_at",
)

ADMIN_AI_USAGE_RUN_COLUMNS = (
    "id",
    "created_at",
    "run_started_at",
    "run_completed_at",
    "run_source",
    "request_id",
    "shard_index",
    "feeds_per_shard",
    "max_ai_reviews",
    "feed_count",
    "fetched_count",
    "candidate_count",
    "already_reviewed_count",
    "unreviewed_count",
    "eligible_for_ai_count",
    "ai_reviewed_count",
    "openai_model",
    "openai_call_count",
    "openai_prompt_tokens",
    "openai_completion_tokens",
    "openai_total_tokens",
    "estimated_openai_cost_usd",
    "openai_review_count",
    "openai_review_prompt_tokens",
    "openai_review_completion_tokens",
    "openai_review_total_tokens",
    "estimated_openai_review_cost_usd",
    "openai_translation_count",
    "openai_translation_prompt_tokens",
    "openai_translation_completion_tokens",
    "openai_translation_total_tokens",
    "estimated_openai_translation_cost_usd",
    "local_ai_model",
    "local_ai_call_count",
    "local_ai_prompt_tokens",
    "local_ai_completion_tokens",
    "local_ai_total_tokens",
    "local_ai_accepted_count",
    "local_ai_rejected_count",
    "local_ai_review_count",
    "local_ai_review_prompt_tokens",
    "local_ai_review_completion_tokens",
    "local_ai_review_total_tokens",
    "local_ai_translation_count",
    "local_ai_translation_prompt_tokens",
    "local_ai_translation_completion_tokens",
    "local_ai_translation_total_tokens",
    "estimated_local_ai_savings_usd",
    "openai_accepted_count",
    "openai_rejected_count",
    "published_accepted_count",
    "total_rejected_count",
    "no_thumbnail_rejected_count",
    "locally_rejected_count",
    "cost_protection_limit_reached",
    "spike_warning_triggered",
    "review_save_ok",
    "article_save_ok",
    "duration_ms",
)

ADMIN_LOCAL_AI_USAGE_RUN_COLUMNS = (
    "id",
    "run_started_at",
    "run_completed_at",
    "run_source",
    "shard_index",
    "ai_provider",
    "local_ai_model",
    "local_ai_call_count",
    "local_ai_prompt_tokens",
    "local_ai_completion_tokens",
    "local_ai_total_tokens",
    "local_ai_accepted_count",
    "local_ai_rejected_count",
    "local_ai_duration_ms",
    "openai_call_count",
    "ai_reviewed_count",
    "duration_ms",
)

ADMIN_LOCAL_AI_REVIEW_COLUMNS = (
    "id",
    "reviewed_at",
    "original_url",
    "source",
    "title",
    "decision",
    "category",
    "positivity_score",
    "summary",
    "reason",
    "ai_provider",
    "ai_model",
    "review_duration_ms",
)

ADMIN_TRANSLATION_QUALITY_ARTICLE_COLUMNS = (
    "id",
    "source",
    "title",
    "original_url",
    "ai_summary",
    "category",
    "published_on_site_at",
    "snapshot_rank",
)

ADMIN_TRANSLATION_QUALITY_SUMMARY_COLUMNS = (
    "original_url",
    "language_code",
    "title",
    "summary",
    "updated_at",
    "generated_by",
    "model",
)

ADMIN_GUARDRAILS_AI_USAGE_RUN_COLUMNS = (
    "run_started_at",
    "openai_call_count",
    "openai_prompt_tokens",
    "openai_completion_tokens",
    "openai_total_tokens",
    "estimated_openai_cost_usd",
    "cost_protection_limit_reached",
    "spike_warning_triggered",
    "local_ai_call_count",
    "local_ai_total_tokens",
)

ADMIN_GUARDRAILS_WORKER_RUN_COLUMNS = (
    "run_started_at",
    "success",
    "shard_index",
    "fetched_count",
    "ai_reviewed_count",
    "accepted_count",
    "rejected_count",
    "duration_ms",
    "cost_protection_limit_reached",
    "spike_warning_triggered",
)

ADMIN_GUARDRAILS_QUOTA_USAGE_EVENT_COLUMNS = (
    "event_type",
    "quantity",
    "created_at",
)

ADMIN_GUARDRAILS_COUNT_TABLES = {
    "articles": ("public.articles", "articleCount", "article_count"),
    "article_summaries": (
        "public.article_summaries",
        "summaryCount",
        "summary_count",
    ),
    "rss_feeds": ("public.rss_feeds", "feedCount", "feed_count"),
}

ADMIN_WORKER_SHARDS_RUN_COLUMNS = (
    "id",
    "created_at",
    "run_started_at",
    "run_completed_at",
    "run_source",
    "request_id",
    "shard_index",
    "feeds_per_shard",
    "max_ai_reviews",
    "success",
    "error_name",
    "error_message",
    "feed_count",
    "fetched_count",
    "candidate_count",
    "already_reviewed_count",
    "unreviewed_count",
    "eligible_for_ai_count",
    "ai_reviewed_count",
    "accepted_count",
    "rejected_count",
    "no_thumbnail_rejected_count",
    "locally_rejected_count",
    "image_hydration_lookup_count",
    "image_hydration_found_count",
    "review_save_ok",
    "article_save_ok",
    "ai_usage_save_ok",
    "cost_protection_limit_reached",
    "spike_warning_triggered",
    "duration_ms",
)

ADMIN_RSS_FEED_HEALTH_RSS_FEED_COLUMNS = (
    "source",
    "url",
    "is_positive_source",
    "is_active",
)

ADMIN_RSS_FEED_HEALTH_FEED_HEALTH_COLUMNS = (
    "id",
    "source",
    "feed_url",
    "last_checked_at",
    "last_success_at",
    "last_failure_at",
    "last_status",
    "last_error_message",
    "last_article_count",
    "last_image_count",
    "last_accepted_count",
    "last_rejected_count",
    "consecutive_failure_count",
    "total_fetch_count",
    "total_success_count",
    "total_failure_count",
    "total_article_count",
    "total_image_count",
    "total_accepted_count",
    "total_rejected_count",
    "created_at",
    "updated_at",
)

ADMIN_FEED_MANAGEMENT_FEED_QUALITY_COLUMNS = (
    "feed_id",
    "source",
    "feed_url",
    "is_active",
    "is_positive_source",
    "source_trust_tier",
    "publisher_allowlist_status",
    "recommended_trust_tier",
    "tier_recommendation_reason",
    "feed_health_id",
    "last_checked_at",
    "last_success_at",
    "last_failure_at",
    "last_status",
    "last_error_message",
    "last_article_count",
    "last_image_count",
    "last_accepted_count",
    "last_rejected_count",
    "consecutive_failure_count",
    "total_fetch_count",
    "total_success_count",
    "total_failure_count",
    "total_article_count",
    "total_image_count",
    "total_accepted_count",
    "total_rejected_count",
    "unique_reviewed_url_count",
    "unique_published_url_count",
    "success_rate_pct",
    "thumbnail_rate_pct",
    "accepted_rate_pct",
    "failure_rate_pct",
    "duplicate_rate_pct",
    "quality_score",
    "quality_grade",
    "quality_reason",
    "updated_at",
)

ADMIN_AUDIT_LOG_EVENT_COLUMNS = (
    "id",
    "created_at",
    "actor_email",
    "action",
    "target_type",
    "target_id",
    "target_label",
    "before_values",
    "after_values",
    "metadata",
)

ADMIN_RUNTIME_FEATURE_FLAG_COLUMNS = (
    "key",
    "enabled",
    "created_at",
    "updated_at",
)

ADMIN_TABLE_READS = {
    "load-admin-quota-usage-events": (
        "public.quota_usage_events",
        "id, created_at, event_type, event_source, provider, quantity, metadata",
        "created_at desc",
    ),
    "load-admin-article-engagement-source-category-summary": (
        "public.article_engagement_source_category_summary",
        "source, category, outbound_click_count, category_interest_count, total_engagement_count, first_event_date, latest_event_date, last_updated_at",
        "total_engagement_count desc, latest_event_date desc nulls last",
    ),
    "load-admin-article-engagement-article-summary": (
        "public.article_engagement_article_summary",
        "article_id, title, original_url, source, category, outbound_click_count, first_event_date, latest_event_date, last_updated_at",
        "outbound_click_count desc, latest_event_date desc nulls last",
    ),
    "load-admin-feed-health-snapshots": (
        "public.feed_health",
        "source, feed_url, last_checked_at, last_success_at, last_failure_at, last_status, last_error_message, consecutive_failure_count, total_fetch_count, total_success_count, total_failure_count, total_article_count, total_image_count, total_accepted_count, total_rejected_count, updated_at",
        "consecutive_failure_count desc, last_checked_at desc nulls last",
    ),
    "load-admin-feed-quality-rows": (
        "public.feed_quality_scores",
        "source, feed_url, quality_score, success_rate, thumbnail_rate, accepted_rate, failure_rate, duplicate_rate, total_fetch_count, total_success_count, total_failure_count, total_article_count, total_image_count, total_accepted_count, total_rejected_count, last_success_at, last_failure_at",
        "quality_score asc, total_accepted_count desc",
    ),
    "load-admin-worker-runs": (
        "public.worker_runs",
        "id, run_started_at, run_completed_at, run_source, request_id, shard_index, success, error_name, error_message, feed_count, fetched_count, candidate_count, ai_reviewed_count, accepted_count, rejected_count, duration_ms",
        "run_started_at desc",
    ),
    "load-admin-ai-usage-runs": (
        "public.ai_usage_runs",
        "id, run_started_at, run_completed_at, run_source, request_id, shard_index, ai_provider, local_ai_total_tokens, openai_total_tokens, estimated_openai_cost_usd, estimated_local_ai_savings_usd, duration_ms",
        "run_started_at desc",
    ),
    "load-admin-article-review-rows": (
        "public.article_ai_reviews",
        "id, original_url, source, title, decision, category, positivity_score, reason, ai_provider, ai_model, review_duration_ms, reviewed_at",
        "reviewed_at desc nulls last",
    ),
}

WRITE_TABLE_COLUMNS = {
    "article_summaries": (
        "original_url",
        "language_code",
        "source_language_code",
        "title",
        "summary",
        "generated_by",
        "model",
        "updated_at",
    ),
    "feed_health": (
        "source",
        "feed_url",
        "last_checked_at",
        "last_success_at",
        "last_failure_at",
        "last_status",
        "last_error_message",
        "last_article_count",
        "last_image_count",
        "last_accepted_count",
        "last_rejected_count",
        "consecutive_failure_count",
        "total_fetch_count",
        "total_success_count",
        "total_failure_count",
        "total_article_count",
        "total_image_count",
        "total_accepted_count",
        "total_rejected_count",
        "updated_at",
    ),
    "article_ai_reviews": (
        "original_url",
        "source",
        "title",
        "decision",
        "category",
        "positivity_score",
        "summary",
        "reason",
        "ai_provider",
        "ai_model",
        "review_duration_ms",
        "reviewed_at",
    ),
    "articles": (
        "source",
        "title",
        "original_url",
        "image_url",
        "published_at",
        "published_on_site_at",
        "original_excerpt",
        "ai_summary",
        "category",
        "positivity_score",
        "ai_provider",
        "ai_model",
        "status",
    ),
    "ai_usage_runs": (
        "run_started_at",
        "run_completed_at",
        "run_source",
        "request_id",
        "shard_index",
        "feeds_per_shard",
        "max_ai_reviews",
        "feed_count",
        "fetched_count",
        "candidate_count",
        "already_reviewed_count",
        "unreviewed_count",
        "eligible_for_ai_count",
        "ai_reviewed_count",
        "ai_provider",
        "local_ai_model",
        "local_ai_call_count",
        "local_ai_prompt_tokens",
        "local_ai_completion_tokens",
        "local_ai_total_tokens",
        "local_ai_accepted_count",
        "local_ai_rejected_count",
        "local_ai_duration_ms",
        "local_ai_review_count",
        "local_ai_review_prompt_tokens",
        "local_ai_review_completion_tokens",
        "local_ai_review_total_tokens",
        "local_ai_review_duration_ms",
        "local_ai_translation_count",
        "local_ai_translation_prompt_tokens",
        "local_ai_translation_completion_tokens",
        "local_ai_translation_total_tokens",
        "local_ai_translation_duration_ms",
        "openai_model",
        "openai_call_count",
        "openai_prompt_tokens",
        "openai_completion_tokens",
        "openai_total_tokens",
        "estimated_openai_cost_usd",
        "openai_review_count",
        "openai_review_prompt_tokens",
        "openai_review_completion_tokens",
        "openai_review_total_tokens",
        "estimated_openai_review_cost_usd",
        "openai_translation_count",
        "openai_translation_prompt_tokens",
        "openai_translation_completion_tokens",
        "openai_translation_total_tokens",
        "estimated_openai_translation_cost_usd",
        "estimated_local_ai_savings_usd",
        "openai_accepted_count",
        "openai_rejected_count",
        "published_accepted_count",
        "total_rejected_count",
        "no_thumbnail_rejected_count",
        "locally_rejected_count",
        "image_hydration_lookup_count",
        "image_hydration_found_count",
        "cost_protection_limit_reached",
        "spike_warning_triggered",
        "review_save_ok",
        "article_save_ok",
        "duration_ms",
    ),
    "worker_runs": (
        "run_started_at",
        "run_completed_at",
        "run_source",
        "request_id",
        "shard_index",
        "feeds_per_shard",
        "max_ai_reviews",
        "success",
        "error_name",
        "error_message",
        "feed_count",
        "fetched_count",
        "candidate_count",
        "already_reviewed_count",
        "unreviewed_count",
        "eligible_for_ai_count",
        "ai_reviewed_count",
        "accepted_count",
        "rejected_count",
        "no_thumbnail_rejected_count",
        "locally_rejected_count",
        "image_hydration_lookup_count",
        "image_hydration_found_count",
        "review_save_ok",
        "article_save_ok",
        "ai_usage_save_ok",
        "cost_protection_limit_reached",
        "spike_warning_triggered",
        "duration_ms",
    ),
}

WORKER_UPLIFT_SHADOW_AGGREGATE_COLUMNS = (
    "article_identity_hash",
    "canonical_url_hash",
    "original_url_hash",
    "aggregate_version",
    "source_feed_url",
    "title_ref",
    "image_url_ref",
    "category",
    "positivity_score",
    "approval_version",
    "translation_languages",
    "publication_status",
    "payload_ref",
    "payload_digest",
    "diagnostic_metadata",
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def bounded_int_value(value: Any, key: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ApiError(400, f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} must be an integer") from exc
    return max(minimum, min(maximum, parsed))


def bounded_int(body: dict[str, Any], key: str, *, default: int, minimum: int, maximum: int) -> int:
    return bounded_int_value(body.get(key, default), key, default=default, minimum=minimum, maximum=maximum)


def string_list(body: dict[str, Any], key: str, *, maximum: int = 1000) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list):
        raise ApiError(400, f"{key} must be an array")
    strings = [item for item in value[:maximum] if isinstance(item, str) and item]
    return strings


def optional_string(body: dict[str, Any], key: str, *, maximum: int = 500) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    value = value.strip()
    if not value:
        return None
    return value[:maximum]


def required_string(body: dict[str, Any], key: str, *, maximum: int = 500) -> str:
    value = optional_string(body, key, maximum=maximum)
    if value is None:
        raise ApiError(400, f"{key} must be a non-empty string")
    return value


def required_iso_datetime_string(body: dict[str, Any], key: str, *, maximum: int = 64) -> str:
    value = required_string(body, key, maximum=maximum)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, f"{key} must be an ISO timestamp") from exc
    return value


def required_int(body: dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    if key not in body:
        raise ApiError(400, f"{key} must be an integer")
    value = body.get(key)
    if isinstance(value, bool):
        raise ApiError(400, f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ApiError(400, f"{key} must be between {minimum} and {maximum}")
    return parsed


def optional_bool(body: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(400, f"{key} must be a boolean")
    return value


def summary_translation_language_codes(body: dict[str, Any]) -> list[str]:
    language_codes = string_list(body, "languageCodes", maximum=20)
    deduped = list(dict.fromkeys(language_codes))
    unsupported = [code for code in deduped if code not in SUMMARY_TRANSLATION_LANGUAGE_CODE_SET]
    if unsupported:
        raise ApiError(
            400,
            "languageCodes must contain supported summary translation languages: "
            + ", ".join(SUMMARY_TRANSLATION_LANGUAGE_CODES),
        )
    return deduped


def article_status(body: dict[str, Any]) -> str:
    status = optional_string(body, "status", maximum=64) or "published"
    if status not in ARTICLE_STATUSES:
        raise ApiError(400, "status must be published or translation_pending")
    return status


def normalize_edge_snapshot_language_code(value: str | None) -> str:
    if not value:
        return DEFAULT_LANGUAGE_CODE

    normalized = value.strip()
    if not normalized:
        return DEFAULT_LANGUAGE_CODE

    lowered = normalized.lower()
    if lowered in {"de-ch", "de_ch", "ch"}:
        return "de-CH"

    return lowered if lowered in SUPPORTED_EDGE_SNAPSHOT_LANGUAGE_CODES else DEFAULT_LANGUAGE_CODE


def requested_edge_snapshot_language_code(body: dict[str, Any]) -> str:
    return normalize_edge_snapshot_language_code(
        optional_string(body, "languageCode", maximum=16)
        or optional_string(body, "requestedLanguageCode", maximum=16)
        or optional_string(body, "lang", maximum=16)
    )


def has_usable_summary_translation(summary: dict[str, Any] | None, requested_language_code: str) -> bool:
    if requested_language_code == DEFAULT_LANGUAGE_CODE or not summary:
        return False

    title = summary.get("title")
    text = summary.get("summary")
    language_code = normalize_edge_snapshot_language_code(str(summary.get("language_code") or ""))
    return (
        language_code == requested_language_code
        and isinstance(title, str)
        and bool(title.strip())
        and isinstance(text, str)
        and bool(text.strip())
    )


def localize_public_feed_edge_snapshot_articles(
    articles: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    requested_language_code: str,
) -> list[dict[str, Any]]:
    summaries_by_url = {
        str(summary.get("original_url")): summary
        for summary in summaries
        if summary.get("original_url")
    }
    localized_articles: list[dict[str, Any]] = []

    for article in articles:
        original_url = str(article.get("original_url") or "")
        summary = summaries_by_url.get(original_url)
        has_translation = has_usable_summary_translation(summary, requested_language_code)
        localized_article = dict(article)

        if has_translation and summary is not None:
            localized_article["title"] = summary["title"]
            localized_article["ai_summary"] = summary["summary"]
            localized_article["language_code"] = requested_language_code
            localized_article["requested_language_code"] = requested_language_code
            localized_article["translation_available"] = True
        else:
            localized_article["language_code"] = DEFAULT_LANGUAGE_CODE
            localized_article["requested_language_code"] = requested_language_code
            localized_article["translation_available"] = requested_language_code == DEFAULT_LANGUAGE_CODE

        localized_articles.append(localized_article)

    return localized_articles


def bool_from_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class PostgresStore:
    def __init__(self) -> None:
        self.host = os.environ.get("NUTSNEWS_WORKER_DB_API_DB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("NUTSNEWS_WORKER_DB_API_DB_PORT", "5432"))
        self.database = os.environ.get("NUTSNEWS_WORKER_DB_API_DB_NAME", "nutsnews_primary_shadow")
        self.user = os.environ.get("NUTSNEWS_WORKER_DB_API_DB_USER", "nutsnews_readonly")
        self.password = os.environ.get("NUTSNEWS_WORKER_DB_API_DB_PASSWORD", "")
        self.writes_enabled = bool_from_env("NUTSNEWS_WORKER_DB_API_WRITES_ENABLED", False)
        self.max_limit = int(os.environ.get("NUTSNEWS_WORKER_DB_API_MAX_LIMIT", "10000"))
        self.worker_uplift_cutover_state = os.environ.get("NUTSNEWS_WORKER_UPLIFT_CUTOVER_STATE", "shadow")
        self.worker_uplift_production_writes_enabled = bool_from_env(
            "NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED",
            False,
        )
        self.worker_uplift_expected_candidate_sha256 = os.environ.get(
            "NUTSNEWS_WORKER_UPLIFT_EXPECTED_CANDIDATE_SHA256",
            "",
        )
        self.worker_uplift_expected_watermark_sha256 = os.environ.get(
            "NUTSNEWS_WORKER_UPLIFT_EXPECTED_WATERMARK_SHA256",
            "",
        )

    def connect(self):
        import psycopg2
        import psycopg2.extras

        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=5,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()]

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.fetch_all(query, params)
        return rows[0] if rows else None

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)


def assert_provider_mode(body: dict[str, Any]) -> str:
    mode = body.get("providerMode")
    if mode not in ALLOWED_PROVIDER_MODES:
        raise ApiError(400, "providerMode must be backend_postgres_shadow or backend_postgres_primary")
    return str(mode)


def assert_write_allowed(
    operation: str,
    body: dict[str, Any],
    store: PostgresStore,
    *,
    write_operations: set[str] = WRITE_OPERATIONS,
) -> None:
    provider_mode = assert_provider_mode(body)
    if operation not in write_operations:
        return
    if provider_mode != "backend_postgres_primary":
        raise ApiError(409, "write operations are disabled outside backend_postgres_primary")
    if not store.writes_enabled:
        raise ApiError(403, "backend PostgreSQL writes are disabled by deployment guardrail")


def configured_database_api_tokens() -> dict[str, str]:
    tokens = {
        LEGACY_WORKER_API_SCOPE: os.environ.get("NUTSNEWS_BACKEND_API_TOKEN", ""),
        "worker-uplift-persistence": os.environ.get("NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN", ""),
        "worker-uplift-publication": os.environ.get("NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN", ""),
    }
    return {scope: token for scope, token in tokens.items() if token}


def authenticate_database_api_token(authorization: str) -> str:
    tokens = configured_database_api_tokens()
    if not tokens:
        raise ApiError(503, "database compatibility API token is not configured")
    if len(set(tokens.values())) != len(tokens):
        raise ApiError(503, "database compatibility API scoped tokens must be distinct")
    if not authorization.startswith("Bearer "):
        raise ApiError(401, "invalid database compatibility API token")
    supplied = authorization.removeprefix("Bearer ")
    for scope, expected_token in tokens.items():
        if hmac.compare_digest(supplied, expected_token):
            return scope
    raise ApiError(401, "invalid database compatibility API token")


def canonical_json(value: Any) -> str:
    return json.dumps(value, default=json_default, sort_keys=True, separators=(",", ":"))


def internal_error_payload(error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": "internal database compatibility API error",
        "errorClass": type(error).__name__,
        "safeMetadataOnly": True,
    }
    pgcode = getattr(error, "pgcode", None)
    if isinstance(pgcode, str) and pgcode:
        payload["pgcode"] = pgcode
    diag = getattr(error, "diag", None)
    sqlstate = getattr(diag, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        payload["sqlstate"] = sqlstate
    return payload


WORKER_UPLIFT_DELIVERY_METADATA_FIELDS = {
    "correlationId",
    "idempotencyKey",
    "messageId",
    "pipelineRunId",
    "sourceMessageId",
    "stageExecutionId",
}


def uplift_payload_digest(operation: str, body: dict[str, Any]) -> str:
    business_body = {
        key: value
        for key, value in body.items()
        if key not in WORKER_UPLIFT_DELIVERY_METADATA_FIELDS
    }
    return hashlib.sha256(canonical_json({"operation": operation, "body": business_body}).encode("utf-8")).hexdigest()


def assert_worker_uplift_scope(operation: str, auth_scope: str, actor_service: str) -> None:
    allowed_operations = WORKER_UPLIFT_SCOPE_OPERATIONS.get(auth_scope)
    if not allowed_operations or operation not in allowed_operations:
        raise ApiError(403, "worker-uplift scoped token is not allowed to call this operation")
    if actor_service != auth_scope:
        raise ApiError(403, "actorService must match the authenticated worker-uplift scope")


def worker_uplift_database_control(store: PostgresStore) -> dict[str, Any] | None:
    """Read the sole production control row, failing closed on any DB error."""
    try:
        row = store.fetch_one(
            """
            select state, active_ingestion_owner, legacy_dispatch_enabled,
                   uplift_scheduler_enabled, uplift_production_writes_enabled,
                   publication_write_mode, candidate_sha256, watermark_sha256
            from worker_uplift_final.cutover_control
            where control_id = %s
            """,
            ("production",),
        )
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def assert_worker_uplift_production_allowed(store: PostgresStore) -> None:
    cutover_state = getattr(store, "worker_uplift_cutover_state", "shadow")
    production_writes_enabled = bool(getattr(store, "worker_uplift_production_writes_enabled", False))
    if cutover_state != "cutover-approved" or not production_writes_enabled:
        raise ApiError(403, "worker-uplift production writes require protected cutover approval")
    if not getattr(store, "writes_enabled", False):
        raise ApiError(403, "backend PostgreSQL writes are disabled by deployment guardrail")
    control = worker_uplift_database_control(store)
    expected_candidate = str(getattr(store, "worker_uplift_expected_candidate_sha256", "") or "")
    expected_watermark = str(getattr(store, "worker_uplift_expected_watermark_sha256", "") or "")
    if (
        control is None
        or control.get("state") != "cutover_active"
        or control.get("active_ingestion_owner") != "worker_uplift"
        or control.get("legacy_dispatch_enabled") is not False
        or control.get("uplift_scheduler_enabled") is not True
        or control.get("uplift_production_writes_enabled") is not True
        or control.get("publication_write_mode") != "production"
        or not re.fullmatch(r"[0-9a-f]{64}", expected_candidate)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_watermark)
        or control.get("candidate_sha256") != expected_candidate
        or control.get("watermark_sha256") != expected_watermark
    ):
        raise ApiError(403, "worker-uplift database single-writer control is not active for this candidate")


def worker_uplift_metadata(operation: str, body: dict[str, Any], auth_scope: str) -> dict[str, Any]:
    metadata = {
        "operation": operation,
        "idempotency_key": required_string(body, "idempotencyKey", maximum=160),
        "message_id": required_string(body, "messageId", maximum=160),
        "correlation_id": required_string(body, "correlationId", maximum=160),
        "pipeline_run_id": required_string(body, "pipelineRunId", maximum=160),
        "stage_execution_id": required_string(body, "stageExecutionId", maximum=160),
        "source_message_id": required_string(body, "sourceMessageId", maximum=160),
        "actor_service": required_string(body, "actorService", maximum=160),
        "schema_version": required_int(body, "schemaVersion", minimum=1, maximum=1000),
        "operation_version": required_int(body, "operationVersion", minimum=1, maximum=1_000_000_000),
        "expected_article_version": required_int(body, "expectedArticleVersion", minimum=0, maximum=1_000_000_000),
    }
    assert_worker_uplift_scope(operation, auth_scope, metadata["actor_service"])
    return metadata


def assert_worker_uplift_publication_payload(operation: str, body: dict[str, Any]) -> None:
    if operation != "uplift-publish-articles-batch":
        return
    original_urls = string_list(body, "originalUrls", maximum=2)
    if len(original_urls) != 1 or len(set(original_urls)) != 1:
        raise ApiError(400, "worker-uplift publication requires exactly one originalUrls entry")
    parsed_url = urlsplit(original_urls[0])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ApiError(400, "worker-uplift publication originalUrls entry must be an HTTP(S) URL")
    if article_status(body) != "published":
        raise ApiError(400, "worker-uplift publication status must be published")
    language_codes = summary_translation_language_codes(body)
    if language_codes != list(SUMMARY_TRANSLATION_LANGUAGE_CODES):
        raise ApiError(400, "worker-uplift publication languageCodes must match the protected policy")


def load_worker_uplift_receipt(store: PostgresStore, idempotency_key: str) -> dict[str, Any] | None:
    row = store.fetch_one(
        """
        select operation, payload_digest, response_json
        from worker_uplift_final.api_command_receipts
        where idempotency_key = %s
        limit 1
        """,
        (idempotency_key,),
    )
    if not row or "payload_digest" not in row or "operation" not in row:
        return None
    return row


def receipt_response(row: dict[str, Any]) -> dict[str, Any]:
    response = row.get("response_json")
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            return {"ok": True, "recorded": True}
        return parsed if isinstance(parsed, dict) else {"ok": True, "recorded": True}
    return {"ok": True, "recorded": True}


def record_worker_uplift_receipt(
    store: PostgresStore,
    metadata: dict[str, Any],
    *,
    auth_scope: str,
    provider_mode: str,
    payload_digest: str,
    response: dict[str, Any],
    status: str,
    shadow_only: bool,
) -> None:
    store.execute(
        """
        insert into worker_uplift_final.api_command_receipts (
          idempotency_key, operation, payload_digest, provider_mode, actor_service, auth_scope,
          schema_version, operation_version, pipeline_run_id, stage_execution_id, source_message_id,
          message_id, correlation_id, expected_article_version, response_json, status, shadow_only,
          diagnostic_metadata
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
        on conflict (idempotency_key) do nothing
        """,
        (
            metadata["idempotency_key"],
            metadata["operation"],
            payload_digest,
            provider_mode,
            metadata["actor_service"],
            auth_scope,
            metadata["schema_version"],
            metadata["operation_version"],
            metadata["pipeline_run_id"],
            metadata["stage_execution_id"],
            metadata["source_message_id"],
            metadata["message_id"],
            metadata["correlation_id"],
            metadata["expected_article_version"],
            canonical_json(response),
            status,
            shadow_only,
            canonical_json({"safe_metadata_only": True}),
        ),
    )


def insert_rows(store: PostgresStore, table: str, rows: list[dict[str, Any]], *, conflict: tuple[str, ...] | None, update: bool) -> None:
    if not rows:
        return
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ApiError(400, f"{table} payload must be an array of objects")
    columns = WRITE_TABLE_COLUMNS[table]
    values = []
    for row in rows:
        values.extend(row.get(column) for column in columns)
    row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
    placeholders = ", ".join([row_placeholder] * len(rows))
    column_sql = ", ".join(columns)
    query = f"insert into public.{table} ({column_sql}) values {placeholders}"
    if conflict:
        conflict_sql = ", ".join(conflict)
        if update:
            update_columns = [column for column in columns if column not in conflict]
            set_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
            query += f" on conflict ({conflict_sql}) do update set {set_sql}"
        else:
            query += f" on conflict ({conflict_sql}) do nothing"
    store.execute(query, tuple(values))


def assert_accepted_articles_are_not_published(articles: Any) -> None:
    if not articles:
        return
    if not isinstance(articles, list) or not all(isinstance(row, dict) for row in articles):
        return
    published_urls = [
        row.get("original_url")
        for row in articles
        if row.get("status") == "published" and row.get("original_url")
    ]
    if published_urls:
        raise ApiError(
            409,
            "save-accepted-articles-batch cannot insert published articles; "
            "save accepted rows as translation_pending and publish after article_summaries rows exist",
        )


def load_missing_summary_translation_rows(
    store: PostgresStore,
    original_urls: list[str],
    language_codes: list[str],
) -> list[dict[str, Any]]:
    if not original_urls or not language_codes:
        return []
    return store.fetch_all(
        """
        select requested_urls.original_url, requested_languages.language_code
        from unnest(%s::text[]) as requested_urls(original_url)
        cross join unnest(%s::text[]) as requested_languages(language_code)
        left join public.article_summaries summaries
          on summaries.original_url = requested_urls.original_url
         and summaries.language_code = requested_languages.language_code
        where summaries.original_url is null
        order by requested_urls.original_url asc, requested_languages.language_code asc
        limit %s
        """,
        (original_urls, language_codes, len(original_urls) * len(language_codes)),
    )


def publish_articles_with_translation_guard(
    store: PostgresStore,
    original_urls: list[str],
    language_codes: list[str],
) -> dict[str, Any]:
    if not original_urls:
        return {
            "ok": True,
            "requestedCount": 0,
            "publishedCount": 0,
            "blockedCount": 0,
            "missingTranslations": [],
        }
    if not language_codes:
        raise ApiError(400, "languageCodes is required when publishing articles")
    missing_translations = load_missing_summary_translation_rows(store, original_urls, language_codes)
    if missing_translations:
        blocked_urls = sorted(
            {
                str(row["original_url"])
                for row in missing_translations
                if isinstance(row.get("original_url"), str) and row.get("original_url")
            }
        )
        return {
            "ok": False,
            "requestedCount": len(original_urls),
            "publishedCount": 0,
            "blockedCount": len(blocked_urls),
            "missingTranslations": missing_translations,
        }
    published_rows = store.fetch_all(
        """
        update public.articles
        set status = 'published'
        where original_url = any(%s)
        returning original_url
        """,
        (original_urls,),
    )
    published_count = len(published_rows)
    missing_count = max(0, len(original_urls) - published_count)
    return {
        "ok": missing_count == 0,
        "requestedCount": len(original_urls),
        "publishedCount": published_count,
        "blockedCount": missing_count,
        "missingTranslations": [],
    }


def aggregate_value(aggregate: dict[str, Any], snake_key: str, camel_key: str | None = None) -> Any:
    if snake_key in aggregate:
        return aggregate[snake_key]
    if camel_key and camel_key in aggregate:
        return aggregate[camel_key]
    return None


def shadow_aggregate_payload(body: dict[str, Any]) -> dict[str, Any]:
    aggregate = body.get("shadowAggregate")
    if not isinstance(aggregate, dict):
        raise ApiError(400, "shadowAggregate must be an object")

    translation_languages = aggregate_value(aggregate, "translation_languages", "translationLanguages") or []
    if not isinstance(translation_languages, list) or not all(isinstance(item, str) for item in translation_languages):
        raise ApiError(400, "shadowAggregate.translationLanguages must be an array of strings")
    diagnostic_metadata = aggregate_value(aggregate, "diagnostic_metadata", "diagnosticMetadata") or {}
    if not isinstance(diagnostic_metadata, dict):
        raise ApiError(400, "shadowAggregate.diagnosticMetadata must be an object")

    row = {
        "article_identity_hash": required_string(aggregate, "articleIdentityHash", maximum=256)
        if "articleIdentityHash" in aggregate
        else required_string(aggregate, "article_identity_hash", maximum=256),
        "canonical_url_hash": required_string(aggregate, "canonicalUrlHash", maximum=256)
        if "canonicalUrlHash" in aggregate
        else required_string(aggregate, "canonical_url_hash", maximum=256),
        "original_url_hash": required_string(aggregate, "originalUrlHash", maximum=256)
        if "originalUrlHash" in aggregate
        else required_string(aggregate, "original_url_hash", maximum=256),
        "aggregate_version": required_int(aggregate, "aggregateVersion" if "aggregateVersion" in aggregate else "aggregate_version", minimum=1, maximum=1_000_000_000),
        "source_feed_url": aggregate_value(aggregate, "source_feed_url", "sourceFeedUrl"),
        "title_ref": aggregate_value(aggregate, "title_ref", "titleRef"),
        "image_url_ref": aggregate_value(aggregate, "image_url_ref", "imageUrlRef"),
        "category": aggregate.get("category"),
        "positivity_score": aggregate_value(aggregate, "positivity_score", "positivityScore"),
        "approval_version": aggregate_value(aggregate, "approval_version", "approvalVersion"),
        "translation_languages": list(dict.fromkeys(translation_languages)),
        "publication_status": aggregate_value(aggregate, "publication_status", "publicationStatus") or "shadow_only",
        "payload_ref": required_string(aggregate, "payloadRef", maximum=500)
        if "payloadRef" in aggregate
        else required_string(aggregate, "payload_ref", maximum=500),
        "payload_digest": required_string(aggregate, "payloadDigest", maximum=256)
        if "payloadDigest" in aggregate
        else required_string(aggregate, "payload_digest", maximum=256),
        "diagnostic_metadata": diagnostic_metadata,
    }
    if row["publication_status"] not in {"shadow_only", "ready", "blocked", "published"}:
        raise ApiError(400, "shadowAggregate.publicationStatus is unsupported")
    return row


def assert_shadow_aggregate_version_is_current(store: PostgresStore, aggregate: dict[str, Any]) -> None:
    row = store.fetch_one(
        """
        select max(aggregate_version)::integer as aggregate_version
        from worker_uplift_final.article_shadow_aggregates
        where article_identity_hash = %s
        """,
        (aggregate["article_identity_hash"],),
    )
    if row and row.get("aggregate_version") is not None and int(row["aggregate_version"]) >= int(aggregate["aggregate_version"]):
        raise ApiError(409, "stale article version for worker-uplift shadow aggregate")


def upsert_shadow_aggregate(store: PostgresStore, aggregate: dict[str, Any]) -> None:
    columns = WORKER_UPLIFT_SHADOW_AGGREGATE_COLUMNS
    values = [
        canonical_json(aggregate[column]) if column == "diagnostic_metadata" else aggregate.get(column)
        for column in columns
    ]
    placeholders = ", ".join("%s::jsonb" if column == "diagnostic_metadata" else "%s" for column in columns)
    update_columns = [column for column in columns if column not in {"article_identity_hash", "aggregate_version"}]
    set_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    store.execute(
        f"""
        insert into worker_uplift_final.article_shadow_aggregates ({", ".join(columns)})
        values ({placeholders})
        on conflict (article_identity_hash, aggregate_version)
        do update set {set_sql}, updated_at = now()
        """,
        tuple(values),
    )


def handle_worker_uplift_operation(
    operation: str,
    body: dict[str, Any],
    store: PostgresStore,
    auth_scope: str,
) -> dict[str, Any]:
    provider_mode = assert_provider_mode(body)
    metadata = worker_uplift_metadata(operation, body, auth_scope)
    assert_worker_uplift_publication_payload(operation, body)
    payload_digest = uplift_payload_digest(operation, body)
    existing_receipt = load_worker_uplift_receipt(store, metadata["idempotency_key"])
    if existing_receipt is not None:
        if existing_receipt.get("operation") != operation or existing_receipt.get("payload_digest") != payload_digest:
            raise ApiError(409, "idempotencyKey was already used with a different worker-uplift payload")
        response = receipt_response(existing_receipt)
        response["duplicate"] = True
        return response

    if provider_mode == "backend_postgres_shadow":
        if operation == "uplift-record-shadow-aggregate":
            aggregate = shadow_aggregate_payload(body)
            assert_shadow_aggregate_version_is_current(store, aggregate)
            upsert_shadow_aggregate(store, aggregate)
        response = {
            "ok": True,
            "operation": operation,
            "mode": "shadow",
            "idempotencyKey": metadata["idempotency_key"],
            "recorded": True,
            "productionSideEffect": False,
        }
        record_worker_uplift_receipt(
            store,
            metadata,
            auth_scope=auth_scope,
            provider_mode=provider_mode,
            payload_digest=payload_digest,
            response=response,
            status="recorded_success",
            shadow_only=True,
        )
        return response

    assert_worker_uplift_production_allowed(store)
    if operation == "uplift-record-shadow-aggregate":
        aggregate = shadow_aggregate_payload(body)
        assert_shadow_aggregate_version_is_current(store, aggregate)
        upsert_shadow_aggregate(store, aggregate)
        response = {"ok": True, "operation": operation, "mode": "primary", "recorded": True}
    else:
        delegate_operation = WORKER_UPLIFT_DELEGATE_OPERATIONS[operation]
        delegate_result = handle_operation(delegate_operation, body, store, auth_scope=LEGACY_WORKER_API_SCOPE)
        response = delegate_result if isinstance(delegate_result, dict) else {"ok": True, "result": delegate_result}
        if operation == "uplift-publish-articles-batch" and (
            response.get("ok") is not True
            or response.get("requestedCount") != 1
            or response.get("publishedCount") != 1
            or response.get("blockedCount") != 0
            or response.get("missingTranslations") != []
        ):
            raise ApiError(409, "worker-uplift publication did not confirm one published article")
    record_worker_uplift_receipt(
        store,
        metadata,
        auth_scope=auth_scope,
        provider_mode=provider_mode,
        payload_digest=payload_digest,
        response=response,
        status="applied_success",
        shadow_only=False,
    )
    return response


def handle_operation(
    operation: str,
    body: dict[str, Any],
    store: PostgresStore,
    auth_scope: str = LEGACY_WORKER_API_SCOPE,
) -> Any:
    if operation not in READ_OPERATIONS and operation not in WRITE_OPERATIONS:
        raise ApiError(404, f"unknown worker database operation: {operation}")

    if operation in WORKER_UPLIFT_WRITE_OPERATIONS:
        return handle_worker_uplift_operation(operation, body, store, auth_scope)
    if auth_scope != LEGACY_WORKER_API_SCOPE:
        raise ApiError(403, "worker-uplift scoped token cannot call legacy worker operation")

    assert_write_allowed(operation, body, store)

    if operation == "load-feeds-for-shard":
        limit = bounded_int(body, "feedsPerShard", default=25, minimum=1, maximum=500)
        offset = bounded_int(body, "offset", default=0, minimum=0, maximum=1_000_000)
        return store.fetch_all(
            """
            select source, url, is_positive_source
            from public.rss_feeds
            where is_active is true
            order by id asc
            limit %s offset %s
            """,
            (limit, offset),
        )

    if operation == "load-reviewed-url-rows":
        limit = bounded_int(body, "lookbackLimit", default=5000, minimum=1, maximum=store.max_limit)
        candidate_urls = string_list(body, "candidateUrls", maximum=store.max_limit)
        if candidate_urls:
            return store.fetch_all(
                """
                select original_url, decision, reason, reviewed_at
                from public.article_ai_reviews
                where original_url = any(%s)
                order by reviewed_at desc nulls last
                limit %s
                """,
                (candidate_urls, limit),
            )
        return store.fetch_all(
            """
            select original_url, decision, reason, reviewed_at
            from public.article_ai_reviews
            order by reviewed_at desc nulls last
            limit %s
            """,
            (limit,),
        )

    if operation == "load-published-article-url-rows":
        limit = bounded_int(body, "lookbackLimit", default=5000, minimum=1, maximum=store.max_limit)
        candidate_urls = string_list(body, "candidateUrls", maximum=store.max_limit)
        if candidate_urls:
            return store.fetch_all(
                """
                select original_url
                from public.articles
                where original_url = any(%s)
                order by published_on_site_at desc nulls last
                limit %s
                """,
                (candidate_urls, limit),
            )
        return store.fetch_all(
            """
            select original_url
            from public.articles
            order by published_on_site_at desc nulls last
            limit %s
            """,
            (limit,),
        )

    if operation == "load-article-count-for-backpressure":
        row = store.fetch_one("select count(*)::bigint as article_count from public.articles")
        return {"articleCount": int(row["article_count"]) if row else None, "error": None}

    if operation == "load-public-feed-snapshot-rows":
        limit = bounded_int(body, "limit", default=50, minimum=1, maximum=store.max_limit)
        requested_language_code = requested_edge_snapshot_language_code(body)
        columns = ", ".join(f"p.{column}" for column in PUBLIC_FEED_EDGE_SNAPSHOT_COLUMNS)
        articles = store.fetch_all(
            f"""
            select {columns}
            from public.public_feed_snapshot p
            order by p.snapshot_rank asc
            limit %s
            """,
            (limit,),
        )
        summaries: list[dict[str, Any]] = []
        original_urls = [
            str(article.get("original_url"))
            for article in articles
            if article.get("original_url")
        ]
        if requested_language_code != DEFAULT_LANGUAGE_CODE and original_urls:
            summaries = store.fetch_all(
                """
                select original_url, language_code, title, summary
                from public.article_summaries
                where original_url = any(%s)
                  and language_code = %s
                limit %s
                """,
                (original_urls, requested_language_code, min(store.max_limit, len(original_urls))),
            )
        return localize_public_feed_edge_snapshot_articles(articles, summaries, requested_language_code)

    if operation == "load-existing-summary-language-rows":
        original_urls = string_list(body, "originalUrls", maximum=store.max_limit)
        language_codes = string_list(body, "languageCodes", maximum=20)
        limit = bounded_int(body, "limit", default=500, minimum=1, maximum=store.max_limit)
        if not original_urls or not language_codes:
            return []
        return store.fetch_all(
            """
            select original_url, language_code
            from public.article_summaries
            where original_url = any(%s)
              and language_code = any(%s)
            limit %s
            """,
            (original_urls, language_codes, limit),
        )

    if operation == "load-summary-translation-recovery-articles":
        limit = bounded_int(body, "lookbackLimit", default=500, minimum=1, maximum=store.max_limit)
        return store.fetch_all(
            """
            select source, title, original_url, ai_summary, category, published_on_site_at, status
            from public.articles
            where status in ('published', 'translation_pending')
              and image_url is not null
              and ai_summary is not null
            order by published_on_site_at desc nulls last, created_at desc
            limit %s
            """,
            (limit,),
        )

    if operation == "load-feed-health-snapshots":
        return store.fetch_all(
            """
            select feed_url, consecutive_failure_count, total_fetch_count, total_success_count,
                   total_failure_count, total_article_count, total_image_count, total_accepted_count,
                   total_rejected_count, last_success_at, last_failure_at
            from public.feed_health
            limit 10000
            """
        )

    if operation == "get-runtime-feature-flag":
        key = body.get("key")
        if not isinstance(key, str):
            raise ApiError(400, "key must be a string")
        default = RUNTIME_FEATURE_FLAG_DEFAULTS.get(key, False)
        row = store.fetch_one("select enabled from public.runtime_feature_flags where key = %s limit 1", (key,))
        return {"key": key, "enabled": bool(row["enabled"]) if row and isinstance(row.get("enabled"), bool) else default}

    if operation == "save-article-summaries-batch":
        insert_rows(store, "article_summaries", body.get("summaries", []), conflict=("original_url", "language_code"), update=True)
        return {"ok": True}

    if operation == "save-feed-health-batch":
        insert_rows(store, "feed_health", body.get("feedHealthRows", []), conflict=("feed_url",), update=True)
        return {"ok": True}

    if operation == "save-article-reviews-batch":
        insert_rows(store, "article_ai_reviews", body.get("reviews", []), conflict=("original_url",), update=True)
        return {"ok": True}

    if operation == "save-accepted-articles-batch":
        articles = body.get("articles", [])
        assert_accepted_articles_are_not_published(articles)
        insert_rows(store, "articles", articles, conflict=("original_url",), update=False)
        return {"ok": True}

    if operation == "publish-articles-batch":
        original_urls = string_list(body, "originalUrls", maximum=store.max_limit)
        status = article_status(body)
        if status == "published":
            return publish_articles_with_translation_guard(store, original_urls, summary_translation_language_codes(body))
        if original_urls:
            store.execute("update public.articles set status = %s where original_url = any(%s)", (status, original_urls))
        return {"ok": True, "requestedCount": len(original_urls)}

    if operation == "refresh-public-feed-snapshot":
        row = store.fetch_one("select public.refresh_public_feed_snapshot() as refreshed_at")
        return {"refreshedAt": row.get("refreshed_at") if row else None}

    if operation == "save-ai-usage-run":
        run = body.get("run")
        if not isinstance(run, dict):
            raise ApiError(400, "run must be an object")
        insert_rows(store, "ai_usage_runs", [run], conflict=None, update=False)
        return {"ok": True}

    if operation == "save-worker-run":
        run = body.get("run")
        if not isinstance(run, dict):
            raise ApiError(400, "run must be an object")
        insert_rows(store, "worker_runs", [run], conflict=None, update=False)
        return {"ok": True}

    raise ApiError(404, f"unknown worker database operation: {operation}")


def app_article_columns() -> str:
    return ", ".join(ARTICLE_COLUMNS)


def sitemap_article_columns() -> str:
    return ", ".join(SITEMAP_ARTICLE_COLUMNS)


def admin_article_review_columns() -> str:
    return ", ".join(ADMIN_ARTICLE_REVIEW_COLUMNS)


def admin_published_article_columns() -> str:
    return ", ".join(ADMIN_PUBLISHED_ARTICLE_COLUMNS)


def ai_decision_version_report_columns() -> str:
    return ", ".join(AI_DECISION_VERSION_REPORT_COLUMNS)


def admin_article_engagement_source_category_columns() -> str:
    return ", ".join(ADMIN_ARTICLE_ENGAGEMENT_SOURCE_CATEGORY_COLUMNS)


def admin_article_engagement_article_columns() -> str:
    return ", ".join(ADMIN_ARTICLE_ENGAGEMENT_ARTICLE_COLUMNS)


def admin_ai_usage_run_columns() -> str:
    return ", ".join(ADMIN_AI_USAGE_RUN_COLUMNS)


def admin_local_ai_usage_run_columns() -> str:
    return ", ".join(ADMIN_LOCAL_AI_USAGE_RUN_COLUMNS)


def admin_local_ai_review_columns() -> str:
    return ", ".join(ADMIN_LOCAL_AI_REVIEW_COLUMNS)


def admin_translation_quality_article_columns() -> str:
    return ", ".join(ADMIN_TRANSLATION_QUALITY_ARTICLE_COLUMNS)


def admin_translation_quality_summary_columns() -> str:
    return ", ".join(ADMIN_TRANSLATION_QUALITY_SUMMARY_COLUMNS)


def admin_guardrails_ai_usage_run_columns() -> str:
    return ", ".join(ADMIN_GUARDRAILS_AI_USAGE_RUN_COLUMNS)


def admin_guardrails_worker_run_columns() -> str:
    return ", ".join(ADMIN_GUARDRAILS_WORKER_RUN_COLUMNS)


def admin_guardrails_quota_usage_event_columns() -> str:
    return ", ".join(ADMIN_GUARDRAILS_QUOTA_USAGE_EVENT_COLUMNS)


def admin_worker_shards_run_columns() -> str:
    return ", ".join(ADMIN_WORKER_SHARDS_RUN_COLUMNS)


def admin_rss_feed_health_rss_feed_columns() -> str:
    return ", ".join(ADMIN_RSS_FEED_HEALTH_RSS_FEED_COLUMNS)


def admin_rss_feed_health_feed_health_columns() -> str:
    return ", ".join(ADMIN_RSS_FEED_HEALTH_FEED_HEALTH_COLUMNS)


def admin_feed_management_feed_quality_columns() -> str:
    return ", ".join(ADMIN_FEED_MANAGEMENT_FEED_QUALITY_COLUMNS)


def admin_audit_log_event_columns() -> str:
    return ", ".join(ADMIN_AUDIT_LOG_EVENT_COLUMNS)


def admin_runtime_feature_flag_columns() -> str:
    return ", ".join(ADMIN_RUNTIME_FEATURE_FLAG_COLUMNS)


def category_clause(body: dict[str, Any], params: list[Any]) -> str:
    category = optional_string(body, "category", maximum=96)
    if category is None or category.lower() == "all":
        return ""
    params.append(f"%{category}%")
    return " and category ilike %s"


def published_article_filters(body: dict[str, Any], params: list[Any]) -> str:
    clause = " where status = 'published' and image_url is not null and image_url <> ''"
    clause += category_clause(body, params)
    cursor_published_on_site_at = optional_string(body, "cursorPublishedOnSiteAt", maximum=64)
    cursor_id = optional_string(body, "cursorId", maximum=128)
    if cursor_published_on_site_at and cursor_id:
        clause += " and (published_on_site_at < %s or (published_on_site_at = %s and id < %s))"
        params.extend((cursor_published_on_site_at, cursor_published_on_site_at, cursor_id))
    return clause


def production_readiness_target_language_codes(body: dict[str, Any]) -> list[str]:
    values = string_list(body, "targetLanguageCodes", maximum=20)
    if not values:
        values = list(SUMMARY_TRANSLATION_LANGUAGE_CODES)
    default_language_code = normalize_edge_snapshot_language_code(
        optional_string(body, "defaultLanguageCode", maximum=16)
    )
    language_codes: list[str] = []
    for value in values:
        normalized = "de-CH" if value.strip().lower() in {"de-ch", "de_ch", "ch"} else value.strip().lower()
        if normalized == default_language_code or normalized == DEFAULT_LANGUAGE_CODE:
            continue
        if normalized not in SUMMARY_TRANSLATION_LANGUAGE_CODE_SET:
            raise ApiError(
                400,
                "targetLanguageCodes must contain supported summary translation languages: "
                + ", ".join(SUMMARY_TRANSLATION_LANGUAGE_CODES),
            )
        if normalized not in language_codes:
            language_codes.append(normalized)
    return language_codes


def production_readiness_growth_window_hours(body: dict[str, Any], index: int, default: int) -> int:
    values = body.get("articleGrowthWindowsHours", [])
    if values is None:
        return default
    if not isinstance(values, list):
        raise ApiError(400, "articleGrowthWindowsHours must be an array")
    if index >= len(values):
        return default
    value = values[index]
    if isinstance(value, bool):
        raise ApiError(400, "articleGrowthWindowsHours values must be integers")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "articleGrowthWindowsHours values must be integers") from exc
    return max(1, min(24 * 365, parsed))


def count_value(row: dict[str, Any] | None, key: str) -> int:
    if not row:
        return 0
    value = row.get(key)
    if value is None:
        return 0
    return int(value)


def count_published_articles_since(store: PostgresStore, hours: int) -> int:
    row = store.fetch_one(
        """
        select count(*)::bigint as article_count
        from public.articles
        where status = 'published'
          and created_at >= now() - (%s * interval '1 hour')
        """,
        (hours,),
    )
    return count_value(row, "article_count")


def clean_admin_option(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def sorted_admin_options(values: set[str]) -> list[str]:
    return sorted(value for value in values if value)


def load_admin_article_review_options(store: PostgresStore, limit: int) -> dict[str, list[str]]:
    rows = store.fetch_all(
        """
        select source, category
        from public.article_ai_reviews
        order by reviewed_at desc nulls last, id desc
        limit %s
        """,
        (limit,),
    )
    sources: set[str] = set()
    categories: set[str] = set()
    for row in rows:
        source = clean_admin_option(row.get("source"))
        category = clean_admin_option(row.get("category"))
        if source:
            sources.add(source)
        if category:
            for value in re.split(r"[|,;/]+", category):
                clean_value = value.strip()
                if clean_value:
                    categories.add(clean_value)
    return {
        "sourceOptions": sorted_admin_options(sources),
        "categoryOptions": sorted_admin_options(categories),
    }


def admin_article_review_filter_string(filters: dict[str, Any], key: str, maximum: int) -> str:
    value = filters.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"filters.{key} must be a string")
    return value.strip()[:maximum]


def admin_article_review_score(filters: dict[str, Any], key: str) -> int | None:
    value = filters.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ApiError(400, f"filters.{key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"filters.{key} must be a number") from exc
    if not math.isfinite(parsed):
        raise ApiError(400, f"filters.{key} must be a finite number")
    return max(0, min(10, math.floor(parsed)))


def admin_article_review_filters(body: dict[str, Any]) -> dict[str, Any]:
    raw_filters = body.get("filters", {})
    if raw_filters is None:
        raw_filters = {}
    if not isinstance(raw_filters, dict):
        raise ApiError(400, "filters must be an object")

    decision = raw_filters.get("decision", "all")
    if not isinstance(decision, str) or decision not in {"all", "accept", "reject"}:
        decision = "all"
    sort = raw_filters.get("sort", "newest")
    if not isinstance(sort, str) or sort not in {"newest", "oldest"}:
        sort = "newest"

    return {
        "decision": decision,
        "source": admin_article_review_filter_string(raw_filters, "source", 160),
        "category": admin_article_review_filter_string(raw_filters, "category", 96),
        "minScore": admin_article_review_score(raw_filters, "minScore"),
        "maxScore": admin_article_review_score(raw_filters, "maxScore"),
        "page": bounded_int_value(raw_filters.get("page", 0), "filters.page", default=0, minimum=0, maximum=100_000),
        "sort": sort,
    }


def admin_article_review_filter_clause(filters: dict[str, Any], params: list[Any]) -> str:
    conditions = ["true"]
    if filters["decision"] != "all":
        conditions.append("decision = %s")
        params.append(filters["decision"])
    if filters["source"]:
        conditions.append("source = %s")
        params.append(filters["source"])
    if filters["category"]:
        conditions.append("category ilike %s")
        params.append(f"%{filters['category']}%")
    if filters["minScore"] is not None:
        conditions.append("positivity_score >= %s")
        params.append(filters["minScore"])
    if filters["maxScore"] is not None:
        conditions.append("positivity_score <= %s")
        params.append(filters["maxScore"])
    return " where " + " and ".join(conditions)


def load_admin_recent_published_article_rows(store: PostgresStore, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    article_rows = store.fetch_all(
        f"""
        select {admin_published_article_columns()}
        from public.articles
        where status = 'published'
        order by published_on_site_at desc nulls last, published_at desc nulls last, id desc
        limit %s
        """,
        (limit,),
    )
    original_urls = [
        str(row.get("original_url"))
        for row in article_rows
        if row.get("original_url")
    ]
    review_rows: list[dict[str, Any]] = []
    if original_urls:
        review_rows = store.fetch_all(
            f"""
            select {admin_article_review_columns()}
            from public.article_ai_reviews
            where original_url = any(%s)
            order by reviewed_at desc nulls last, id desc
            limit %s
            """,
            (original_urls, min(store.max_limit, len(original_urls))),
        )
    return article_rows, review_rows


def load_admin_article_review_page_rows(
    body: dict[str, Any],
    store: PostgresStore,
    filters: dict[str, Any],
) -> dict[str, Any]:
    page_size = bounded_int(
        body,
        "pageSize",
        default=50,
        minimum=1,
        maximum=min(store.max_limit, 200),
    )
    offset = min(5_000_000, filters["page"] * page_size)
    filter_params: list[Any] = []
    where = admin_article_review_filter_clause(filters, filter_params)
    total_row = store.fetch_one(
        f"""
        select count(*)::bigint as total_matching_reviews
        from public.article_ai_reviews
        {where}
        """,
        tuple(filter_params),
    )
    order_direction = "asc" if filters["sort"] == "oldest" else "desc"
    review_rows = store.fetch_all(
        f"""
        select {admin_article_review_columns()}
        from public.article_ai_reviews
        {where}
        order by reviewed_at {order_direction} nulls last, id {order_direction}
        limit %s offset %s
        """,
        tuple(filter_params + [page_size, offset]),
    )
    original_urls = [
        str(row.get("original_url"))
        for row in review_rows
        if row.get("original_url")
    ]
    published_articles: list[dict[str, Any]] = []
    if original_urls:
        published_articles = store.fetch_all(
            f"""
            select {admin_published_article_columns()}
            from public.articles
            where original_url = any(%s)
            limit %s
            """,
            (original_urls, min(store.max_limit, len(original_urls))),
        )
    return {
        "reviewRows": review_rows,
        "publishedArticlesForReviews": published_articles,
        "totalMatchingReviews": count_value(total_row, "total_matching_reviews"),
        "reviewError": None,
    }


def load_app_admin_article_reviews(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    filters = admin_article_review_filters(body)
    max_option_rows = bounded_int(
        body,
        "maxOptionRows",
        default=5000,
        minimum=1,
        maximum=min(store.max_limit, 5000),
    )
    recent_published_article_limit = bounded_int(
        body,
        "recentPublishedArticleLimit",
        default=10,
        minimum=1,
        maximum=min(store.max_limit, 100),
    )
    version_report_limit = bounded_int(
        body,
        "aiDecisionVersionReportLimit",
        default=20,
        minimum=1,
        maximum=min(store.max_limit, 100),
    )

    options = load_admin_article_review_options(store, max_option_rows)
    recent_article_rows, recent_review_rows = load_admin_recent_published_article_rows(
        store,
        recent_published_article_limit,
    )
    try:
        version_report_rows = store.fetch_all(
            f"""
            select {ai_decision_version_report_columns()}
            from public.ai_decision_version_report
            order by version_rank asc
            limit %s
            """,
            (version_report_limit,),
        )
        version_report_error = None
    except Exception:
        version_report_rows = []
        version_report_error = "ai_decision_version_report query failed"
    review_page = load_admin_article_review_page_rows(body, store, filters)

    return {
        "rows": [
            {
                "sourceOptions": options["sourceOptions"],
                "categoryOptions": options["categoryOptions"],
                "recentPublishedArticleRows": recent_article_rows,
                "recentPublishedReviewRows": recent_review_rows,
                "versionReportRows": version_report_rows,
                "versionReportError": version_report_error,
                "reviewRows": review_page["reviewRows"],
                "publishedArticlesForReviews": review_page["publishedArticlesForReviews"],
                "totalMatchingReviews": review_page["totalMatchingReviews"],
                "reviewError": review_page["reviewError"],
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_article_engagement(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    source_category_limit = bounded_int(
        body,
        "sourceCategoryLimit",
        default=100,
        minimum=1,
        maximum=min(store.max_limit, 1000),
    )
    article_limit = bounded_int(
        body,
        "articleLimit",
        default=25,
        minimum=1,
        maximum=min(store.max_limit, 1000),
    )

    try:
        source_category_rows = store.fetch_all(
            f"""
            select {admin_article_engagement_source_category_columns()}
            from public.article_engagement_source_category_summary
            order by total_engagement_count desc, latest_event_date desc nulls last
            limit %s
            """,
            (source_category_limit,),
        )
        source_category_error = None
    except Exception:
        source_category_rows = []
        source_category_error = "article_engagement_source_category_summary query failed"

    try:
        article_rows = store.fetch_all(
            f"""
            select {admin_article_engagement_article_columns()}
            from public.article_engagement_article_summary
            order by outbound_click_count desc, latest_event_date desc nulls last
            limit %s
            """,
            (article_limit,),
        )
        article_error = None
    except Exception:
        article_rows = []
        article_error = "article_engagement_article_summary query failed"

    return {
        "rows": [
            {
                "sourceCategoryRows": source_category_rows,
                "sourceCategoryError": source_category_error,
                "articleRows": article_rows,
                "articleError": article_error,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_ai_usage(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    since = required_iso_datetime_string(body, "since")
    limit = bounded_int(
        body,
        "limit",
        default=5000,
        minimum=1,
        maximum=min(store.max_limit, 5000),
    )

    usage_run_rows = store.fetch_all(
        f"""
        select {admin_ai_usage_run_columns()}
        from public.ai_usage_runs
        where run_started_at >= %s::timestamptz
        order by run_started_at desc nulls last, id desc
        limit %s
        """,
        (since, limit),
    )

    return {
        "rows": [
            {
                "usageRunRows": usage_run_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_local_ai(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    since = required_iso_datetime_string(body, "since")
    run_limit = bounded_int(
        body,
        "runLimit",
        default=5000,
        minimum=1,
        maximum=min(store.max_limit, 5000),
    )
    review_limit = bounded_int(
        body,
        "reviewLimit",
        default=50,
        minimum=1,
        maximum=min(store.max_limit, 50),
    )

    usage_run_rows = store.fetch_all(
        f"""
        select {admin_local_ai_usage_run_columns()}
        from public.ai_usage_runs
        where (ai_provider = %s or local_ai_call_count > 0)
          and run_started_at >= %s::timestamptz
        order by run_started_at desc nulls last, id desc
        limit %s
        """,
        ("local", since, run_limit),
    )
    recent_review_rows = store.fetch_all(
        f"""
        select {admin_local_ai_review_columns()}
        from public.article_ai_reviews
        where ai_provider = %s
        order by reviewed_at desc nulls last, id desc
        limit %s
        """,
        ("local", review_limit),
    )

    return {
        "rows": [
            {
                "usageRunRows": usage_run_rows,
                "recentReviewRows": recent_review_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_translation_quality(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    audit_limit = bounded_int(
        body,
        "auditLimit",
        default=60,
        minimum=1,
        maximum=min(store.max_limit, 500),
    )
    summary_lookup_limit = bounded_int(
        body,
        "summaryLookupLimit",
        default=20000,
        minimum=1,
        maximum=min(store.max_limit, 20000),
    )
    target_language_codes = production_readiness_target_language_codes(body)

    article_rows = store.fetch_all(
        f"""
        select {admin_translation_quality_article_columns()}
        from public.public_feed_snapshot
        order by snapshot_rank asc
        limit %s
        """,
        (audit_limit,),
    )
    original_urls = [
        str(article.get("original_url"))
        for article in article_rows
        if article.get("original_url")
    ]
    summary_rows: list[dict[str, Any]] = []
    if original_urls and target_language_codes:
        summary_rows = store.fetch_all(
            f"""
            select {admin_translation_quality_summary_columns()}
            from public.article_summaries
            where original_url = any(%s)
              and language_code = any(%s)
            limit %s
            """,
            (
                original_urls,
                target_language_codes,
                min(store.max_limit, summary_lookup_limit, len(original_urls) * len(target_language_codes)),
            ),
        )

    return {
        "rows": [
            {
                "articleRows": article_rows,
                "summaryRows": summary_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def guardrails_count_tables(body: dict[str, Any]) -> list[str]:
    if "countTables" not in body:
        return list(ADMIN_GUARDRAILS_COUNT_TABLES)

    count_tables = list(dict.fromkeys(string_list(body, "countTables", maximum=10)))
    unsupported = [
        table_name
        for table_name in count_tables
        if table_name not in ADMIN_GUARDRAILS_COUNT_TABLES
    ]
    if unsupported:
        raise ApiError(
            400,
            "countTables must only contain articles, article_summaries, and rss_feeds",
        )
    return count_tables


def guardrails_fetch_all(
    store: PostgresStore,
    query: str,
    params: tuple[Any, ...],
    *,
    label: str,
    partial_errors: list[str],
) -> list[dict[str, Any]]:
    try:
        return store.fetch_all(query, params)
    except Exception:
        partial_errors.append(f"{label} query failed")
        return []


def guardrails_count_table(
    store: PostgresStore,
    table_name: str,
    *,
    partial_errors: list[str],
) -> int | None:
    table_sql, _response_key, query_key = ADMIN_GUARDRAILS_COUNT_TABLES[table_name]
    try:
        return count_value(
            store.fetch_one(f"select count(*)::bigint as {query_key} from {table_sql}"),
            query_key,
        )
    except Exception:
        partial_errors.append(f"{table_name} count query failed")
        return None


def bounded_worker_uplift_stage_names(body: dict[str, Any]) -> list[str]:
    requested = string_list(body, "stages", maximum=len(WORKER_UPLIFT_STAGES))
    if not requested:
        return list(WORKER_UPLIFT_STAGES)
    normalized = list(dict.fromkeys(stage.strip().lower().replace("-", "_") for stage in requested if stage.strip()))
    unsupported = [stage for stage in normalized if stage not in WORKER_UPLIFT_STAGES]
    if unsupported:
        raise ApiError(400, "stages must contain known worker-uplift stages")
    return normalized


def normalize_worker_uplift_owner(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "legacy": "legacy_shards",
        "legacy_worker": "legacy_shards",
        "legacy_workers": "legacy_shards",
        "uplift": "worker_uplift",
        "rabbitmq": "worker_uplift",
        "worker_uplift_primary": "worker_uplift",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in WORKER_UPLIFT_ACTIVE_INGESTION_OWNER_SET else None


def worker_uplift_cutover_state(store: PostgresStore) -> str:
    value = str(getattr(store, "worker_uplift_cutover_state", "shadow") or "shadow").strip().lower()
    return value or "shadow"


def worker_uplift_active_ingestion_owner(body: dict[str, Any], store: PostgresStore) -> str:
    requested = normalize_worker_uplift_owner(optional_string(body, "activeIngestionOwner", maximum=64))
    if requested:
        return requested
    cutover_state = worker_uplift_cutover_state(store)
    if "rollback" in cutover_state:
        return "rollback"
    if cutover_state == "cutover-approved":
        return "worker_uplift"
    return "legacy_shards"


def safe_projection_int(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_projection_float(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_projection_string(row: dict[str, Any], key: str, *, maximum: int = 256) -> str | None:
    value = row.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def normalize_worker_uplift_stage_status(row: dict[str, Any], active_owner: str) -> str:
    status = safe_projection_string(row, "stage_status", maximum=64)
    if status in {"failed", "degraded", "rollback", "legacy_only", "unknown"}:
        return status
    if active_owner == "rollback":
        return "rollback"
    if active_owner == "legacy_shards" and not any(
        row.get(key) is not None
        for key in ("last_attempt_at", "last_success_at", "last_failure_at", "updated_at", "deployment_version")
    ):
        return "legacy_only"
    dlq_count = safe_projection_int(row, "dlq_count") or 0
    consecutive_failures = safe_projection_int(row, "consecutive_failure_count") or 0
    stale_status = safe_projection_string(row, "stale_status", maximum=64)
    if dlq_count > 0 or consecutive_failures > 0:
        return "degraded"
    if stale_status == "stale":
        return "stale"
    if status == "healthy":
        return "healthy"
    if row.get("last_success_at") is not None or row.get("updated_at") is not None:
        return "healthy"
    return "unknown"


def normalize_worker_uplift_stale_status(row: dict[str, Any]) -> str:
    status = safe_projection_string(row, "stale_status", maximum=64)
    return status if status in WORKER_UPLIFT_STALE_STATUS_SET else "unknown"


def empty_worker_uplift_stage(stage: str, active_owner: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "activeIngestionOwner": active_owner,
        "stageStatus": "rollback" if active_owner == "rollback" else "legacy_only",
        "staleStatus": "unknown",
        "lastAttemptAt": None,
        "lastSuccessAt": None,
        "lastFailureAt": None,
        "consecutiveFailureCount": 0,
        "throughputPerMinute": None,
        "latencyP50Ms": None,
        "latencyP95Ms": None,
        "retryCount": 0,
        "dlqCount": 0,
        "queueAgeSeconds": None,
        "activeConsumers": None,
        "deploymentVersion": None,
        "telemetryVersion": None,
        "projectionVersion": None,
        "updatedAt": None,
        "errorClass": None,
        "sanitizedErrorMessage": None,
    }


def worker_uplift_stage_from_row(row: dict[str, Any], active_owner: str) -> dict[str, Any] | None:
    stage = safe_projection_string(row, "stage_name", maximum=64)
    if stage not in WORKER_UPLIFT_STAGES:
        return None
    normalized = empty_worker_uplift_stage(stage, active_owner)
    owner = normalize_worker_uplift_owner(safe_projection_string(row, "active_ingestion_owner", maximum=64))
    if owner:
        normalized["activeIngestionOwner"] = owner
    normalized.update(
        {
            "stageStatus": normalize_worker_uplift_stage_status(row, active_owner),
            "staleStatus": normalize_worker_uplift_stale_status(row),
            "lastAttemptAt": row.get("last_attempt_at"),
            "lastSuccessAt": row.get("last_success_at"),
            "lastFailureAt": row.get("last_failure_at"),
            "consecutiveFailureCount": safe_projection_int(row, "consecutive_failure_count") or 0,
            "throughputPerMinute": safe_projection_float(row, "throughput_per_minute"),
            "latencyP50Ms": safe_projection_int(row, "latency_p50_ms"),
            "latencyP95Ms": safe_projection_int(row, "latency_p95_ms"),
            "retryCount": safe_projection_int(row, "retry_count") or 0,
            "dlqCount": safe_projection_int(row, "dlq_count") or 0,
            "queueAgeSeconds": safe_projection_int(row, "queue_age_seconds"),
            "activeConsumers": safe_projection_int(row, "active_consumers"),
            "deploymentVersion": safe_projection_string(row, "deployment_version", maximum=128),
            "telemetryVersion": safe_projection_int(row, "telemetry_version"),
            "projectionVersion": safe_projection_int(row, "projection_version"),
            "updatedAt": row.get("updated_at"),
            "errorClass": safe_projection_string(row, "sanitized_error_code", maximum=128),
            "sanitizedErrorMessage": safe_projection_string(row, "sanitized_error_message", maximum=512),
        }
    )
    if normalized["dlqCount"] > 0 and normalized["stageStatus"] == "healthy":
        normalized["stageStatus"] = "degraded"
    return normalized


def stage_order_sql(column_name: str = "stage_name") -> str:
    clauses = " ".join(f"when '{stage}' then {index}" for index, stage in enumerate(WORKER_UPLIFT_STAGES))
    return f"case {column_name} {clauses} else 999 end"


def fetch_worker_uplift_stage_projection_rows(
    store: PostgresStore,
    stage_names: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    return store.fetch_all(
        f"""
        select stage_name, active_ingestion_owner, stage_status, stale_status,
               last_attempt_at, last_success_at, last_failure_at,
               consecutive_failure_count, throughput_per_minute,
               latency_p50_ms, latency_p95_ms, retry_count, dlq_count,
               queue_age_seconds, active_consumers, deployment_version,
               telemetry_version, projection_version, sanitized_error_code,
               sanitized_error_message, updated_at
        from worker_uplift_final.stage_health_projections
        where stage_name = any(%s)
        order by {stage_order_sql()}
        limit %s
        """,
        (stage_names, limit),
    )


def worker_uplift_overall_status(
    active_owner: str,
    stage_rows: list[dict[str, Any]],
    partial_errors: list[dict[str, Any]],
) -> str:
    if active_owner == "rollback":
        return "rollback"
    statuses = {str(row.get("stageStatus")) for row in stage_rows}
    if any(status in {"failed", "degraded"} for status in statuses):
        return "degraded"
    if "stale" in statuses:
        return "stale"
    if partial_errors:
        return "partial"
    if statuses and statuses <= {"legacy_only"}:
        return "legacy_only"
    if statuses and statuses <= {"healthy"}:
        return "healthy"
    return "unknown"


def load_worker_uplift_stage_health_projection(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    stage_names = bounded_worker_uplift_stage_names(body)
    stage_limit = bounded_int_value(
        body.get("stageLimit", len(stage_names)),
        "stageLimit",
        default=len(stage_names),
        minimum=1,
        maximum=len(WORKER_UPLIFT_STAGES),
    )
    active_owner = worker_uplift_active_ingestion_owner(body, store)
    partial_errors: list[dict[str, Any]] = []
    stage_rows_by_name = {
        stage: empty_worker_uplift_stage(stage, active_owner)
        for stage in stage_names[:stage_limit]
    }

    try:
        projection_rows = fetch_worker_uplift_stage_projection_rows(store, list(stage_rows_by_name), stage_limit)
    except Exception as exc:
        partial_errors.append(
            {
                "source": "worker_uplift_final.stage_health_projections",
                "errorClass": exc.__class__.__name__,
                "redacted": True,
            }
        )
        projection_rows = []

    for row in projection_rows:
        stage_row = worker_uplift_stage_from_row(row, active_owner)
        if stage_row is not None and stage_row["stage"] in stage_rows_by_name:
            stage_rows_by_name[stage_row["stage"]] = stage_row

    stage_rows = [stage_rows_by_name[stage] for stage in stage_rows_by_name]
    overall_status = worker_uplift_overall_status(active_owner, stage_rows, partial_errors)
    return {
        "schemaVersion": WORKER_UPLIFT_ADMIN_PROJECTION_VERSION,
        "source": "backend_postgres_durable_projection",
        "grafanaDependency": False,
        "activeIngestionOwner": active_owner,
        "cutoverState": worker_uplift_cutover_state(store),
        "productionWritesEnabled": bool(getattr(store, "worker_uplift_production_writes_enabled", False)),
        "overallStatus": overall_status,
        "stageRows": stage_rows,
        "partialErrors": partial_errors,
        "links": {
            "dashboardPath": "grafana/backend-metrics/dashboards.json",
            "runbookPath": "runbooks/WORKER_UPLIFT_RABBITMQ_METRICS.md",
        },
    }


def load_app_admin_worker_uplift_health(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    return {
        "rows": [
            {
                "workerUpliftHealth": load_worker_uplift_stage_health_projection(body, store),
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_guardrails(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    since = required_iso_datetime_string(body, "since")
    limit = bounded_int(
        body,
        "limit",
        default=10000,
        minimum=1,
        maximum=min(store.max_limit, 10000),
    )
    requested_count_tables = guardrails_count_tables(body)
    partial_errors: list[str] = []

    ai_usage_run_rows = guardrails_fetch_all(
        store,
        f"""
        select {admin_guardrails_ai_usage_run_columns()}
        from public.ai_usage_runs
        where run_started_at >= %s::timestamptz
        order by run_started_at desc nulls last, id desc
        limit %s
        """,
        (since, limit),
        label="ai_usage_runs",
        partial_errors=partial_errors,
    )
    worker_run_rows = guardrails_fetch_all(
        store,
        f"""
        select {admin_guardrails_worker_run_columns()}
        from public.worker_runs
        where run_started_at >= %s::timestamptz
        order by run_started_at desc nulls last, id desc
        limit %s
        """,
        (since, limit),
        label="worker_runs",
        partial_errors=partial_errors,
    )
    quota_usage_event_rows = guardrails_fetch_all(
        store,
        f"""
        select {admin_guardrails_quota_usage_event_columns()}
        from public.quota_usage_events
        where created_at >= %s::timestamptz
        order by created_at desc nulls last, id desc
        limit %s
        """,
        (since, limit),
        label="quota_usage_events",
        partial_errors=partial_errors,
    )
    counts = {
        "articleCount": None,
        "summaryCount": None,
        "feedCount": None,
    }
    for table_name in requested_count_tables:
        _table_sql, response_key, _query_key = ADMIN_GUARDRAILS_COUNT_TABLES[table_name]
        counts[response_key] = guardrails_count_table(
            store,
            table_name,
            partial_errors=partial_errors,
        )

    return {
        "rows": [
            {
                "aiUsageRunRows": ai_usage_run_rows,
                "workerRunRows": worker_run_rows,
                "quotaUsageEventRows": quota_usage_event_rows,
                "articleCount": counts["articleCount"],
                "summaryCount": counts["summaryCount"],
                "feedCount": counts["feedCount"],
                "partialErrors": partial_errors,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_worker_shards(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    limit = bounded_int(
        body,
        "limit",
        default=500,
        minimum=1,
        maximum=min(store.max_limit, 500),
    )
    if "shardCount" in body:
        bounded_int(body, "shardCount", default=25, minimum=1, maximum=1000)
    if "staleAfterMinutes" in body:
        bounded_int(body, "staleAfterMinutes", default=180, minimum=1, maximum=24 * 60)
    if "slowRunMs" in body:
        bounded_int(body, "slowRunMs", default=15000, minimum=1, maximum=60 * 60 * 1000)
    if "dailyWindowDays" in body:
        bounded_int(body, "dailyWindowDays", default=7, minimum=1, maximum=90)

    worker_run_rows = store.fetch_all(
        f"""
        select {admin_worker_shards_run_columns()}
        from public.worker_runs
        order by run_started_at desc nulls last, id desc
        limit %s
        """,
        (limit,),
    )
    worker_uplift_health = load_worker_uplift_stage_health_projection(body, store)

    return {
        "rows": [
            {
                "workerRunRows": worker_run_rows,
                "workerUpliftHealth": worker_uplift_health,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_rss_feed_health(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    limit = bounded_int(
        body,
        "limit",
        default=10000,
        minimum=1,
        maximum=min(store.max_limit, 10000),
    )
    if "staleAfterHours" in body:
        bounded_int(body, "staleAfterHours", default=24, minimum=1, maximum=24 * 30)

    rss_feed_rows = store.fetch_all(
        f"""
        select {admin_rss_feed_health_rss_feed_columns()}
        from public.rss_feeds
        order by id asc
        limit %s
        """,
        (limit,),
    )
    feed_health_rows = store.fetch_all(
        f"""
        select {admin_rss_feed_health_feed_health_columns()}
        from public.feed_health
        order by total_accepted_count desc nulls last, id desc
        limit %s
        """,
        (limit,),
    )

    return {
        "rows": [
            {
                "rssFeedRows": rss_feed_rows,
                "feedHealthRows": feed_health_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_feed_management(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    limit = bounded_int(
        body,
        "limit",
        default=10000,
        minimum=1,
        maximum=min(store.max_limit, 10000),
    )

    feed_quality_rows = store.fetch_all(
        f"""
        select {admin_feed_management_feed_quality_columns()}
        from public.feed_quality_scores
        order by quality_score asc nulls first,
                 total_accepted_count desc nulls last,
                 source asc
        limit %s
        """,
        (limit,),
    )

    return {
        "rows": [
            {
                "feedQualityRows": feed_quality_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_audit_log(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    limit = bounded_int(
        body,
        "limit",
        default=50,
        minimum=1,
        maximum=min(store.max_limit, 50),
    )

    audit_event_rows = store.fetch_all(
        f"""
        select {admin_audit_log_event_columns()}
        from public.admin_audit_events
        order by created_at desc nulls last, id desc
        limit %s
        """,
        (limit,),
    )

    return {
        "rows": [
            {
                "auditEventRows": audit_event_rows,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_runtime_feature_flags(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    limit = bounded_int(
        body,
        "limit",
        default=100,
        minimum=1,
        maximum=min(store.max_limit, 100),
    )
    offset = bounded_int(body, "offset", default=0, minimum=0, maximum=1_000_000)

    rows = store.fetch_all(
        f"""
        select {admin_runtime_feature_flag_columns()}
        from public.runtime_feature_flags
        order by key asc
        limit %s offset %s
        """,
        (limit, offset),
    )

    return {
        "rows": rows,
        "rowCount": len(rows),
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_production_readiness(body: dict[str, Any], store: PostgresStore) -> dict[str, Any]:
    recent_article_limit = bounded_int(
        body,
        "recentArticleLimit",
        default=100,
        minimum=1,
        maximum=min(store.max_limit, 500),
    )
    translation_sample_limit = bounded_int(
        body,
        "translationSampleLimit",
        default=60,
        minimum=1,
        maximum=min(store.max_limit, recent_article_limit),
    )
    target_language_codes = production_readiness_target_language_codes(body)
    articles_last_24_hours_window = production_readiness_growth_window_hours(body, 0, 24)
    articles_last_7_days_window = production_readiness_growth_window_hours(body, 1, 24 * 7)

    article_count = count_value(
        store.fetch_one("select count(*)::bigint as article_count from public.articles"),
        "article_count",
    )
    public_feed_snapshot_count = count_value(
        store.fetch_one(
            "select count(*)::bigint as public_feed_snapshot_count from public.public_feed_snapshot"
        ),
        "public_feed_snapshot_count",
    )
    recent_articles = store.fetch_all(
        """
        select id, original_url, image_url, published_on_site_at, created_at
        from public.articles
        where status = 'published'
        order by published_on_site_at desc nulls last, created_at desc nulls last, id desc
        limit %s
        """,
        (recent_article_limit,),
    )
    worker_run = store.fetch_one(
        """
        select id, run_started_at, run_completed_at, success, error_name, error_message,
               feed_count, fetched_count, candidate_count, accepted_count, rejected_count, duration_ms
        from public.worker_runs
        order by run_started_at desc nulls last, id desc
        limit 1
        """
    )
    articles_last_24_hours = count_published_articles_since(store, articles_last_24_hours_window)
    articles_last_7_days = count_published_articles_since(store, articles_last_7_days_window)

    translation_original_urls = [
        str(article.get("original_url"))
        for article in recent_articles[:translation_sample_limit]
        if article.get("original_url")
    ]
    translation_expected_count = len(translation_original_urls) * len(target_language_codes)
    translation_summaries: list[dict[str, Any]] = []
    if translation_expected_count:
        translation_summaries = store.fetch_all(
            """
            select original_url, language_code
            from public.article_summaries
            where original_url = any(%s)
              and language_code = any(%s)
            limit %s
            """,
            (
                translation_original_urls,
                target_language_codes,
                min(store.max_limit, translation_expected_count),
            ),
        )
    worker_uplift_health = load_worker_uplift_stage_health_projection(body, store)

    return {
        "rows": [
            {
                "articleCount": article_count,
                "publicFeedSnapshotCount": public_feed_snapshot_count,
                "recentArticles": recent_articles,
                "workerRun": worker_run,
                "articlesLast24Hours": articles_last_24_hours,
                "articlesLast7Days": articles_last_7_days,
                "translationSummaries": translation_summaries,
                "translationExpectedCount": translation_expected_count,
                "workerUpliftHealth": worker_uplift_health,
            }
        ],
        "rowCount": 1,
        "generatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def load_app_admin_rows(operation: str, body: dict[str, Any], store: PostgresStore) -> list[dict[str, Any]]:
    table, columns, order_by = ADMIN_TABLE_READS[operation]
    limit = bounded_int(body, "limit", default=100, minimum=1, maximum=store.max_limit)
    offset = bounded_int(body, "offset", default=0, minimum=0, maximum=1_000_000)
    return store.fetch_all(
        f"select {columns} from {table} order by {order_by} limit %s offset %s",
        (limit, offset),
    )


def handle_app_operation(operation: str, body: dict[str, Any], store: PostgresStore) -> Any:
    if operation not in APP_READ_OPERATIONS and operation not in APP_WRITE_OPERATIONS:
        raise ApiError(404, f"unknown app database operation: {operation}")

    assert_write_allowed(operation, body, store, write_operations=APP_WRITE_OPERATIONS)

    if operation == "app-provider-smoke":
        return {
            "ok": True,
            "provider": "backend_postgres",
            "writesEnabled": store.writes_enabled,
        }

    if operation == "load-readiness-schema-contract":
        return store.fetch_all(
            """
            select legacy_schema_version, migration_head, expected_schema_fingerprint, actual_schema_fingerprint
            from public.nutsnews_migration_schema_contract()
            """
        )

    if operation == "get-runtime-feature-flag":
        key = body.get("key")
        if not isinstance(key, str):
            raise ApiError(400, "key must be a string")
        default = RUNTIME_FEATURE_FLAG_DEFAULTS.get(key, False)
        row = store.fetch_one("select enabled from public.runtime_feature_flags where key = %s limit 1", (key,))
        return {"key": key, "enabled": bool(row["enabled"]) if row and isinstance(row.get("enabled"), bool) else default}

    if operation in {"load-public-feed-snapshot", "load-home-feed-snapshot"}:
        default_limit = 250 if operation == "load-home-feed-snapshot" else 6
        max_limit = min(store.max_limit, 250)
        limit = bounded_int(body, "limit", default=default_limit, minimum=1, maximum=max_limit)
        offset = bounded_int(body, "offset", default=0, minimum=0, maximum=1_000_000)
        requested_language_code = requested_edge_snapshot_language_code(body)
        params: list[Any] = []
        where = " where true" + category_clause(body, params)
        params.extend((limit, offset))
        articles = store.fetch_all(
            f"""
            select {app_article_columns()}
            from public.public_feed_snapshot
            {where}
            order by snapshot_rank asc
            limit %s offset %s
            """,
            tuple(params),
        )
        summaries: list[dict[str, Any]] = []
        original_urls = [
            str(article.get("original_url"))
            for article in articles
            if article.get("original_url")
        ]
        if requested_language_code != DEFAULT_LANGUAGE_CODE and original_urls:
            summaries = store.fetch_all(
                """
                select original_url, language_code, title, summary
                from public.article_summaries
                where original_url = any(%s)
                  and language_code = %s
                limit %s
                """,
                (original_urls, requested_language_code, min(store.max_limit, len(original_urls))),
            )
        return localize_public_feed_edge_snapshot_articles(articles, summaries, requested_language_code)

    if operation == "load-published-articles":
        limit = bounded_int(body, "limit", default=6, minimum=1, maximum=min(store.max_limit, 100))
        offset = bounded_int(body, "offset", default=0, minimum=0, maximum=1_000_000)
        params = []
        where = published_article_filters(body, params)
        params.extend((limit, offset))
        return store.fetch_all(
            f"""
            select {app_article_columns()}
            from public.articles
            {where}
            order by published_on_site_at desc nulls last, id desc
            limit %s offset %s
            """,
            tuple(params),
        )

    if operation == "load-published-categories":
        limit = bounded_int(body, "limit", default=1000, minimum=1, maximum=store.max_limit)
        return store.fetch_all(
            """
            select category
            from public.public_feed_snapshot
            where category is not null
            limit %s
            """,
            (limit,),
        )

    if operation == "load-article-detail":
        article_id = required_string(body, "id", maximum=128)
        return store.fetch_one(
            f"""
            select {app_article_columns()}
            from public.articles
            where status = 'published'
              and id = %s
              and image_url is not null
              and image_url <> ''
            limit 1
            """,
            (article_id,),
        )

    if operation == "load-recent-article-sitemap-items":
        limit = bounded_int(body, "limit", default=100, minimum=1, maximum=50000)
        return store.fetch_all(
            f"""
            select {sitemap_article_columns()}
            from public.articles
            where status = 'published'
              and image_url is not null
              and image_url <> ''
            order by published_on_site_at desc nulls last, id desc
            limit %s
            """,
            (limit,),
        )

    if operation == "load-published-article-sitemap-count":
        row = store.fetch_one(
            """
            select count(*)::bigint as article_count
            from public.articles
            where status = 'published'
              and image_url is not null
              and image_url <> ''
            """
        )
        return {"articleCount": int(row["article_count"]) if row else 0}

    if operation == "load-article-sitemap-items-page":
        offset = bounded_int(body, "offset", default=0, minimum=0, maximum=5_000_000)
        limit = bounded_int(body, "limit", default=5000, minimum=1, maximum=50000)
        return store.fetch_all(
            f"""
            select {sitemap_article_columns()}
            from public.articles
            where status = 'published'
              and image_url is not null
              and image_url <> ''
            order by published_on_site_at desc nulls last, id desc
            limit %s offset %s
            """,
            (limit, offset),
        )

    if operation == "search-published-articles":
        query = required_string(body, "query", maximum=80)
        page_size = bounded_int(body, "pageSize", default=20, minimum=1, maximum=50)
        page_offset = bounded_int(body, "pageOffset", default=0, minimum=0, maximum=1_000_000)
        return store.fetch_all(
            f"select {app_article_columns()} from public.search_articles(%s, %s, %s)",
            (query, page_size, page_offset),
        )

    if operation == "load-admin-production-readiness":
        return load_app_admin_production_readiness(body, store)

    if operation == "load-admin-article-reviews":
        return load_app_admin_article_reviews(body, store)

    if operation == "load-admin-article-engagement":
        return load_app_admin_article_engagement(body, store)

    if operation == "load-admin-ai-usage":
        return load_app_admin_ai_usage(body, store)

    if operation == "load-admin-local-ai":
        return load_app_admin_local_ai(body, store)

    if operation == "load-admin-translation-quality":
        return load_app_admin_translation_quality(body, store)

    if operation == "load-admin-guardrails":
        return load_app_admin_guardrails(body, store)

    if operation == "load-admin-worker-shards":
        return load_app_admin_worker_shards(body, store)

    if operation == "load-admin-worker-uplift-health":
        return load_app_admin_worker_uplift_health(body, store)

    if operation == "load-admin-rss-feed-health":
        return load_app_admin_rss_feed_health(body, store)

    if operation == "load-admin-feed-management":
        return load_app_admin_feed_management(body, store)

    if operation == "load-admin-audit-log":
        return load_app_admin_audit_log(body, store)

    if operation == "load-admin-runtime-feature-flags":
        return load_app_admin_runtime_feature_flags(body, store)

    if operation in ADMIN_TABLE_READS:
        return load_app_admin_rows(operation, body, store)

    if operation == "record-quota-usage-event":
        event_type = required_string(body, "eventType", maximum=120)
        event_source = optional_string(body, "eventSource", maximum=160) or "unknown"
        provider = optional_string(body, "provider", maximum=120)
        quantity = bounded_int(body, "quantity", default=1, minimum=1, maximum=1000000)
        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ApiError(400, "metadata must be an object")
        store.execute(
            """
            insert into public.quota_usage_events (event_type, event_source, provider, quantity, metadata)
            values (%s, %s, %s, %s, %s::jsonb)
            """,
            (event_type, event_source, provider, quantity, json.dumps(metadata, separators=(",", ":"))),
        )
        return {"ok": True}

    if operation == "record-article-engagement-event":
        event_type = required_string(body, "eventType", maximum=64)
        article_id = optional_string(body, "articleId", maximum=128)
        source = optional_string(body, "source", maximum=160)
        category = optional_string(body, "category", maximum=96)
        quantity = bounded_int(body, "quantity", default=1, minimum=1, maximum=10)
        row = store.fetch_one(
            "select * from public.record_article_engagement_event(%s, %s, %s, %s, %s)",
            (event_type, article_id, source, category, quantity),
        )
        return {"ok": True, "row": row}

    if operation == "set-runtime-feature-flag":
        key = required_string(body, "key", maximum=120)
        enabled = optional_bool(body, "enabled")
        store.execute(
            """
            insert into public.runtime_feature_flags (key, enabled)
            values (%s, %s)
            on conflict (key) do update set enabled = excluded.enabled
            """,
            (key, enabled),
        )
        return {"ok": True}

    raise ApiError(404, f"unknown app database operation: {operation}")


class WorkerDbApiHandler(BaseHTTPRequestHandler):
    server_version = "NutsNewsDbCompatApi/1.0"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.write_json(200, {"status": "ok"})
            return
        self.write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            self.handle_post()
        except ApiError as error:
            self.write_json(error.status, {"error": error.message})
        except Exception as error:
            self.write_json(500, internal_error_payload(error))

    def handle_post(self) -> None:
        route = None
        if self.path.startswith("/api/worker/db/"):
            route = "worker"
        elif self.path.startswith("/api/app/db/"):
            route = "app"
        else:
            raise ApiError(404, "not found")
        operation = self.path.rsplit("/", 1)[-1]
        auth_scope = authenticate_database_api_token(self.headers.get("authorization", ""))
        content_length = int(self.headers.get("content-length", "0") or "0")
        if content_length > 2_000_000:
            raise ApiError(413, "request body too large")
        try:
            body = json.loads(self.rfile.read(content_length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(400, "request body must be JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "request body must be a JSON object")
        if route == "worker":
            result = handle_operation(operation, body, self.server.store, auth_scope=auth_scope)  # type: ignore[attr-defined]
        else:
            if auth_scope != LEGACY_WORKER_API_SCOPE:
                raise ApiError(403, "worker-uplift scoped token cannot call app database operations")
            result = handle_app_operation(operation, body, self.server.store)  # type: ignore[attr-defined]
        self.write_json(200, result)

    def write_json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, default=json_default, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


class WorkerDbApiServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler_class)
        self.store = PostgresStore()


def main() -> int:
    bind = os.environ.get("NUTSNEWS_WORKER_DB_API_BIND", "127.0.0.1")
    port = int(os.environ.get("NUTSNEWS_WORKER_DB_API_PORT", "8093"))
    server = WorkerDbApiServer((bind, port), WorkerDbApiHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
