#!/usr/bin/env python3
"""Loopback-only NutsNews database compatibility API."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


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
}

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
    return {
        "ok": True,
        "requestedCount": len(original_urls),
        "publishedCount": len(published_rows),
        "blockedCount": 0,
        "missingTranslations": [],
    }


def handle_operation(operation: str, body: dict[str, Any], store: PostgresStore) -> Any:
    if operation not in READ_OPERATIONS and operation not in WRITE_OPERATIONS:
        raise ApiError(404, f"unknown worker database operation: {operation}")

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
        except Exception:
            self.write_json(500, {"error": "internal database compatibility API error"})

    def handle_post(self) -> None:
        route = None
        if self.path.startswith("/api/worker/db/"):
            route = "worker"
        elif self.path.startswith("/api/app/db/"):
            route = "app"
        else:
            raise ApiError(404, "not found")
        operation = self.path.rsplit("/", 1)[-1]
        expected_token = os.environ.get("NUTSNEWS_BACKEND_API_TOKEN", "")
        if not expected_token:
            raise ApiError(503, "database compatibility API token is not configured")
        authorization = self.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization.removeprefix("Bearer "), expected_token):
            raise ApiError(401, "invalid database compatibility API token")
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
            result = handle_operation(operation, body, self.server.store)  # type: ignore[attr-defined]
        else:
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
