#!/usr/bin/env python3
"""Validate, provision, and verify NutsNews backend Grafana metrics dashboards."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "grafana" / "backend-metrics" / "dashboards.json"


def load_spec(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    folder = spec.get("folder", {})
    dashboards = spec.get("dashboards", [])
    guardrails = spec.get("guardrails", {})
    if not folder.get("uid") or not folder.get("title"):
        errors.append("folder.uid and folder.title are required")
    if not dashboards:
        errors.append("at least one dashboard is required")
    if len(dashboards) > int(guardrails.get("max_dashboards", 10)):
        errors.append("dashboard count exceeds guardrail")

    total_panels = 0
    queries: set[str] = set()
    seen_uids: set[str] = set()
    for dashboard in dashboards:
        uid = str(dashboard.get("uid", ""))
        title = str(dashboard.get("title", ""))
        panels = dashboard.get("panels", [])
        if not uid or not title:
            errors.append("each dashboard requires uid and title")
        if uid in seen_uids:
            errors.append(f"duplicate dashboard uid: {uid}")
        seen_uids.add(uid)
        if len(panels) > int(guardrails.get("max_panels_per_dashboard", 12)):
            errors.append(f"{uid} exceeds max panels per dashboard")
        total_panels += len(panels)
        for panel in panels:
            expr = str(panel.get("expr", "")).strip()
            if not panel.get("title") or not expr:
                errors.append(f"{uid} contains a panel without title or expr")
            if "$" in expr:
                errors.append(f"{uid} contains an unrendered variable in expression")
            queries.add(expr)
    if total_panels > int(guardrails.get("max_total_panels", 80)):
        errors.append("total panel count exceeds guardrail")
    if len(queries) > int(guardrails.get("max_unique_queries", 100)):
        errors.append("unique query count exceeds guardrail")
    return errors


class GrafanaClient:
    def __init__(self, url: str, token: str, timeout: int = 20) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Grafana API {method} {path} failed with {exc.code}: {detail}") from exc
        return json.loads(raw) if raw else {}


def choose_prometheus_datasource(client: GrafanaClient) -> dict[str, Any]:
    datasources = client.request("GET", "/api/datasources")
    prometheus = [item for item in datasources if item.get("type") == "prometheus" and item.get("uid")]
    if not prometheus:
        raise RuntimeError("no Prometheus datasource found in Grafana")
    default = next((item for item in prometheus if item.get("isDefault")), None)
    return default or prometheus[0]


def ensure_folder(client: GrafanaClient, folder: dict[str, str], apply: bool) -> dict[str, Any]:
    uid = folder["uid"]
    try:
        existing = client.request("GET", f"/api/folders/{urllib.parse.quote(uid)}")
    except RuntimeError as exc:
        if "failed with 404" not in str(exc):
            raise
        if not apply:
            return {"uid": uid, "status": "missing"}
        return client.request("POST", "/api/folders", {"uid": uid, "title": folder["title"]})
    if apply and existing.get("title") != folder["title"]:
        return client.request("PUT", f"/api/folders/{urllib.parse.quote(uid)}", {"title": folder["title"]})
    return existing


def panel_model(panel: dict[str, Any], panel_id: int, datasource: dict[str, str], x: int, y: int) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": panel["title"],
        "datasource": datasource,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {"unit": panel.get("unit", "short")},
            "overrides": [],
        },
        "targets": [
            {
                "datasource": datasource,
                "expr": panel["expr"],
                "legendFormat": "{{unit}}{{stage}}{{device}}{{name}}{{__name__}}",
                "refId": "A",
            }
        ],
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}},
    }


def dashboard_model(spec: dict[str, Any], datasource_uid: str) -> dict[str, Any]:
    datasource = {"type": "prometheus", "uid": datasource_uid}
    panels = []
    for idx, panel in enumerate(spec["panels"], start=1):
        x = 0 if idx % 2 == 1 else 12
        y = ((idx - 1) // 2) * 8
        panels.append(panel_model(panel, idx, datasource, x, y))
    return {
        "uid": spec["uid"],
        "title": spec["title"],
        "tags": ["nutsnews", "backend", "metrics"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 1,
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }


def upsert_dashboards(client: GrafanaClient, spec: dict[str, Any], datasource_uid: str, apply: bool) -> list[dict[str, str]]:
    folder_uid = spec["folder"]["uid"]
    results = []
    for dashboard in spec["dashboards"]:
        model = dashboard_model(dashboard, datasource_uid)
        if apply:
            response = client.request(
                "POST",
                "/api/dashboards/db",
                {"dashboard": model, "folderUid": folder_uid, "overwrite": True, "message": "Managed by ramideltoro/nutsnews-backend"},
            )
            results.append({"uid": dashboard["uid"], "title": dashboard["title"], "status": response.get("status", "updated")})
        else:
            results.append({"uid": dashboard["uid"], "title": dashboard["title"], "status": "validated"})
    return results


def verify_query(client: GrafanaClient, datasource_uid: str, query: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"query": query})
    response = client.request("GET", f"/api/datasources/proxy/uid/{urllib.parse.quote(datasource_uid)}/api/v1/query?{encoded}")
    data = response.get("data", {})
    result = data.get("result", []) if isinstance(data, dict) else []
    return {
        "query": query,
        "status": response.get("status", "unknown"),
        "result_count": len(result) if isinstance(result, list) else 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = load_spec(args.spec)
    errors = validate_spec(spec)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    report: dict[str, Any] = {
        "spec": str(args.spec),
        "folder": spec["folder"],
        "guardrails": spec.get("guardrails", {}),
        "dashboard_count": len(spec.get("dashboards", [])),
        "panel_count": sum(len(item.get("panels", [])) for item in spec.get("dashboards", [])),
        "status": "validated",
    }

    if args.check and not (args.apply or args.verify):
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.output:
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0

    url = os.environ.get("GRAFANA_URL", "").strip()
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not url or not token:
        print("GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN are required for apply/verify", file=sys.stderr)
        return 1

    if args.wait_seconds:
        time.sleep(args.wait_seconds)

    client = GrafanaClient(url, token)
    report["grafana_health"] = client.request("GET", "/api/health")
    datasource = choose_prometheus_datasource(client)
    datasource_uid = datasource["uid"]
    report["datasource"] = {"uid": datasource_uid, "name": datasource.get("name"), "type": datasource.get("type")}
    report["folder_result"] = ensure_folder(client, spec["folder"], args.apply)
    report["dashboards"] = upsert_dashboards(client, spec, datasource_uid, args.apply)

    if args.verify:
        report["verification"] = [
            verify_query(client, datasource_uid, 'up{job="nutsnews-backend-host"}'),
            verify_query(client, datasource_uid, 'nutsnews_backend_backup_stage_healthy{job="nutsnews-backend-host",stage="backup"}'),
            verify_query(client, datasource_uid, 'nutsnews_backend_public_endpoint_healthy{job="nutsnews-backend-host"}'),
        ]
        if any(item["result_count"] < 1 for item in report["verification"]):
            report["status"] = "missing_metrics"
        else:
            report["status"] = "verified"
    elif args.apply:
        report["status"] = "applied"

    print(json.dumps(report, indent=2, sort_keys=True))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["status"] != "missing_metrics" else 2


if __name__ == "__main__":
    raise SystemExit(main())
