#!/usr/bin/env python3
"""Validate the backend PostgreSQL replacement plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-replacement-plan.json"
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    plan = load_json(PLAN_PATH)
    baseline = load_json(BASELINE_PATH)
    errors: list[str] = []

    if plan.get("decision") != "deploy_private_restore_verified_failover_target":
        errors.append("decision must deploy the private restore-verified failover target")

    if plan.get("install_postgres_now") is not True:
        errors.append("install_postgres_now must be true")

    if plan.get("production_cutover_allowed") is not False:
        errors.append("production_cutover_allowed must be false")

    public_ports = {int(entry["port"]) for entry in baseline.get("public_tcp_ports", [])}
    if not public_ports.issubset({22, 80, 443}):
        errors.append(f"service baseline exposes unsupported public ports while PostgreSQL is not deployed: {sorted(public_ports)}")

    not_deployed = set(baseline.get("not_deployed", []))
    if "PostgreSQL" in not_deployed:
        errors.append("service baseline must no longer mark PostgreSQL as not_deployed")

    rpo_rto = plan.get("rpo_rto", {})
    for field in ("target_rpo", "target_rto", "current_state"):
        if field not in rpo_rto:
            errors.append(f"missing RPO/RTO field: {field}")

    usage = plan.get("current_supabase_usage", {})
    for field in ("postgres", "postgrest_data_api", "rls_and_grants", "auth", "storage", "realtime", "edge_functions", "search", "backups"):
        if field not in usage:
            errors.append(f"missing current Supabase usage field: {field}")

    topology = plan.get("target_topology", {})
    if topology.get("writer") != "single production writer only":
        errors.append("target topology must enforce a single production writer")
    if "no multi-writer" not in topology.get("standby", ""):
        errors.append("standby topology must explicitly forbid multi-writer")
    if "bare Postgres" not in topology.get("data_api", "") and "bare PostgreSQL" not in topology.get("data_api", ""):
        errors.append("data API mapping must state bare Postgres is not enough")
    if "no public database port" not in topology.get("network", ""):
        errors.append("network topology must forbid public database ports")
    if "SSH tunnel" not in topology.get("dashboard", ""):
        errors.append("dashboard topology must document SSH tunnel access")

    operating_mode = plan.get("initial_operating_mode", {})
    if operating_mode.get("writer") != "Supabase remains the only production writer.":
        errors.append("initial operating mode must keep Supabase as the production writer")
    if operating_mode.get("multi_writer") != "forbidden":
        errors.append("initial operating mode must forbid multi-writer")

    feature_mapping = plan.get("feature_mapping", {})
    for feature in (
        "Supabase Postgres tables/functions",
        "Supabase REST/PostgREST API",
        "anon and service_role keys",
        "RLS/grants",
        "Supabase Auth",
        "Supabase Storage",
        "Supabase Realtime",
        "Supabase Edge Functions",
        "full-text search",
        "backups",
    ):
        if feature not in feature_mapping:
            errors.append(f"missing feature mapping: {feature}")

    phases = [phase.get("phase") for phase in plan.get("implementation_plan", [])]
    for phase in ("private_postgres_install", "dashboard_install", "non_production_restore", "observability", "production_cutover"):
        if phase not in phases:
            errors.append(f"missing implementation phase: {phase}")

    for phase in plan.get("implementation_plan", []):
        if phase.get("phase") == "production_cutover" and phase.get("mutation_allowed") is not False:
            errors.append("production_cutover must remain disabled in this issue")

    failover = plan.get("failover", {})
    for field in ("trigger", "app_switch", "production_data_restore", "rpo", "rto"):
        if field not in failover:
            errors.append(f"missing failover field: {field}")

    failback = plan.get("failback", {})
    if failback.get("sync_back_supported") is not False:
        errors.append("failback must document that sync-back is not supported yet")
    if "bidirectional sync" not in failback.get("reason", ""):
        errors.append("failback reason must explain bidirectional sync risk")

    rollback = plan.get("rollback", {})
    for field in ("before_cutover", "during_cutover", "after_cutover"):
        if field not in rollback:
            errors.append(f"missing rollback field: {field}")

    if len(plan.get("manual_approvals_before_production_use", [])) < 3:
        errors.append("plan must document manual approvals before production use")

    validation = plan.get("validation", {})
    if validation.get("drill_workflow") != ".github/workflows/backend-postgres-failover-drill.yml":
        errors.append("validation must name the backend PostgreSQL restore drill workflow")
    if "protected apply" not in validation.get("live_verification", ""):
        errors.append("live verification must include protected apply evidence")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("PostgreSQL replacement plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
