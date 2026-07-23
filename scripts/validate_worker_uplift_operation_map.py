#!/usr/bin/env python3
"""Validate worker-uplift operation ownership mapping."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "worker-uplift-operation-map.json"
RUNBOOK_PATH = ROOT / "runbooks" / "WORKER_UPLIFT_OPERATION_MAP.md"

REQUIRED_IDS = {
    "legacy-worker-shard-deploy",
    "legacy-worker-smoke-postdeploy",
    "legacy-worker-shadow-smoke",
    "legacy-worker-feed-health",
    "legacy-worker-translation-audit",
    "legacy-worker-backpressure-locks",
    "legacy-worker-database-provider",
    "legacy-worker-controller-failover",
    "legacy-worker-supabase-backup-restore",
    "backend-host-deploy",
    "backend-host-restart",
    "backend-service-scale-drain",
    "backend-status-health",
    "backend-logs",
    "backend-queue-dlq-inspect-replay",
    "backend-broker-backup-restore",
    "backend-reconciliation",
    "backend-smoke-health",
    "backend-postgres-backup-restore",
    "backend-cloudflare-routing",
    "backend-credential-readiness",
    "infra-grafana-plan-drift",
    "infra-grafana-apply-verify",
    "backend-grafana-validation-only",
}

REQUIRED_LEGACY_SOURCE_MARKERS = {
    "worker-pipeline.yml",
    "deploy_worker_shards.mjs",
    "worker-smoke-test.yml",
    "worker-shadow-smoke.yml",
    "feed_health_report.mjs",
    "audit_article_translations.mjs",
    "assert_worker_backpressure_locks.mjs",
    "WORKER_DATABASE_PROVIDER_MODES.md",
    "FAILOVER_ALERTS.md",
    "FAILOVER_ANALYTICS_ENGINE.md",
    "post_deploy_verify.sh",
    "supabase_backup.mjs",
    "validate_supabase_restore.sh",
}

REQUIRED_BACKEND_OPERATIONS = {
    "deploy",
    "restart",
    "scale",
    "status",
    "logs",
    "queue",
    "DLQ",
    "drain",
    "broker backup",
    "restore",
    "reconciliation",
    "smoke",
    "health",
}

VALID_BOUNDARIES = {
    "read_only",
    "protected_backend_environment",
    "protected_infra_environment",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def strings(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def main() -> int:
    errors: list[str] = []
    data = load_json(MAP_PATH)
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    entries = data.get("operation_entries", [])

    if data.get("version") != 1:
        errors.append("operation map version must be 1")
    if data.get("tracking_issue") != 70:
        errors.append("tracking_issue must be 70")
    if data.get("implementation_repo") != "ramideltoro/nutsnews-backend":
        errors.append("implementation repo must be nutsnews-backend")
    if data.get("tracking_repo") != "ramideltoro/nutsnews-worker":
        errors.append("tracking repo must be nutsnews-worker")
    if not data.get("guardrail", "").startswith("This map assigns"):
        errors.append("guardrail must state this map is non-mutating")
    if "ramideltoro/nutsnews-docs/NUTSNEWS_WORKER_UPLIFT_OPERATION_MAP.md" not in data.get("shared_docs", []):
        errors.append("shared docs link is missing from operation map")

    if not isinstance(entries, list) or not entries:
        errors.append("operation_entries must be a non-empty list")
        entries = []

    ids = [entry.get("id") for entry in entries]
    if len(ids) != len(set(ids)):
        errors.append("operation entry ids must be unique")

    missing = REQUIRED_IDS - set(ids)
    if missing:
        errors.append(f"missing required operation ids: {', '.join(sorted(missing))}")

    all_text = strings(data)
    for marker in REQUIRED_LEGACY_SOURCE_MARKERS:
        if marker not in all_text:
            errors.append(f"legacy source marker missing: {marker}")
    for marker in REQUIRED_BACKEND_OPERATIONS:
        if marker not in all_text and marker not in runbook:
            errors.append(f"backend operation marker missing: {marker}")

    failover_entries = []
    grafana_entries = []

    for entry in entries:
        entry_id = entry.get("id", "<missing>")
        destination = entry.get("destination", {})
        owner_repo = destination.get("owner_repo", "")
        owner_files = destination.get("owner_files", [])
        workflow = destination.get("workflow", "")
        boundary = entry.get("mutation_boundary", "")
        classification = entry.get("classification", "")

        for field in ("operation", "source_scope", "approval_required", "rollback"):
            if not entry.get(field):
                errors.append(f"{entry_id} missing {field}")
        if not owner_repo:
            errors.append(f"{entry_id} missing destination.owner_repo")
        if not owner_files:
            errors.append(f"{entry_id} missing destination.owner_files")
        if not workflow:
            errors.append(f"{entry_id} missing destination.workflow")
        if boundary not in VALID_BOUNDARIES:
            errors.append(f"{entry_id} has invalid mutation boundary: {boundary}")
        if entry.get("runtime_coupling_to_legacy_worker") is not False:
            errors.append(f"{entry_id} must not couple runtime to legacy worker")
        if entry.get("new_deployment_requires_worker_checkout") is not False:
            errors.append(f"{entry_id} must not require nutsnews-worker checkout for new deployments")

        if owner_repo == "ramideltoro/nutsnews-worker" and not classification.startswith("intentional_retirement"):
            errors.append(f"{entry_id} assigns an active new owner to nutsnews-worker")
        if entry.get("source_scope") == "legacy_worker" and not entry.get("legacy_sources"):
            errors.append(f"{entry_id} is a legacy worker entry without legacy_sources")

        text = strings(entry)
        if "failover" in text.lower():
            failover_entries.append(entry)
        if "grafana" in text.lower():
            grafana_entries.append(entry)

    if not any(entry.get("failover_separate") is True for entry in failover_entries):
        errors.append("DNS failover must have a failover_separate=true entry")
    for entry in failover_entries:
        text = strings(entry).lower()
        if "ingestion-only" in text or "ingestion_only" in text:
            errors.append(f"{entry.get('id')} must not classify failover as ingestion-only")

    if not any(entry.get("destination", {}).get("owner_repo") == "ramideltoro/nutsnews-infra" for entry in grafana_entries):
        errors.append("Grafana resource operations must have nutsnews-infra owner entries")
    for entry in grafana_entries:
        text = strings(entry).lower()
        if "resource" in text and "ramideltoro/nutsnews-backend" == entry.get("destination", {}).get("owner_repo"):
            if entry.get("id") not in {"backend-grafana-validation-only", "backend-logs"}:
                errors.append(f"{entry.get('id')} gives backend a Grafana resource operation")

    for phrase in (
        "docs/worker-uplift-operation-map.json",
        "NUTSNEWS_WORKER_UPLIFT_OPERATION_MAP.md",
        "must not require a checkout of `ramideltoro/nutsnews-worker`",
        "DNS failover is not ingestion",
        "Grafana resource ownership is centralized in `ramideltoro/nutsnews-infra`",
    ):
        if phrase not in runbook:
            errors.append(f"runbook missing phrase: {phrase}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker uplift operation map is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
