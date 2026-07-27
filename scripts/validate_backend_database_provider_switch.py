#!/usr/bin/env python3
"""Validate the backend database provider switch contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWITCH_PATH = ROOT / "docs" / "backend-database-provider-switch.json"
API_CONTRACT_PATH = ROOT / "docs" / "backend-api-compatibility-contract.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    switch = load_json(SWITCH_PATH)
    api_contract = load_json(API_CONTRACT_PATH)
    errors: list[str] = []

    if switch.get("contract_id") != "backend-database-provider-switch":
        errors.append("contract_id must be backend-database-provider-switch")
    if switch.get("issue") != 117:
        errors.append("issue must be 117")
    if switch.get("safe_default") != "supabase_primary":
        errors.append("safe_default must be supabase_primary")
    if switch.get("production_primary_confirmation") != "enable-backend-postgres-primary":
        errors.append("production primary confirmation must be explicit")
    if "docs/backend-api-compatibility-contract.json" not in switch.get("depends_on", []):
        errors.append("provider switch must depend on the backend API compatibility contract")
    if "docs/backend-supabase-standby-promotion-decision.json" not in switch.get("depends_on", []):
        errors.append("provider switch must depend on the Supabase standby promotion decision")
    if "docs/backend-supabase-standby-recovery-boundaries.json" not in switch.get("depends_on", []):
        errors.append("provider switch must depend on the Supabase standby recovery boundaries")
    if api_contract.get("production_cutover_blocker") is not True:
        errors.append("API compatibility contract must remain a production cutover blocker")

    modes = {item.get("mode"): item for item in switch.get("modes", [])}
    for mode in ("supabase_primary", "backend_postgres_shadow", "backend_postgres_primary"):
        if mode not in modes:
            errors.append(f"missing provider mode: {mode}")
    if modes.get("supabase_primary", {}).get("safe_default") is not True:
        errors.append("supabase_primary must be the only safe default")
    if modes.get("backend_postgres_shadow", {}).get("writer") != "supabase":
        errors.append("shadow mode must keep Supabase as writer")
    if modes.get("backend_postgres_shadow", {}).get("serves_production_responses") is not False:
        errors.append("shadow mode must not serve production responses")
    primary = modes.get("backend_postgres_primary", {})
    if primary.get("requires_protected_cutover") is not True:
        errors.append("backend_postgres_primary must require protected cutover")
    if primary.get("requires_confirmation") != switch.get("production_primary_confirmation"):
        errors.append("backend_postgres_primary confirmation must match top-level confirmation")

    env = {item.get("name"): item for item in switch.get("environment_variables", [])}
    provider_mode = env.get("NUTSNEWS_DATABASE_PROVIDER_MODE", {})
    if provider_mode.get("production_primary_allowed_by_default") is not False:
        errors.append("production primary mode must not be allowed by default")
    token = env.get("NUTSNEWS_BACKEND_API_TOKEN", {})
    if token.get("secret_scope") != "protected_environment":
        errors.append("NUTSNEWS_BACKEND_API_TOKEN must be scoped to a protected environment")

    replacements = switch.get("secret_replacements", [])
    if not any(item.get("old") == "SUPABASE_SERVICE_ROLE_KEY" and item.get("new") == "NUTSNEWS_BACKEND_API_TOKEN" for item in replacements):
        errors.append("service-role replacement must be documented")

    order = switch.get("deployment_order", [])
    for required in ("app backend_postgres_shadow support", "worker backend_postgres_shadow support", "staging rehearsal with rollback"):
        if required not in order:
            errors.append(f"missing deployment order step: {required}")

    rollback = switch.get("rollback", {})
    if rollback.get("target_mode") != "supabase_primary":
        errors.append("rollback target must be supabase_primary")
    rollback_text = "\n".join(rollback.get("requires", [])) + "\n" + rollback.get("forward_recovery_boundary", "")
    for required in ("writer pause evidence", "no concurrent writes", "forward recovery"):
        if required not in rollback_text:
            errors.append(f"rollback contract missing: {required}")

    companion_issues = set(switch.get("companion_issues", []))
    for url in (
        "https://github.com/ramideltoro/nutsnews/issues/255",
        "https://github.com/ramideltoro/nutsnews-worker/issues/27",
    ):
        if url not in companion_issues:
            errors.append(f"missing companion issue: {url}")

    validation = switch.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_backend_database_provider_switch.py":
        errors.append("local_validator must name this validator")
    if validation.get("dry_run_workflow") != ".github/workflows/backend-database-provider-switch-dry-run.yml":
        errors.append("dry_run_workflow must name the workflow")
    live_status = validation.get("live_status", "")
    for required in ("app and worker provider modes implemented", "protected cutover", "owner approval", "hot standby"):
        if required not in live_status:
            errors.append(f"live_status must record provider switch status: {required}")

    standby_decision = switch.get("supabase_standby_promotion_decision", {})
    if standby_decision.get("issue") != "ramideltoro/nutsnews#528":
        errors.append("provider switch must point to #528 standby promotion decision")
    if standby_decision.get("required_for_production_provider_switch") is not True:
        errors.append("production provider switch must require the #528 decision")
    if standby_decision.get("accepted_decision") != "GO":
        errors.append("provider switch must consume only GO decisions")
    if standby_decision.get("single_use") is not True:
        errors.append("provider switch decision consumption must be single-use")
    if standby_decision.get("ttl_seconds") != 300:
        errors.append("provider switch decision TTL must be 300 seconds")

    standby_recovery = switch.get("supabase_standby_recovery_boundaries", {})
    if standby_recovery.get("issue") != "ramideltoro/nutsnews#504":
        errors.append("provider switch must point to #504 standby recovery boundaries")
    if standby_recovery.get("required_before_production_supabase_switch") is not True:
        errors.append("production Supabase switch must require #504 recovery boundaries")
    if standby_recovery.get("post_failover_authoritative_provider") != "existing_production_supabase_standby":
        errors.append("post-failover authoritative provider must be existing production Supabase")
    if standby_recovery.get("backend_postgres_reuse_policy") != "blocked_until_rebuilt_or_reconciled_from_supabase":
        errors.append("backend PostgreSQL reuse policy must block reuse until Supabase-origin reconciliation")
    switch_back_requires = "\n".join(standby_recovery.get("switch_back_requires", []))
    for required in ("parity", "sequence safety", "no-split-brain", "owner approval"):
        if required not in switch_back_requires:
            errors.append(f"switch-back requirements must include {required}")

    post_cutover = switch.get("post_cutover_status", {})
    if post_cutover.get("standby_retention_issue") != "ramideltoro/nutsnews#506":
        errors.append("post-cutover status must point to #506 standby retention policy")
    retained = "\n".join(post_cutover.get("retained_standby", []))
    for required in ("existing production Supabase", "supabase-standby", "sync relay", "failover approval"):
        if required not in retained:
            errors.append(f"post-cutover retained standby list must include {required}")
    cleanup = "\n".join(post_cutover.get("migration_cleanup_pending", []))
    if "obsolete Supabase-to-backend logical replication" not in cleanup:
        errors.append("post-cutover cleanup must be limited to obsolete Supabase-to-backend logical replication")

    for token in (
        "missing_supabase_standby_promotion_decision",
        "supabase_standby_promotion_decision_not_go",
        "supabase_standby_promotion_decision_already_consumed",
        "supabase_standby_promotion_decision_expired",
        "promotion_decision_required",
        "validate_recovery_boundaries_contract",
        "recovery_boundaries_required",
    ):
        if token not in (ROOT / "scripts" / "backend_database_provider_switch_plan.py").read_text(encoding="utf-8"):
            errors.append(f"provider switch plan missing decision guard: {token}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend database provider switch contract is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
