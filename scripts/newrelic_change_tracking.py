#!/usr/bin/env python3
"""Create or validate New Relic change-tracking deployment markers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "docs" / "newrelic-live-configuration.json"
ENDPOINTS = {
    "us": "https://api.newrelic.com/graphql",
    "eu": "https://api.eu.newrelic.com/graphql",
    "jp": "https://api.jp.newrelic.com/graphql",
}


def redact(text: str) -> str:
    redacted = text
    for name, value in os.environ.items():
        if any(term in name.upper() for term in ("KEY", "PASSWORD", "TOKEN", "SECRET")) and len(value) >= 8:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def build_deployment_payload() -> tuple[dict[str, Any], list[str]]:
    environment = os.environ.get("NEW_RELIC_DEPLOY_ENVIRONMENT") or os.environ.get("NUTSNEWS_BACKEND_ENVIRONMENT", "production")
    version = os.environ.get("NEW_RELIC_DEPLOY_VERSION") or os.environ.get("GITHUB_SHA", "")
    branch = os.environ.get("NEW_RELIC_DEPLOY_BRANCH") or os.environ.get("GITHUB_REF_NAME", "")
    changelog = os.environ.get("NEW_RELIC_DEPLOY_CHANGELOG", "")
    commit = os.environ.get("GITHUB_SHA", "")
    payload = {
        "categoryAndTypeData": {
            "kind": {"category": "deployment", "type": "basic"},
            "categoryFields": {"deployment": {"version": version, "changelog": changelog, "commit": commit}},
        },
        "entitySearch": {"query": f"id = '{os.environ.get('NEW_RELIC_ENTITY_GUID', '').strip()}'"},
        "shortDescription": f"nutsnews-backend {version[:12]}",
        "description": os.environ.get("NEW_RELIC_DEPLOY_DESCRIPTION") or branch,
        "user": os.environ.get("NEW_RELIC_DEPLOY_USER") or os.environ.get("GITHUB_ACTOR", ""),
        "groupId": f"nutsnews-backend-{environment}",
        "customAttributes": {
            "deployBranch": branch,
            "deployEnvironment": environment,
            "repository": "ramideltoro/nutsnews-backend",
        },
    }
    missing = [name for name, value in (("NEW_RELIC_ENTITY_GUID", os.environ.get("NEW_RELIC_ENTITY_GUID", "").strip()), ("version", version)) if not value]
    return payload, missing


def env_config() -> tuple[str, str]:
    missing = [name for name in ("NEW_RELIC_USER_KEY",) if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required New Relic environment variables: " + ", ".join(missing))
    region = os.environ.get("NEW_RELIC_REGION", "us").strip().lower() or "us"
    if region not in ENDPOINTS:
        raise RuntimeError("NEW_RELIC_REGION must be one of: " + ", ".join(sorted(ENDPOINTS)))
    return ENDPOINTS[region], os.environ["NEW_RELIC_USER_KEY"]


def graphql(endpoint: str, user_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Api-Key": user_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"NerdGraph HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("NerdGraph request failed") from exc
    data = json.loads(body)
    if data.get("errors"):
        raise RuntimeError("NerdGraph returned errors")
    return data.get("data") or {}


def create_deployment(endpoint: str, user_key: str, deployment: dict[str, Any]) -> dict[str, Any]:
    mutation = """
    mutation($event: ChangeTrackingEventInput!) {
      changeTrackingCreateEvent(changeTrackingEvent: $event) {
        changeTrackingEvent {
          changeTrackingId
          category
          categoryAndType
          description
          entity {
            guid
            name
          }
          groupId
          shortDescription
          type
        }
        messages
      }
    }
    """
    data = graphql(endpoint, user_key, mutation, {"event": deployment})
    result = data.get("changeTrackingCreateEvent", {})
    if result.get("messages"):
        raise RuntimeError("changeTrackingCreateEvent returned messages")
    return result


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate marker config and payload without calling NerdGraph.")
    parser.add_argument("--skip-without-credentials", action="store_true", help="Exit successfully when live credentials are unavailable.")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        markers = config.get("deployment_markers", {})
        if markers.get("fail_safe") is not True:
            raise RuntimeError("deployment markers must be fail-safe")
        payload, payload_missing = build_deployment_payload()
        report = {
            "status": "pass",
            "safe_metadata_only": True,
            "script": markers.get("script"),
            "required_fields": markers.get("required_fields", []),
            "payload_missing": payload_missing,
        }
        if args.check:
            print(json.dumps(report, sort_keys=True))
            return 0
        credential_missing = [name for name in ("NEW_RELIC_USER_KEY", "NEW_RELIC_ENTITY_GUID") if not os.environ.get(name, "").strip()]
        if payload_missing or credential_missing:
            missing = sorted(set(payload_missing + credential_missing))
            if args.skip_without_credentials:
                print(json.dumps({"status": "skipped_with_reason", "reason": "missing_new_relic_change_tracking_inputs", "missing": missing, "safe_metadata_only": True}, sort_keys=True))
                return 0
            raise RuntimeError("Missing required New Relic change-tracking inputs: " + ", ".join(missing))
        endpoint, user_key = env_config()
        result = create_deployment(endpoint, user_key, payload)
        print(json.dumps({"status": "pass", "deployment": result, "safe_metadata_only": True}, sort_keys=True))
        return 0
    except RuntimeError as exc:
        print(json.dumps({"status": "fail", "error": redact(str(exc)), "safe_metadata_only": True}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main_args())
