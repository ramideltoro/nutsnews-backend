#!/usr/bin/env python3
"""Validate the backend PostgreSQL primary shadow monitoring plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-primary-shadow-monitoring-plan.json"
REQUIRED_METRICS = {
    "nutsnews_backend_postgres_replication_subscription_count",
    "nutsnews_backend_postgres_replication_blocker_count",
    "nutsnews_backend_postgres_replication_max_lag_seconds",
}
REQUIRED_ALERTS = {
    "subscription_missing_or_inactive",
    "replication_lag_exceeds_threshold",
    "source_slot_missing_or_inactive",
    "backup_restore_proof_stale_or_failed",
    "shadow_parity_failed",
}
FORBIDDEN_MARKERS = ("postgres://", "postgresql://", "password=", "token=", "secret=", "service_role=", "supabase.co")


def main() -> int:
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing monitoring plan: {PLAN_PATH}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid monitoring plan JSON: {exc}") from exc

    errors: list[str] = []
    if plan.get("plan_id") != "backend-postgres-primary-shadow-monitoring":
        errors.append("plan_id must be backend-postgres-primary-shadow-monitoring")
    if plan.get("issue") != 217:
        errors.append("issue must be 217")
    if plan.get("tracking_issue") != 120:
        errors.append("tracking_issue must be 120")
    if set(plan.get("depends_on_issues", [])) != {213, 214, 216}:
        errors.append("monitoring plan must depend on issues 213, 214, and 216")

    target = plan.get("target", {})
    if target.get("database") != "nutsnews_primary_shadow":
        errors.append("target database must be nutsnews_primary_shadow")
    if target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("target network path must use SSH tunnel to loopback PostgreSQL")
    if target.get("public_5432_allowed") is not False:
        errors.append("target must forbid public 5432")

    ingestion = plan.get("status_ingestion", {})
    for field in ("replication_health_workflow", "dashboard_status_file", "textfile_metric", "ops_dashboard_refresh"):
        if field not in ingestion:
            errors.append(f"status ingestion missing {field}")
    workflow = ingestion.get("replication_health_workflow", "")
    if not workflow or not (ROOT / workflow).exists():
        errors.append("replication health workflow path is invalid")

    if set(plan.get("metrics", [])) != REQUIRED_METRICS:
        errors.append("monitoring plan must list the required replication metrics")
    alert_ids = {item.get("id") for item in plan.get("alert_conditions", [])}
    if alert_ids != REQUIRED_ALERTS:
        errors.append("monitoring plan must list the required alert conditions")

    simulation = plan.get("failure_simulation", {})
    simulation_workflow = simulation.get("workflow", "")
    if not simulation_workflow or not (ROOT / simulation_workflow).exists():
        errors.append("failure simulation workflow path is invalid")
    if "simulate-broken" not in simulation.get("mode", ""):
        errors.append("failure simulation must use simulate-broken mode")
    if "fails closed" not in simulation.get("expected_result", ""):
        errors.append("failure simulation must fail closed")

    for runbook in plan.get("runbooks", []):
        if not (ROOT / runbook).exists():
            errors.append(f"runbook path is invalid: {runbook}")

    artifact_policy = plan.get("artifact_policy", {})
    if artifact_policy.get("safe_metadata_only") is not True:
        errors.append("artifact policy must be safe metadata only")
    for item in ("database_urls", "passwords", "tokens", "wal_contents", "database_dumps", "row_data"):
        if item not in artifact_policy.get("forbidden_evidence", []):
            errors.append(f"artifact policy must forbid {item}")

    cutover = plan.get("cutover_policy", {})
    if cutover.get("failed_monitoring_blocks_cutover") is not True:
        errors.append("failed monitoring must block cutover")
    if cutover.get("supabase_remains_writer_until_issue") != 119:
        errors.append("Supabase must remain writer until issue 119")

    validator = plan.get("validation", {}).get("local_validator", "").removeprefix("python3 ")
    if not validator or not (ROOT / validator).exists():
        errors.append("local validator path is invalid")

    serialized = json.dumps(plan).lower()
    if any(marker in serialized for marker in FORBIDDEN_MARKERS):
        errors.append("monitoring plan must not include secrets, provider hostnames, or database URLs")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend PostgreSQL primary shadow monitoring plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
