#!/usr/bin/env python3
"""Smoke test the backend app database compatibility API."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
