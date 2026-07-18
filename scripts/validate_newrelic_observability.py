#!/usr/bin/env python3
"""Validate New Relic taxonomy, dashboard catalog, and runbook guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from scripts.provision_newrelic_dashboards import load_dashboard_files, validate_catalog
except ModuleNotFoundError:  # pragma: no cover
    from provision_newrelic_dashboards import load_dashboard_files, validate_catalog


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "docs" / "newrelic-observability-taxonomy.json"
RUNBOOK = ROOT / "runbooks" / "NEW_RELIC_OBSERVABILITY.md"
LOG_POLICY = ROOT / "docs" / "newrelic-log-policy.json"
SERVICE_LEVELS = ROOT / "docs" / "newrelic-service-levels.json"
CACHE_QUEUE_DECISION = ROOT / "docs" / "newrelic-cache-queue-decision.json"
PRIVACY_REVIEW = ROOT / "docs" / "newrelic-telemetry-privacy-review.json"
DASHBOARD_UX = ROOT / "docs" / "newrelic-dashboard-ux.json"
DEMO_RUNBOOK = ROOT / "runbooks" / "NEW_RELIC_OBSERVABILITY_DEMO.md"


def main() -> int:
    errors: list[str] = []
    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    service = taxonomy.get("service", {})
    if service.get("canonical_name") != "nutsnews-backend-production":
        errors.append("taxonomy canonical service name must be nutsnews-backend-production")
    if taxonomy.get("dashboard_naming", {}).get("prefix") != "NutsNews Backend - ":
        errors.append("dashboard naming prefix mismatch")
    required_tags = taxonomy.get("standard_tags", {})
    for tag in ("service.name", "environment", "owner", "team", "repository", "host.name"):
        if not required_tags.get(tag):
            errors.append(f"missing standard tag: {tag}")
    for entity in taxonomy.get("entities", []):
        tags = entity.get("required_tags", {})
        if tags.get("service.name") != "nutsnews-backend-production":
            errors.append(f"entity {entity.get('name')} missing canonical service tag")
        if not tags.get("owner"):
            errors.append(f"entity {entity.get('name')} missing owner tag")
    try:
        validate_catalog(load_dashboard_files())
    except ValueError as exc:
        errors.append(str(exc))
    log_policy = json.loads(LOG_POLICY.read_text(encoding="utf-8"))
    required_fields = {field["name"] for field in log_policy.get("required_fields", [])}
    for field in (
        "timestamp",
        "level",
        "service.name",
        "environment",
        "request.id",
        "trace.id",
        "route",
        "http.statusCode",
        "duration.ms",
        "deployment.version",
        "exception.class",
        "message.safe",
    ):
        if field not in required_fields:
            errors.append(f"log policy missing required field: {field}")
    if not log_policy.get("redaction_rules"):
        errors.append("log policy must define redaction rules")
    if not log_policy.get("drop_rules"):
        errors.append("log policy must define drop rules")
    if not log_policy.get("daily_ingest_estimate_mb"):
        errors.append("log policy must define daily ingest estimate")
    service_levels = json.loads(SERVICE_LEVELS.read_text(encoding="utf-8"))
    if service_levels.get("service") != "nutsnews-backend-production":
        errors.append("service levels must use nutsnews-backend-production")
    if service_levels.get("apdex", {}).get("target_seconds") != 0.5:
        errors.append("service levels must define 0.5 second Apdex target")
    sli_ids = {sli.get("id") for sli in service_levels.get("service_levels", [])}
    for sli_id in ("availability", "latency", "error_free", "freshness"):
        if sli_id not in sli_ids:
            errors.append(f"service levels missing SLI: {sli_id}")
    for sli in service_levels.get("service_levels", []):
        if not sli.get("target") or not sli.get("nrql"):
            errors.append(f"service level {sli.get('id')} missing target or NRQL")
    cache_queue = json.loads(CACHE_QUEUE_DECISION.read_text(encoding="utf-8"))
    if cache_queue.get("decision") != "no_cache_or_queue_dashboard_now":
        errors.append("cache/queue decision must avoid placeholder dashboards")
    if cache_queue.get("active_cache_or_queue_workloads") != []:
        errors.append("cache/queue decision must reflect no active backend-owned workloads")
    privacy = json.loads(PRIVACY_REVIEW.read_text(encoding="utf-8"))
    if not privacy.get("allowlist") or not privacy.get("denylist"):
        errors.append("privacy review must define allowlist and denylist")
    for scope in ("logs", "apm_attributes", "custom_events", "synthetics"):
        if scope not in privacy.get("coverage", {}):
            errors.append(f"privacy review missing coverage: {scope}")
    dashboard_ux = json.loads(DASHBOARD_UX.read_text(encoding="utf-8"))
    dashboard_slugs = {dashboard.get("slug") for dashboard in load_dashboard_files()}
    ux_slugs = {dashboard.get("slug") for dashboard in dashboard_ux.get("dashboards", [])}
    missing_ux = sorted(dashboard_slugs - ux_slugs)
    if missing_ux:
        errors.append(f"dashboard UX map missing slugs: {missing_ux}")
    variable_names = {variable.get("name") for variable in dashboard_ux.get("variables", [])}
    for variable in ("environment", "host", "transaction", "status_code", "deployment_version"):
        if variable not in variable_names:
            errors.append(f"dashboard UX missing variable: {variable}")
    demo = DEMO_RUNBOOK.read_text(encoding="utf-8")
    for text in ("Latency Walkthrough", "Error Walkthrough", "Database Walkthrough", "Production Readiness Checklist", "Known Gaps"):
        if text not in demo:
            errors.append(f"demo runbook missing {text}")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for text in (
        "NEW_RELIC_LICENSE_KEY",
        "NEW_RELIC_USER_KEY",
        "NEW_RELIC_ACCOUNT_ID",
        "NEW_RELIC_REGION",
        "NEW_RELIC_APP_NAME",
        "scripts/provision_newrelic_dashboards.py --check",
        "scripts/backend_newrelic_observability_check.py --offline",
        "docs/newrelic-log-policy.json",
        "docs/newrelic-service-levels.json",
        "docs/newrelic-cache-queue-decision.json",
        "docs/newrelic-telemetry-privacy-review.json",
        "docs/newrelic-dashboard-ux.json",
        "runbooks/NEW_RELIC_OBSERVABILITY_DEMO.md",
    ):
        if text not in runbook:
            errors.append(f"runbook missing {text}")
    if errors:
        print(json.dumps({"status": "fail", "errors": errors, "safe_metadata_only": True}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "safe_metadata_only": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
