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
