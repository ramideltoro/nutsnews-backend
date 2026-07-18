#!/usr/bin/env python3
"""Validate or provision NutsNews New Relic dashboards through NerdGraph."""

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
DASHBOARD_DIR = ROOT / "docs" / "newrelic" / "dashboards"
ENDPOINTS = {
    "us": "https://api.newrelic.com/graphql",
    "eu": "https://api.eu.newrelic.com/graphql",
    "jp": "https://api.jp.newrelic.com/graphql",
}
FORBIDDEN_QUERY_TERMS = ("api_key", "license_key", "password", "secret", "token")


class ValidationError(ValueError):
    pass


def load_dashboard_files(directory: Path = DASHBOARD_DIR) -> list[dict[str, Any]]:
    dashboards = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path.relative_to(ROOT))
        dashboards.append(data)
    return dashboards


def validate_dashboard_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    path = spec.get("_path", "<memory>")
    dashboard = spec.get("dashboard")
    if not spec.get("slug"):
        errors.append(f"{path}: missing slug")
    if not isinstance(dashboard, dict):
        return errors + [f"{path}: missing dashboard object"]
    name = str(dashboard.get("name") or "")
    if not name.startswith("NutsNews Backend - "):
        errors.append(f"{path}: dashboard name must use NutsNews Backend prefix")
    if dashboard.get("permissions") != "PUBLIC_READ_ONLY":
        errors.append(f"{path}: permissions must be PUBLIC_READ_ONLY")
    pages = dashboard.get("pages")
    if not isinstance(pages, list) or not pages:
        errors.append(f"{path}: dashboard requires at least one page")
        return errors
    for page_index, page in enumerate(pages, start=1):
        widgets = page.get("widgets") if isinstance(page, dict) else None
        if not isinstance(widgets, list) or not widgets:
            errors.append(f"{path}: page {page_index} requires widgets")
            continue
        for widget_index, widget in enumerate(widgets, start=1):
            title = widget.get("title")
            if not title:
                errors.append(f"{path}: page {page_index} widget {widget_index} missing title")
            layout = widget.get("layout") or {}
            try:
                column = int(layout["column"])
                width = int(layout["width"])
                row = int(layout["row"])
                height = int(layout["height"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"{path}: widget {title or widget_index} has invalid layout")
                continue
            if column < 1 or row < 1 or width < 1 or height < 1 or column + width - 1 > 12:
                errors.append(f"{path}: widget {title or widget_index} layout exceeds 12 columns")
            raw = widget.get("rawConfiguration") or {}
            raw_text = json.dumps(raw, sort_keys=True).lower()
            for term in FORBIDDEN_QUERY_TERMS:
                if term in raw_text:
                    errors.append(f"{path}: widget {title or widget_index} contains forbidden term {term}")
            if widget.get("visualization", {}).get("id") != "viz.markdown" and "since 24 hours ago" not in raw_text:
                errors.append(f"{path}: widget {title or widget_index} must include SINCE 24 hours ago")
    return errors


def validate_catalog(dashboards: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_slugs: set[str] = set()
    for spec in dashboards:
        errors.extend(validate_dashboard_spec(spec))
        dashboard = spec.get("dashboard") or {}
        name = str(dashboard.get("name") or "")
        slug = str(spec.get("slug") or "")
        if name in seen_names:
            errors.append(f"{spec.get('_path')}: duplicate dashboard name {name}")
        if slug in seen_slugs:
            errors.append(f"{spec.get('_path')}: duplicate dashboard slug {slug}")
        seen_names.add(name)
        seen_slugs.add(slug)
    if errors:
        raise ValidationError("\n".join(errors))


def render_account_id(value: Any, account_id: int) -> Any:
    if value == "{{ACCOUNT_ID}}":
        return account_id
    if isinstance(value, list):
        return [render_account_id(item, account_id) for item in value]
    if isinstance(value, dict):
        return {key: render_account_id(item, account_id) for key, item in value.items()}
    return value


def graphql(endpoint: str, user_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Api-Key": user_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"NerdGraph HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("NerdGraph request failed") from exc
    data = json.loads(body)
    if data.get("errors"):
        raise RuntimeError("NerdGraph returned errors")
    return data.get("data") or {}


def find_dashboard_guid(endpoint: str, user_key: str, dashboard_name: str) -> str | None:
    escaped_name = dashboard_name.replace("'", "\\'")
    query = """
    query($query: String!) {
      actor {
        entitySearch(query: $query) {
          results {
            entities {
              guid
              name
              type
              domain
            }
          }
        }
      }
    }
    """
    data = graphql(endpoint, user_key, query, {"query": f"name = '{escaped_name}' AND type = 'DASHBOARD'"})
    entities = data.get("actor", {}).get("entitySearch", {}).get("results", {}).get("entities", [])
    for entity in entities:
        if entity.get("name") == dashboard_name:
            return str(entity.get("guid"))
    return None


def create_dashboard(endpoint: str, user_key: str, account_id: int, dashboard: dict[str, Any]) -> str:
    query = """
    mutation($accountId: Int!, $dashboard: DashboardInput!) {
      dashboardCreate(accountId: $accountId, dashboard: $dashboard) {
        entityResult {
          guid
          name
        }
        errors {
          type
          description
        }
      }
    }
    """
    data = graphql(endpoint, user_key, query, {"accountId": account_id, "dashboard": dashboard})
    result = data.get("dashboardCreate", {})
    if result.get("errors"):
        raise RuntimeError("dashboardCreate returned errors")
    return str(result.get("entityResult", {}).get("guid") or "")


def update_dashboard(endpoint: str, user_key: str, guid: str, dashboard: dict[str, Any]) -> str:
    query = """
    mutation($guid: EntityGuid!, $dashboard: DashboardInput!) {
      dashboardUpdate(guid: $guid, dashboard: $dashboard) {
        entityResult {
          guid
          name
        }
        errors {
          type
          description
        }
      }
    }
    """
    data = graphql(endpoint, user_key, query, {"guid": guid, "dashboard": dashboard})
    result = data.get("dashboardUpdate", {})
    if result.get("errors"):
        raise RuntimeError("dashboardUpdate returned errors")
    return str(result.get("entityResult", {}).get("guid") or guid)


def env_config() -> tuple[str, str, int]:
    missing = [name for name in ("NEW_RELIC_USER_KEY", "NEW_RELIC_ACCOUNT_ID") if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required New Relic environment variables: " + ", ".join(missing))
    region = os.environ.get("NEW_RELIC_REGION", "us").strip().lower() or "us"
    if region not in ENDPOINTS:
        raise RuntimeError("NEW_RELIC_REGION must be one of: " + ", ".join(sorted(ENDPOINTS)))
    try:
        account_id = int(os.environ["NEW_RELIC_ACCOUNT_ID"])
    except ValueError as exc:
        raise RuntimeError("NEW_RELIC_ACCOUNT_ID must be an integer") from exc
    return ENDPOINTS[region], os.environ["NEW_RELIC_USER_KEY"], account_id


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate definitions without calling NerdGraph.")
    args = parser.parse_args(argv)

    dashboards = load_dashboard_files()
    try:
        validate_catalog(dashboards)
        if args.check:
            print(json.dumps({"status": "pass", "dashboard_count": len(dashboards), "safe_metadata_only": True}, sort_keys=True))
            return 0
        endpoint, user_key, account_id = env_config()
        results = []
        for spec in dashboards:
            dashboard = render_account_id(spec["dashboard"], account_id)
            name = dashboard["name"]
            guid = find_dashboard_guid(endpoint, user_key, name)
            if guid:
                final_guid = update_dashboard(endpoint, user_key, guid, dashboard)
                action = "updated"
            else:
                final_guid = create_dashboard(endpoint, user_key, account_id, dashboard)
                action = "created"
            results.append(
                {
                    "slug": spec["slug"],
                    "name": name,
                    "action": action,
                    "guid": final_guid,
                    "url": f"https://one.newrelic.com/redirect/entity/{final_guid}",
                }
            )
        print(json.dumps({"status": "pass", "dashboards": results, "safe_metadata_only": True}, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValidationError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc), "safe_metadata_only": True}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main_args())
