#!/usr/bin/env python3
"""Smoke test the backend app database compatibility API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 15


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def request_json(base_url: str, token: str, operation: str, body: dict[str, Any]) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}/{operation}"
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-nutsnews-db-client": "backend-app-smoke",
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload or "{}")
    except HTTPError as error:
        payload = error.read().decode("utf-8")
        try:
            parsed: Any = json.loads(payload or "{}")
        except json.JSONDecodeError:
            parsed = {"error": payload}
        return error.code, parsed
    except URLError as error:
        raise RuntimeError(f"request failed for {operation}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--base-url-env", default="NUTSNEWS_BACKEND_API_URL")
    parser.add_argument("--token-env", default="NUTSNEWS_BACKEND_API_TOKEN")
    args = parser.parse_args()

    base_url = env_value(args.base_url_env)
    token = env_value(args.token_env)
    if args.offline or not base_url or not token:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "offline mode" if args.offline else "missing_backend_api_url_or_token",
                    "base_url_env": args.base_url_env,
                    "token_env": args.token_env,
                    "supabase_required": False,
                },
                sort_keys=True,
            )
        )
        return 0

    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    smoke_status, smoke_payload = request_json(
        base_url,
        token,
        "app-provider-smoke",
        {"providerMode": "backend_postgres_shadow"},
    )
    checks.append({"operation": "app-provider-smoke", "status": smoke_status, "payload": smoke_payload})
    if smoke_status != 200 or smoke_payload.get("ok") is not True:
        failures.append("app-provider-smoke")
    writes_enabled = bool(smoke_payload.get("writesEnabled")) if isinstance(smoke_payload, dict) else False

    read_status, read_payload = request_json(
        base_url,
        token,
        "load-public-feed-snapshot",
        {"providerMode": "backend_postgres_shadow", "limit": 5},
    )
    row_count = len(read_payload) if isinstance(read_payload, list) else None
    checks.append({"operation": "load-public-feed-snapshot", "status": read_status, "row_count": row_count})
    if read_status != 200 or not isinstance(read_payload, list):
        failures.append("load-public-feed-snapshot")

    readiness_status, readiness_payload = request_json(
        base_url,
        token,
        "load-admin-production-readiness",
        {
            "providerMode": "backend_postgres_primary",
            "recentArticleLimit": 100,
            "translationSampleLimit": 60,
            "defaultLanguageCode": "en",
            "targetLanguageCodes": ["fr", "ja", "de-CH", "de", "el"],
            "articleGrowthWindowsHours": [24, 24 * 7],
        },
    )
    readiness_rows = (
        readiness_payload.get("rows")
        if isinstance(readiness_payload, dict)
        else None
    )
    checks.append(
        {
            "operation": "load-admin-production-readiness",
            "status": readiness_status,
            "row_count": len(readiness_rows) if isinstance(readiness_rows, list) else None,
        }
    )
    if readiness_status != 200 or not isinstance(readiness_rows, list) or not readiness_rows:
        failures.append("load-admin-production-readiness")

    worker_shards_status, worker_shards_payload = request_json(
        base_url,
        token,
        "load-admin-worker-shards",
        {
            "providerMode": "backend_postgres_primary",
            "limit": 20,
            "shardCount": 25,
            "staleAfterMinutes": 180,
            "slowRunMs": 15000,
            "dailyWindowDays": 7,
        },
    )
    worker_shards_rows = (
        worker_shards_payload.get("rows")
        if isinstance(worker_shards_payload, dict)
        else None
    )
    worker_shards_snapshot = (
        worker_shards_rows[0]
        if isinstance(worker_shards_rows, list) and worker_shards_rows
        else {}
    )
    worker_run_rows = (
        worker_shards_snapshot.get("workerRunRows")
        if isinstance(worker_shards_snapshot, dict)
        else None
    )
    missing_worker_shards_fields = sorted(
        field
        for field in {"workerRunRows"}
        if not isinstance(worker_shards_snapshot, dict) or field not in worker_shards_snapshot
    )
    first_worker_run = (
        worker_run_rows[0]
        if isinstance(worker_run_rows, list) and worker_run_rows
        else {}
    )
    missing_worker_run_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_worker_run, dict)
        and first_worker_run
        and field not in first_worker_run
    )
    checks.append(
        {
            "operation": "load-admin-worker-shards",
            "status": worker_shards_status,
            "row_count": len(worker_shards_rows) if isinstance(worker_shards_rows, list) else None,
            "worker_run_count": len(worker_run_rows) if isinstance(worker_run_rows, list) else None,
            "missing_fields": missing_worker_shards_fields,
            "missing_run_fields": missing_worker_run_fields,
        }
    )
    if (
        worker_shards_status != 200
        or not isinstance(worker_shards_rows, list)
        or not worker_shards_rows
        or not isinstance(worker_run_rows, list)
        or missing_worker_shards_fields
        or missing_worker_run_fields
    ):
        failures.append("load-admin-worker-shards")

    rss_feed_health_status, rss_feed_health_payload = request_json(
        base_url,
        token,
        "load-admin-rss-feed-health",
        {
            "providerMode": "backend_postgres_primary",
            "limit": 20,
            "staleAfterHours": 24,
        },
    )
    rss_feed_health_rows = (
        rss_feed_health_payload.get("rows")
        if isinstance(rss_feed_health_payload, dict)
        else None
    )
    rss_feed_health_snapshot = (
        rss_feed_health_rows[0]
        if isinstance(rss_feed_health_rows, list) and rss_feed_health_rows
        else {}
    )
    rss_feed_rows = (
        rss_feed_health_snapshot.get("rssFeedRows")
        if isinstance(rss_feed_health_snapshot, dict)
        else None
    )
    feed_health_rows = (
        rss_feed_health_snapshot.get("feedHealthRows")
        if isinstance(rss_feed_health_snapshot, dict)
        else None
    )
    missing_rss_feed_health_fields = sorted(
        field
        for field in {"rssFeedRows", "feedHealthRows"}
        if not isinstance(rss_feed_health_snapshot, dict) or field not in rss_feed_health_snapshot
    )
    first_rss_feed = (
        rss_feed_rows[0]
        if isinstance(rss_feed_rows, list) and rss_feed_rows
        else {}
    )
    missing_rss_feed_fields = sorted(
        field
        for field in {"source", "url", "is_positive_source", "is_active"}
        if isinstance(first_rss_feed, dict)
        and first_rss_feed
        and field not in first_rss_feed
    )
    first_feed_health = (
        feed_health_rows[0]
        if isinstance(feed_health_rows, list) and feed_health_rows
        else {}
    )
    missing_feed_health_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_feed_health, dict)
        and first_feed_health
        and field not in first_feed_health
    )
    checks.append(
        {
            "operation": "load-admin-rss-feed-health",
            "status": rss_feed_health_status,
            "row_count": len(rss_feed_health_rows) if isinstance(rss_feed_health_rows, list) else None,
            "rss_feed_count": len(rss_feed_rows) if isinstance(rss_feed_rows, list) else None,
            "feed_health_count": len(feed_health_rows) if isinstance(feed_health_rows, list) else None,
            "missing_fields": missing_rss_feed_health_fields,
            "missing_rss_feed_fields": missing_rss_feed_fields,
            "missing_feed_health_fields": missing_feed_health_fields,
        }
    )
    if (
        rss_feed_health_status != 200
        or not isinstance(rss_feed_health_rows, list)
        or not rss_feed_health_rows
        or not isinstance(rss_feed_rows, list)
        or not isinstance(feed_health_rows, list)
        or missing_rss_feed_health_fields
        or missing_rss_feed_fields
        or missing_feed_health_fields
    ):
        failures.append("load-admin-rss-feed-health")

    feed_management_status, feed_management_payload = request_json(
        base_url,
        token,
        "load-admin-feed-management",
        {
            "providerMode": "backend_postgres_primary",
            "limit": 20,
        },
    )
    feed_management_rows = (
        feed_management_payload.get("rows")
        if isinstance(feed_management_payload, dict)
        else None
    )
    feed_management_snapshot = (
        feed_management_rows[0]
        if isinstance(feed_management_rows, list) and feed_management_rows
        else {}
    )
    feed_quality_rows = (
        feed_management_snapshot.get("feedQualityRows")
        if isinstance(feed_management_snapshot, dict)
        else None
    )
    missing_feed_management_fields = sorted(
        field
        for field in {"feedQualityRows"}
        if not isinstance(feed_management_snapshot, dict) or field not in feed_management_snapshot
    )
    first_feed_quality = (
        feed_quality_rows[0]
        if isinstance(feed_quality_rows, list) and feed_quality_rows
        else {}
    )
    missing_feed_quality_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_feed_quality, dict)
        and first_feed_quality
        and field not in first_feed_quality
    )
    checks.append(
        {
            "operation": "load-admin-feed-management",
            "status": feed_management_status,
            "row_count": len(feed_management_rows) if isinstance(feed_management_rows, list) else None,
            "feed_quality_count": len(feed_quality_rows) if isinstance(feed_quality_rows, list) else None,
            "missing_fields": missing_feed_management_fields,
            "missing_feed_quality_fields": missing_feed_quality_fields,
        }
    )
    if (
        feed_management_status != 200
        or not isinstance(feed_management_rows, list)
        or not feed_management_rows
        or not isinstance(feed_quality_rows, list)
        or missing_feed_management_fields
        or missing_feed_quality_fields
    ):
        failures.append("load-admin-feed-management")

    audit_log_status, audit_log_payload = request_json(
        base_url,
        token,
        "load-admin-audit-log",
        {
            "providerMode": "backend_postgres_primary",
            "limit": 20,
        },
    )
    audit_log_rows = (
        audit_log_payload.get("rows")
        if isinstance(audit_log_payload, dict)
        else None
    )
    audit_log_snapshot = (
        audit_log_rows[0]
        if isinstance(audit_log_rows, list) and audit_log_rows
        else {}
    )
    audit_event_rows = (
        audit_log_snapshot.get("auditEventRows")
        if isinstance(audit_log_snapshot, dict)
        else None
    )
    missing_audit_log_fields = sorted(
        field
        for field in {"auditEventRows"}
        if not isinstance(audit_log_snapshot, dict) or field not in audit_log_snapshot
    )
    first_audit_event = (
        audit_event_rows[0]
        if isinstance(audit_event_rows, list) and audit_event_rows
        else {}
    )
    missing_audit_event_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_audit_event, dict)
        and first_audit_event
        and field not in first_audit_event
    )
    checks.append(
        {
            "operation": "load-admin-audit-log",
            "status": audit_log_status,
            "row_count": len(audit_log_rows) if isinstance(audit_log_rows, list) else None,
            "audit_event_count": len(audit_event_rows) if isinstance(audit_event_rows, list) else None,
            "missing_fields": missing_audit_log_fields,
            "missing_event_fields": missing_audit_event_fields,
        }
    )
    if (
        audit_log_status != 200
        or not isinstance(audit_log_rows, list)
        or not audit_log_rows
        or not isinstance(audit_event_rows, list)
        or missing_audit_log_fields
        or missing_audit_event_fields
    ):
        failures.append("load-admin-audit-log")

    article_reviews_status, article_reviews_payload = request_json(
        base_url,
        token,
        "load-admin-article-reviews",
        {
            "providerMode": "backend_postgres_primary",
            "filters": {
                "decision": "all",
                "source": "",
                "category": "",
                "minScore": None,
                "maxScore": None,
                "page": 0,
                "sort": "newest",
            },
            "pageSize": 5,
            "recentPublishedArticleLimit": 3,
            "aiDecisionVersionReportLimit": 3,
            "maxOptionRows": 100,
        },
    )
    article_reviews_rows = (
        article_reviews_payload.get("rows")
        if isinstance(article_reviews_payload, dict)
        else None
    )
    article_reviews_snapshot = (
        article_reviews_rows[0]
        if isinstance(article_reviews_rows, list) and article_reviews_rows
        else {}
    )
    required_article_review_fields = {
        "sourceOptions",
        "categoryOptions",
        "recentPublishedArticleRows",
        "recentPublishedReviewRows",
        "versionReportRows",
        "reviewRows",
        "publishedArticlesForReviews",
        "totalMatchingReviews",
    }
    missing_article_review_fields = sorted(
        field
        for field in required_article_review_fields
        if not isinstance(article_reviews_snapshot, dict) or field not in article_reviews_snapshot
    )
    checks.append(
        {
            "operation": "load-admin-article-reviews",
            "status": article_reviews_status,
            "row_count": len(article_reviews_rows) if isinstance(article_reviews_rows, list) else None,
            "missing_fields": missing_article_review_fields,
        }
    )
    if (
        article_reviews_status != 200
        or not isinstance(article_reviews_rows, list)
        or not article_reviews_rows
        or missing_article_review_fields
    ):
        failures.append("load-admin-article-reviews")

    engagement_status, engagement_payload = request_json(
        base_url,
        token,
        "load-admin-article-engagement",
        {
            "providerMode": "backend_postgres_primary",
            "sourceCategoryLimit": 10,
            "articleLimit": 5,
        },
    )
    engagement_rows = (
        engagement_payload.get("rows")
        if isinstance(engagement_payload, dict)
        else None
    )
    engagement_snapshot = (
        engagement_rows[0]
        if isinstance(engagement_rows, list) and engagement_rows
        else {}
    )
    missing_engagement_fields = sorted(
        field
        for field in {
            "sourceCategoryRows",
            "sourceCategoryError",
            "articleRows",
            "articleError",
        }
        if not isinstance(engagement_snapshot, dict) or field not in engagement_snapshot
    )
    checks.append(
        {
            "operation": "load-admin-article-engagement",
            "status": engagement_status,
            "row_count": len(engagement_rows) if isinstance(engagement_rows, list) else None,
            "missing_fields": missing_engagement_fields,
        }
    )
    if (
        engagement_status != 200
        or not isinstance(engagement_rows, list)
        or not engagement_rows
        or missing_engagement_fields
    ):
        failures.append("load-admin-article-engagement")

    ai_usage_since = (
        datetime.now(UTC)
        - timedelta(days=30)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ai_usage_status, ai_usage_payload = request_json(
        base_url,
        token,
        "load-admin-ai-usage",
        {
            "providerMode": "backend_postgres_primary",
            "since": ai_usage_since,
            "limit": 20,
        },
    )
    ai_usage_rows = (
        ai_usage_payload.get("rows")
        if isinstance(ai_usage_payload, dict)
        else None
    )
    ai_usage_snapshot = (
        ai_usage_rows[0]
        if isinstance(ai_usage_rows, list) and ai_usage_rows
        else {}
    )
    usage_run_rows = (
        ai_usage_snapshot.get("usageRunRows")
        if isinstance(ai_usage_snapshot, dict)
        else None
    )
    missing_ai_usage_fields = sorted(
        field
        for field in {"usageRunRows"}
        if not isinstance(ai_usage_snapshot, dict) or field not in ai_usage_snapshot
    )
    required_ai_usage_run_fields = {
        "run_started_at",
        "openai_model",
        "openai_call_count",
        "openai_total_tokens",
        "estimated_openai_cost_usd",
        "openai_review_count",
        "openai_translation_count",
        "local_ai_model",
        "local_ai_call_count",
        "local_ai_total_tokens",
        "estimated_local_ai_savings_usd",
        "duration_ms",
    }
    first_usage_run = (
        usage_run_rows[0]
        if isinstance(usage_run_rows, list) and usage_run_rows
        else {}
    )
    missing_ai_usage_run_fields = sorted(
        field
        for field in required_ai_usage_run_fields
        if isinstance(first_usage_run, dict)
        and first_usage_run
        and field not in first_usage_run
    )
    checks.append(
        {
            "operation": "load-admin-ai-usage",
            "status": ai_usage_status,
            "row_count": len(ai_usage_rows) if isinstance(ai_usage_rows, list) else None,
            "usage_run_count": len(usage_run_rows) if isinstance(usage_run_rows, list) else None,
            "missing_fields": missing_ai_usage_fields,
            "missing_run_fields": missing_ai_usage_run_fields,
        }
    )
    if (
        ai_usage_status != 200
        or not isinstance(ai_usage_rows, list)
        or not ai_usage_rows
        or not isinstance(usage_run_rows, list)
        or missing_ai_usage_fields
        or missing_ai_usage_run_fields
    ):
        failures.append("load-admin-ai-usage")

    local_ai_status, local_ai_payload = request_json(
        base_url,
        token,
        "load-admin-local-ai",
        {
            "providerMode": "backend_postgres_primary",
            "since": ai_usage_since,
            "runLimit": 20,
            "reviewLimit": 10,
        },
    )
    local_ai_rows = (
        local_ai_payload.get("rows")
        if isinstance(local_ai_payload, dict)
        else None
    )
    local_ai_snapshot = (
        local_ai_rows[0]
        if isinstance(local_ai_rows, list) and local_ai_rows
        else {}
    )
    local_ai_usage_run_rows = (
        local_ai_snapshot.get("usageRunRows")
        if isinstance(local_ai_snapshot, dict)
        else None
    )
    local_ai_review_rows = (
        local_ai_snapshot.get("recentReviewRows")
        if isinstance(local_ai_snapshot, dict)
        else None
    )
    missing_local_ai_fields = sorted(
        field
        for field in {"usageRunRows", "recentReviewRows"}
        if not isinstance(local_ai_snapshot, dict) or field not in local_ai_snapshot
    )
    first_local_ai_run = (
        local_ai_usage_run_rows[0]
        if isinstance(local_ai_usage_run_rows, list) and local_ai_usage_run_rows
        else {}
    )
    missing_local_ai_run_fields = sorted(
        field
        for field in {
            "run_started_at",
            "ai_provider",
            "local_ai_model",
            "local_ai_call_count",
            "local_ai_total_tokens",
            "local_ai_duration_ms",
            "openai_call_count",
            "ai_reviewed_count",
            "duration_ms",
        }
        if isinstance(first_local_ai_run, dict)
        and first_local_ai_run
        and field not in first_local_ai_run
    )
    first_local_ai_review = (
        local_ai_review_rows[0]
        if isinstance(local_ai_review_rows, list) and local_ai_review_rows
        else {}
    )
    missing_local_ai_review_fields = sorted(
        field
        for field in {
            "reviewed_at",
            "original_url",
            "decision",
            "ai_provider",
            "ai_model",
            "review_duration_ms",
        }
        if isinstance(first_local_ai_review, dict)
        and first_local_ai_review
        and field not in first_local_ai_review
    )
    checks.append(
        {
            "operation": "load-admin-local-ai",
            "status": local_ai_status,
            "row_count": len(local_ai_rows) if isinstance(local_ai_rows, list) else None,
            "usage_run_count": len(local_ai_usage_run_rows) if isinstance(local_ai_usage_run_rows, list) else None,
            "recent_review_count": len(local_ai_review_rows) if isinstance(local_ai_review_rows, list) else None,
            "missing_fields": missing_local_ai_fields,
            "missing_run_fields": missing_local_ai_run_fields,
            "missing_review_fields": missing_local_ai_review_fields,
        }
    )
    if (
        local_ai_status != 200
        or not isinstance(local_ai_rows, list)
        or not local_ai_rows
        or not isinstance(local_ai_usage_run_rows, list)
        or not isinstance(local_ai_review_rows, list)
        or missing_local_ai_fields
        or missing_local_ai_run_fields
        or missing_local_ai_review_fields
    ):
        failures.append("load-admin-local-ai")

    translation_quality_status, translation_quality_payload = request_json(
        base_url,
        token,
        "load-admin-translation-quality",
        {
            "providerMode": "backend_postgres_primary",
            "auditLimit": 10,
            "summaryLookupLimit": 100,
            "targetLanguageCodes": ["fr", "ja", "de-CH", "de", "el"],
        },
    )
    translation_quality_rows = (
        translation_quality_payload.get("rows")
        if isinstance(translation_quality_payload, dict)
        else None
    )
    translation_quality_snapshot = (
        translation_quality_rows[0]
        if isinstance(translation_quality_rows, list) and translation_quality_rows
        else {}
    )
    translation_article_rows = (
        translation_quality_snapshot.get("articleRows")
        if isinstance(translation_quality_snapshot, dict)
        else None
    )
    translation_summary_rows = (
        translation_quality_snapshot.get("summaryRows")
        if isinstance(translation_quality_snapshot, dict)
        else None
    )
    missing_translation_quality_fields = sorted(
        field
        for field in {"articleRows", "summaryRows"}
        if not isinstance(translation_quality_snapshot, dict) or field not in translation_quality_snapshot
    )
    first_translation_article = (
        translation_article_rows[0]
        if isinstance(translation_article_rows, list) and translation_article_rows
        else {}
    )
    missing_translation_article_fields = sorted(
        field
        for field in {
            "id",
            "source",
            "title",
            "original_url",
            "ai_summary",
            "category",
            "published_on_site_at",
            "snapshot_rank",
        }
        if isinstance(first_translation_article, dict)
        and first_translation_article
        and field not in first_translation_article
    )
    first_translation_summary = (
        translation_summary_rows[0]
        if isinstance(translation_summary_rows, list) and translation_summary_rows
        else {}
    )
    missing_translation_summary_fields = sorted(
        field
        for field in {
            "original_url",
            "language_code",
            "title",
            "summary",
            "updated_at",
            "generated_by",
            "model",
        }
        if isinstance(first_translation_summary, dict)
        and first_translation_summary
        and field not in first_translation_summary
    )
    checks.append(
        {
            "operation": "load-admin-translation-quality",
            "status": translation_quality_status,
            "row_count": len(translation_quality_rows) if isinstance(translation_quality_rows, list) else None,
            "article_count": len(translation_article_rows) if isinstance(translation_article_rows, list) else None,
            "summary_count": len(translation_summary_rows) if isinstance(translation_summary_rows, list) else None,
            "missing_fields": missing_translation_quality_fields,
            "missing_article_fields": missing_translation_article_fields,
            "missing_summary_fields": missing_translation_summary_fields,
        }
    )
    if (
        translation_quality_status != 200
        or not isinstance(translation_quality_rows, list)
        or not translation_quality_rows
        or not isinstance(translation_article_rows, list)
        or not isinstance(translation_summary_rows, list)
        or missing_translation_quality_fields
        or missing_translation_article_fields
        or missing_translation_summary_fields
    ):
        failures.append("load-admin-translation-quality")

    guardrails_status, guardrails_payload = request_json(
        base_url,
        token,
        "load-admin-guardrails",
        {
            "providerMode": "backend_postgres_primary",
            "since": ai_usage_since,
            "limit": 20,
            "countTables": ["articles", "article_summaries", "rss_feeds"],
        },
    )
    guardrails_rows = (
        guardrails_payload.get("rows")
        if isinstance(guardrails_payload, dict)
        else None
    )
    guardrails_snapshot = (
        guardrails_rows[0]
        if isinstance(guardrails_rows, list) and guardrails_rows
        else {}
    )
    guardrails_ai_rows = (
        guardrails_snapshot.get("aiUsageRunRows")
        if isinstance(guardrails_snapshot, dict)
        else None
    )
    guardrails_worker_rows = (
        guardrails_snapshot.get("workerRunRows")
        if isinstance(guardrails_snapshot, dict)
        else None
    )
    guardrails_quota_rows = (
        guardrails_snapshot.get("quotaUsageEventRows")
        if isinstance(guardrails_snapshot, dict)
        else None
    )
    missing_guardrails_fields = sorted(
        field
        for field in {
            "aiUsageRunRows",
            "workerRunRows",
            "quotaUsageEventRows",
            "articleCount",
            "summaryCount",
            "feedCount",
            "partialErrors",
        }
        if not isinstance(guardrails_snapshot, dict) or field not in guardrails_snapshot
    )
    first_guardrails_ai_row = (
        guardrails_ai_rows[0]
        if isinstance(guardrails_ai_rows, list) and guardrails_ai_rows
        else {}
    )
    missing_guardrails_ai_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_guardrails_ai_row, dict)
        and first_guardrails_ai_row
        and field not in first_guardrails_ai_row
    )
    first_guardrails_worker_row = (
        guardrails_worker_rows[0]
        if isinstance(guardrails_worker_rows, list) and guardrails_worker_rows
        else {}
    )
    missing_guardrails_worker_fields = sorted(
        field
        for field in {
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
        }
        if isinstance(first_guardrails_worker_row, dict)
        and first_guardrails_worker_row
        and field not in first_guardrails_worker_row
    )
    first_guardrails_quota_row = (
        guardrails_quota_rows[0]
        if isinstance(guardrails_quota_rows, list) and guardrails_quota_rows
        else {}
    )
    missing_guardrails_quota_fields = sorted(
        field
        for field in {"event_type", "quantity", "created_at"}
        if isinstance(first_guardrails_quota_row, dict)
        and first_guardrails_quota_row
        and field not in first_guardrails_quota_row
    )
    checks.append(
        {
            "operation": "load-admin-guardrails",
            "status": guardrails_status,
            "row_count": len(guardrails_rows) if isinstance(guardrails_rows, list) else None,
            "ai_usage_run_count": len(guardrails_ai_rows) if isinstance(guardrails_ai_rows, list) else None,
            "worker_run_count": len(guardrails_worker_rows) if isinstance(guardrails_worker_rows, list) else None,
            "quota_event_count": len(guardrails_quota_rows) if isinstance(guardrails_quota_rows, list) else None,
            "article_count": guardrails_snapshot.get("articleCount") if isinstance(guardrails_snapshot, dict) else None,
            "summary_count": guardrails_snapshot.get("summaryCount") if isinstance(guardrails_snapshot, dict) else None,
            "feed_count": guardrails_snapshot.get("feedCount") if isinstance(guardrails_snapshot, dict) else None,
            "partial_errors": guardrails_snapshot.get("partialErrors") if isinstance(guardrails_snapshot, dict) else None,
            "missing_fields": missing_guardrails_fields,
            "missing_ai_fields": missing_guardrails_ai_fields,
            "missing_worker_fields": missing_guardrails_worker_fields,
            "missing_quota_fields": missing_guardrails_quota_fields,
        }
    )
    if (
        guardrails_status != 200
        or not isinstance(guardrails_rows, list)
        or not guardrails_rows
        or not isinstance(guardrails_ai_rows, list)
        or not isinstance(guardrails_worker_rows, list)
        or not isinstance(guardrails_quota_rows, list)
        or missing_guardrails_fields
        or missing_guardrails_ai_fields
        or missing_guardrails_worker_fields
        or missing_guardrails_quota_fields
    ):
        failures.append("load-admin-guardrails")

    shadow_write_status, shadow_write_payload = request_json(
        base_url,
        token,
        "record-quota-usage-event",
        {
            "providerMode": "backend_postgres_shadow",
            "eventType": "backend_app_db_api_smoke",
            "eventSource": "backend",
        },
    )
    checks.append(
        {
            "operation": "record-quota-usage-event-shadow",
            "status": shadow_write_status,
            "payload": shadow_write_payload,
        }
    )
    if shadow_write_status != 409:
        failures.append("record-quota-usage-event-shadow")

    if writes_enabled:
        checks.append(
            {
                "operation": "record-quota-usage-event-primary-guarded",
                "status": "skipped",
                "reason": "backend primary writes are enabled",
            }
        )
    else:
        guarded_write_status, guarded_write_payload = request_json(
            base_url,
            token,
            "record-quota-usage-event",
            {
                "providerMode": "backend_postgres_primary",
                "eventType": "backend_app_db_api_smoke",
                "eventSource": "backend",
            },
        )
        checks.append(
            {
                "operation": "record-quota-usage-event-primary-guarded",
                "status": guarded_write_status,
                "payload": guarded_write_payload,
            }
        )
        if guarded_write_status != 403:
            failures.append("record-quota-usage-event-primary-guarded")

    report = {
        "status": "pass" if not failures else "fail",
        "base_url_env": args.base_url_env,
        "token_env": args.token_env,
        "supabase_required": False,
        "checks": checks,
        "failures": failures,
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
