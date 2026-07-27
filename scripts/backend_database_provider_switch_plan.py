#!/usr/bin/env python3
"""Emit a non-mutating backend database provider switch plan."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWITCH_PATH = ROOT / "docs" / "backend-database-provider-switch.json"
RECOVERY_BOUNDARIES_PATH = ROOT / "docs" / "backend-supabase-standby-recovery-boundaries.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def load_json(path: str, blocker_prefix: str, blockers: list[str]) -> dict | None:
    if not path:
        blockers.append("missing_supabase_standby_promotion_decision")
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        blockers.append(f"{blocker_prefix}_missing")
        return None
    except json.JSONDecodeError:
        blockers.append(f"{blocker_prefix}_malformed")
        return None
    if not isinstance(data, dict):
        blockers.append(f"{blocker_prefix}_malformed")
        return None
    return data


def ledger_consumed(path: str, decision_id: str) -> bool:
    if not path:
        return False
    try:
        ledger = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(ledger, dict):
        return False
    consumed = ledger.get("consumed_decision_ids")
    if isinstance(consumed, list) and decision_id in {str(item) for item in consumed}:
        return True
    decisions = ledger.get("decisions")
    if isinstance(decisions, list):
        return any(
            isinstance(item, dict) and item.get("decision_id") == decision_id and item.get("consumed_at_utc")
            for item in decisions
        )
    return False


def validate_promotion_decision(args: argparse.Namespace, blockers: list[str]) -> dict:
    decision = load_json(args.promotion_decision, "supabase_standby_promotion_decision", blockers)
    if decision is None:
        return {"decision_id": None, "would_consume": False}

    decision_id = decision.get("decision_id") if isinstance(decision.get("decision_id"), str) else None
    if decision.get("decision") != "GO" or decision.get("status") != "GO":
        blockers.append("supabase_standby_promotion_decision_not_go")
    if decision.get("safe_metadata_only") is not True:
        blockers.append("supabase_standby_promotion_decision_not_safe_metadata")
    if decision.get("single_use") is not True:
        blockers.append("supabase_standby_promotion_decision_not_single_use")
    if decision.get("consumed") is True:
        blockers.append("supabase_standby_promotion_decision_already_consumed")
    if not decision_id:
        blockers.append("supabase_standby_promotion_decision_id_missing")
    elif ledger_consumed(args.decision_consumption_ledger, decision_id):
        blockers.append("supabase_standby_promotion_decision_already_consumed")

    now = parse_utc(args.now_utc)
    expires = parse_utc(str(decision.get("expires_at_utc") or ""))
    if now is None or expires is None:
        blockers.append("supabase_standby_promotion_decision_expiry_malformed")
    elif now > expires:
        blockers.append("supabase_standby_promotion_decision_expired")

    if not args.failover_attempt_id:
        blockers.append("missing_failover_attempt_id")
    elif decision.get("failover_attempt_id") != args.failover_attempt_id:
        blockers.append("supabase_standby_promotion_decision_attempt_mismatch")
    if not args.candidate_application_revision:
        blockers.append("missing_candidate_application_revision")
    elif decision.get("candidate_application_revision") != args.candidate_application_revision:
        blockers.append("supabase_standby_promotion_decision_revision_mismatch")
    if not args.fence_epoch:
        blockers.append("missing_fence_epoch")
    elif decision.get("fence_epoch") != args.fence_epoch:
        blockers.append("supabase_standby_promotion_decision_epoch_mismatch")

    if decision.get("target_is_existing_production_supabase") is not True:
        blockers.append("supabase_standby_promotion_decision_target_mismatch")
    if decision.get("create_new_supabase_project") is not False:
        blockers.append("supabase_standby_promotion_decision_new_project_not_forbidden")
    if decision.get("create_nutsnews_standby_database") is not False:
        blockers.append("supabase_standby_promotion_decision_standby_database_not_forbidden")
    if decision.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("supabase_standby_promotion_decision_supabase_writes_not_blocked")

    return {"decision_id": decision_id, "would_consume": bool(decision_id)}


def validate_recovery_boundaries_contract(path: str, blockers: list[str]) -> dict:
    recovery = load_json(path or str(RECOVERY_BOUNDARIES_PATH), "supabase_standby_recovery_boundaries", blockers)
    if recovery is None:
        return {
            "contract_id": None,
            "post_failover_authoritative_provider": None,
            "backend_postgres_reuse_policy": None,
        }

    if recovery.get("contract_id") != "backend-supabase-standby-recovery-boundaries":
        blockers.append("supabase_standby_recovery_boundaries_contract_mismatch")
    if recovery.get("tracking_issue") != "ramideltoro/nutsnews#504":
        blockers.append("supabase_standby_recovery_boundaries_issue_mismatch")

    target = recovery.get("target_after_failover", {})
    safety = recovery.get("safety", {})
    if not isinstance(target, dict) or target.get("label") != "existing_production_supabase_standby":
        blockers.append("supabase_standby_recovery_boundaries_target_mismatch")
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("supabase_standby_recovery_boundaries_existing_target_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("supabase_standby_recovery_boundaries_new_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("supabase_standby_recovery_boundaries_standby_database_not_forbidden")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("supabase_standby_recovery_boundaries_not_safe_metadata")
    if not isinstance(safety, dict) or safety.get("post_failover_supabase_is_authoritative") is not True:
        blockers.append("supabase_standby_recovery_boundaries_authority_missing")
    if not isinstance(safety, dict) or safety.get("backend_postgres_reuse_blocked_until_rebuilt_or_reconciled_from_supabase") is not True:
        blockers.append("supabase_standby_recovery_boundaries_backend_reuse_not_blocked")

    boundaries = {
        item.get("id"): item
        for item in recovery.get("boundaries", [])
        if isinstance(item, dict)
    }
    switch_back = boundaries.get("switch_back_to_backend_postgres", {})
    post_failover = boundaries.get("post_supabase_failover_forward_recovery", {})
    if not isinstance(post_failover, dict) or post_failover.get("authoritative_provider") != "existing_production_supabase_standby":
        blockers.append("supabase_standby_recovery_boundaries_post_failover_authority_mismatch")
    if not isinstance(switch_back, dict) or switch_back.get("backend_postgres_reuse_allowed") != "only_after_rebuild_or_reconciliation_from_supabase":
        blockers.append("supabase_standby_recovery_boundaries_switch_back_policy_mismatch")

    return {
        "contract_id": recovery.get("contract_id"),
        "post_failover_authoritative_provider": post_failover.get("authoritative_provider") if isinstance(post_failover, dict) else None,
        "backend_postgres_reuse_policy": "blocked_until_rebuilt_or_reconciled_from_supabase",
    }


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="supabase_primary")
    parser.add_argument("--environment", default="non-production")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--promotion-decision", default="")
    parser.add_argument("--decision-consumption-ledger", default="")
    parser.add_argument("--recovery-boundaries", default=str(RECOVERY_BOUNDARIES_PATH))
    parser.add_argument("--failover-attempt-id", default="")
    parser.add_argument("--candidate-application-revision", default="")
    parser.add_argument("--fence-epoch", default="")
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    switch = json.loads(SWITCH_PATH.read_text(encoding="utf-8"))
    modes = {item["mode"]: item for item in switch["modes"]}
    if args.mode not in modes:
        raise SystemExit(f"unsupported provider mode: {args.mode}")

    selected = modes[args.mode]
    blockers: list[str] = []
    if args.mode == "backend_postgres_primary" and args.confirmation != switch["production_primary_confirmation"]:
        blockers.append("missing_backend_postgres_primary_confirmation")
    if args.environment == "production" and args.mode != "supabase_primary":
        blockers.append("production_switch_requires_protected_cutover_workflow")
    promotion = {"decision_id": None, "would_consume": False}
    recovery = {
        "contract_id": None,
        "post_failover_authoritative_provider": None,
        "backend_postgres_reuse_policy": None,
    }
    if args.environment == "production":
        recovery = validate_recovery_boundaries_contract(args.recovery_boundaries, blockers)
        promotion = validate_promotion_decision(args, blockers)

    report = {
        "status": "blocked" if blockers else "dry_run_ready",
        "checked_at_utc": utc_now(),
        "mode": args.mode,
        "environment": args.environment,
        "writer": selected["writer"],
        "serves_production_responses": selected["serves_production_responses"],
        "mutation_performed": False,
        "required_environment": switch["environment_variables"],
        "deployment_order": switch["deployment_order"],
        "rollback": switch["rollback"],
        "blockers": blockers,
        "safe_default": switch["safe_default"],
        "promotion_decision_required": args.environment == "production",
        "promotion_decision_id": promotion["decision_id"],
        "would_consume_promotion_decision": promotion["would_consume"] and not blockers,
        "recovery_boundaries_required": args.environment == "production",
        "recovery_boundaries_contract_id": recovery["contract_id"],
        "post_failover_authoritative_provider": recovery["post_failover_authoritative_provider"],
        "backend_postgres_reuse_policy": recovery["backend_postgres_reuse_policy"],
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
