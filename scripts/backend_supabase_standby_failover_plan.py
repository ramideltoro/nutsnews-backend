#!/usr/bin/env python3
"""Render a protected Supabase standby failover plan."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backend_database_provider_switch_plan as provider_switch

DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-failover-workflow.json"
DEFAULT_RECOVERY_BOUNDARIES = ROOT / "docs" / "backend-supabase-standby-recovery-boundaries.json"
WORKFLOW_ID = "backend-supabase-standby-failover"
ISSUE = "ramideltoro/nutsnews#502"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_TARGET = "existing_production_supabase_standby"
APPLY_CONFIRMATION = "execute-supabase-standby-failover"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, *, missing: str, malformed: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(missing) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(malformed) from exc
    if not isinstance(data, dict):
        raise ValueError(malformed)
    return data


def contract_blockers(contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if contract.get("schema_version") != 1:
        blockers.append("contract_schema_version_mismatch")
    if contract.get("workflow_id") != WORKFLOW_ID:
        blockers.append("workflow_id_mismatch")
    if contract.get("tracking_issue") != ISSUE:
        blockers.append("contract_issue_mismatch")
    if contract.get("epic") != EPIC:
        blockers.append("contract_epic_mismatch")
    for dependency in (
        "docs/backend-supabase-standby-promotion-decision.json",
        "docs/backend-supabase-standby-recovery-boundaries.json",
        "docs/backend-database-provider-switch.json",
    ):
        if dependency not in contract.get("depends_on", []):
            blockers.append(f"missing_dependency_{dependency}")

    target = contract.get("target_after_failover", {})
    decision = contract.get("required_decision", {})
    recovery = contract.get("required_recovery_boundaries", {})
    safety = contract.get("safety", {})
    if not isinstance(target, dict) or target.get("label") != EXPECTED_TARGET:
        blockers.append("target_policy_mismatch")
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(decision, dict) or decision.get("issue") != "ramideltoro/nutsnews#528":
        blockers.append("required_decision_issue_mismatch")
    if not isinstance(decision, dict) or decision.get("accepted_decision") != "GO":
        blockers.append("required_decision_not_go")
    if not isinstance(decision, dict) or decision.get("single_use") is not True:
        blockers.append("required_decision_not_single_use")
    if not isinstance(recovery, dict) or recovery.get("issue") != "ramideltoro/nutsnews#504":
        blockers.append("required_recovery_boundaries_issue_mismatch")
    if not isinstance(recovery, dict) or recovery.get("post_failover_authoritative_provider") != EXPECTED_TARGET:
        blockers.append("required_recovery_boundaries_target_mismatch")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("contract_not_safe_metadata")
    if not isinstance(safety, dict) or safety.get("dry_run_by_default") is not True:
        blockers.append("dry_run_default_missing")
    if not isinstance(safety, dict) or safety.get("does_not_reimplement_gate_checks") is not True:
        blockers.append("gate_reimplementation_policy_missing")

    actions = {item.get("id"): item for item in contract.get("provider_switch_actions", []) if isinstance(item, dict)}
    for action_id in ("consume_promotion_decision", "app_provider_switch", "worker_provider_switch", "post_failover_smoke"):
        if action_id not in actions:
            blockers.append(f"missing_provider_switch_action_{action_id}")
    for action_id in ("app_provider_switch", "worker_provider_switch"):
        action = actions.get(action_id, {})
        if action.get("target_database_provider_mode") != "supabase_primary":
            blockers.append(f"{action_id}_target_mode_mismatch")
        if action.get("production_writes_paused") != "true":
            blockers.append(f"{action_id}_writes_pause_policy_mismatch")
        if action.get("required_confirmation") != "deploy-supabase-primary":
            blockers.append(f"{action_id}_confirmation_mismatch")
    return sorted(set(blockers))


def planned_actions(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "owner": item["owner"],
            "mechanism": item.get("mechanism"),
            "target_database_provider_mode": item.get("target_database_provider_mode"),
            "production_writes_paused": item.get("production_writes_paused"),
            "required_confirmation": item.get("required_confirmation"),
        }
        for item in contract.get("provider_switch_actions", [])
        if isinstance(item, dict)
    ]


def consumption_record(decision_id: str | None, args: argparse.Namespace) -> dict[str, Any] | None:
    if not decision_id:
        return None
    return {
        "decision_id": decision_id,
        "consumed_by": ISSUE,
        "operation": args.operation,
        "failover_attempt_id": args.failover_attempt_id,
        "candidate_application_revision": args.candidate_application_revision,
        "fence_epoch": args.fence_epoch,
        "consumed_at_utc": utc_now(),
        "safe_metadata_only": True,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    blockers = contract_blockers(contract)
    if args.operation == "apply" and args.confirmation != APPLY_CONFIRMATION:
        blockers.append("missing_supabase_standby_failover_confirmation")

    recovery = provider_switch.validate_recovery_boundaries_contract(args.recovery_boundaries, blockers)
    promotion = provider_switch.validate_promotion_decision(args, blockers)
    would_consume = args.operation == "apply" and promotion["would_consume"] and not blockers

    status = "blocked" if blockers else ("apply_ready" if args.operation == "apply" else "dry_run_ready")
    return {
        "status": status,
        "operation": args.operation,
        "workflow_id": WORKFLOW_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "failover_attempt_id": args.failover_attempt_id or None,
        "candidate_application_revision": args.candidate_application_revision or None,
        "fence_epoch": args.fence_epoch or None,
        "source_before_failover": "backend_postgres_primary",
        "target_after_failover": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "backend_postgresql_remains_primary_until_approved_failover": True,
        "promotion_decision_required": True,
        "promotion_decision_id": promotion["decision_id"],
        "would_consume_promotion_decision": would_consume,
        "consumption_record": consumption_record(promotion["decision_id"], args) if would_consume else None,
        "recovery_boundaries_required": True,
        "recovery_boundaries_contract_id": recovery["contract_id"],
        "post_failover_authoritative_provider": recovery["post_failover_authoritative_provider"],
        "backend_postgres_reuse_policy": recovery["backend_postgres_reuse_policy"],
        "provider_switch_performed_by_this_workflow": False,
        "mutation_performed": False,
        "planned_actions": planned_actions(contract),
        "safe_metadata_only": True,
        "blockers": sorted(set(blockers)),
    }


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "operation": args.operation,
        "workflow_id": WORKFLOW_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "mutation_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "safe_metadata_only": True,
        "blockers": [blocker],
    }


def write_summary(report: dict[str, Any], path: str) -> None:
    if not path:
        return
    lines = [
        "# Supabase Standby Failover Plan",
        "",
        f"- Operation: `{report['operation']}`",
        f"- Status: `{report['status']}`",
        f"- Attempt: `{report.get('failover_attempt_id')}`",
        f"- Promotion decision id: `{report.get('promotion_decision_id')}`",
        f"- Would consume decision: `{report.get('would_consume_promotion_decision')}`",
        f"- Target after failover: `{report.get('target_after_failover')}`",
        f"- Planned actions: `{len(report.get('planned_actions', []))}`",
        f"- Blockers: `{', '.join(report['blockers']) if report['blockers'] else 'none'}`",
        "",
        "Safe metadata only; gate results are consumed from #528 and are not reimplemented here.",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--operation", choices=["dry-run", "apply"], default="dry-run")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--promotion-decision", default="")
    parser.add_argument("--decision-consumption-ledger", default="")
    parser.add_argument("--recovery-boundaries", default=str(DEFAULT_RECOVERY_BOUNDARIES))
    parser.add_argument("--failover-attempt-id", default="")
    parser.add_argument("--candidate-application-revision", default="")
    parser.add_argument("--fence-epoch", default="")
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = evaluate(args)
    except ValueError as exc:
        report = fail_result(args, str(exc))

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    write_summary(report, args.summary)
    print(text)
    return 1 if args.enforce and report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
