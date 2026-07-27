#!/usr/bin/env python3
"""Render non-mutating Supabase standby recovery boundary decisions."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-recovery-boundaries.json"
CONTRACT_ID = "backend-supabase-standby-recovery-boundaries"
ISSUE = "ramideltoro/nutsnews#504"
EPIC = "ramideltoro/nutsnews#521"
BACKEND_PRIMARY = "backend_postgres_primary"
SUPABASE_STANDBY = "existing_production_supabase_standby"
BOUNDARIES = {
    "pre_switch_abort",
    "post_supabase_failover_forward_recovery",
    "switch_back_to_backend_postgres",
}
SWITCH_BACK_EVIDENCE = {
    "backend_rebuild_or_reconciliation_from_supabase": "backend_reconciliation_evidence",
    "supabase_to_backend_parity": "parity_evidence",
    "backend_sequence_safety": "sequence_evidence",
    "no_split_brain_fence": "no_split_brain_evidence",
    "writer_pause": "writer_pause_evidence",
    "owner_approval": "owner_approval_evidence",
    "staging_drill_evidence": "staging_drill_evidence",
}


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


def tri_state(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def contract_blockers(contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if contract.get("schema_version") != 1:
        blockers.append("contract_schema_version_mismatch")
    if contract.get("contract_id") != CONTRACT_ID:
        blockers.append("contract_id_mismatch")
    if contract.get("tracking_issue") != ISSUE:
        blockers.append("contract_issue_mismatch")
    if contract.get("epic") != EPIC:
        blockers.append("contract_epic_mismatch")

    source = contract.get("source_before_failover", {})
    target = contract.get("target_after_failover", {})
    safety = contract.get("safety", {})
    if not isinstance(source, dict) or source.get("label") != BACKEND_PRIMARY:
        blockers.append("source_policy_mismatch")
    if not isinstance(target, dict) or target.get("label") != SUPABASE_STANDBY:
        blockers.append("target_policy_mismatch")
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("contract_not_safe_metadata")
    if not isinstance(safety, dict) or safety.get("mutates_production") is not False:
        blockers.append("contract_must_not_mutate_production")
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary_until_approved_failover") is not True:
        blockers.append("backend_primary_until_failover_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("app_worker_writes_to_supabase_before_approved_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked_before_failover")
    if not isinstance(safety, dict) or safety.get("post_failover_supabase_is_authoritative") is not True:
        blockers.append("post_failover_supabase_authority_policy_missing")
    if not isinstance(safety, dict) or safety.get("backend_postgres_reuse_blocked_until_rebuilt_or_reconciled_from_supabase") is not True:
        blockers.append("backend_reuse_block_policy_missing")

    boundaries = {item.get("id"): item for item in contract.get("boundaries", []) if isinstance(item, dict)}
    if set(boundaries) != BOUNDARIES:
        blockers.append("boundary_set_mismatch")
    switch_back = boundaries.get("switch_back_to_backend_postgres", {})
    required_gate_ids = set(switch_back.get("requires_gate_ids", [])) if isinstance(switch_back, dict) else set()
    if required_gate_ids != set(SWITCH_BACK_EVIDENCE):
        blockers.append("switch_back_gate_set_mismatch")

    gate_ids = {item.get("id") for item in contract.get("switch_back_gates", []) if isinstance(item, dict)}
    if gate_ids != set(SWITCH_BACK_EVIDENCE):
        blockers.append("switch_back_gate_contract_mismatch")
    for item in contract.get("switch_back_gates", []):
        if isinstance(item, dict) and item.get("required_status") != "PASS":
            blockers.append(f"{item.get('id', 'unknown')}_required_status_mismatch")
    return sorted(set(blockers))


def evidence_summary(evidence_id: str, path_value: str) -> dict[str, Any]:
    evidence_blocker_id = evidence_id if evidence_id.endswith("_evidence") else f"{evidence_id}_evidence"
    summary: dict[str, Any] = {
        "id": evidence_id,
        "path_provided": bool(path_value),
        "status": "MISSING",
        "safe_metadata_only": True,
        "blockers": [],
    }
    if not path_value:
        summary["blockers"].append(f"missing_{evidence_blocker_id}")
        return summary
    try:
        data = load_json(Path(path_value), missing=f"{evidence_blocker_id}_missing", malformed=f"{evidence_blocker_id}_malformed")
    except ValueError as exc:
        summary["status"] = "INVALID"
        summary["blockers"].append(str(exc))
        return summary

    status = data.get("status")
    summary["status"] = status if isinstance(status, str) else "UNKNOWN"
    if status != "PASS":
        summary["blockers"].append(f"{evidence_id}_evidence_not_pass")
    if data.get("safe_metadata_only") is not True:
        summary["safe_metadata_only"] = False
        summary["blockers"].append(f"{evidence_id}_evidence_not_safe_metadata")

    source_label = data.get("source_label")
    target_label = data.get("target_label")
    if evidence_id in {
        "backend_rebuild_or_reconciliation_from_supabase",
        "supabase_to_backend_parity",
    }:
        if source_label != SUPABASE_STANDBY:
            summary["blockers"].append(f"{evidence_id}_source_not_authoritative_supabase")
        if target_label != "backend_postgres_rebuilt_candidate":
            summary["blockers"].append(f"{evidence_id}_target_not_backend_candidate")
    if evidence_id == "backend_sequence_safety" and target_label != "backend_postgres_rebuilt_candidate":
        summary["blockers"].append("backend_sequence_safety_target_not_backend_candidate")
    if evidence_id == "no_split_brain_fence":
        if data.get("write_eligible_provider_count") != 1:
            summary["blockers"].append("no_split_brain_fence_write_eligible_count_mismatch")
        if data.get("eligible_provider") not in {SUPABASE_STANDBY, "backend_postgres_rebuilt_candidate"}:
            summary["blockers"].append("no_split_brain_fence_eligible_provider_mismatch")
    return summary


def base_report(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    boundaries = {item["id"]: item for item in contract.get("boundaries", []) if isinstance(item, dict) and item.get("id") in BOUNDARIES}
    boundary = boundaries.get(args.boundary, {})
    return {
        "status": "blocked",
        "boundary": args.boundary,
        "contract_id": CONTRACT_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "failover_attempt_id": args.failover_attempt_id or None,
        "fence_epoch": args.fence_epoch or None,
        "provider_switch_performed": tri_state(args.provider_switch_performed),
        "mutation_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "source_before_failover": BACKEND_PRIMARY,
        "target_after_failover": SUPABASE_STANDBY,
        "authoritative_provider": boundary.get("authoritative_provider") or boundary.get("authoritative_provider_before_switch"),
        "backend_postgres_reuse_allowed": boundary.get("backend_postgres_reuse_allowed"),
        "backend_postgres_reuse_policy": "blocked_until_rebuilt_or_reconciled_from_authoritative_supabase",
        "supabase_is_authoritative_after_failover": args.boundary != "pre_switch_abort",
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "safe_metadata_only": True,
        "required_switch_back_gate_count": len(SWITCH_BACK_EVIDENCE),
        "passed_switch_back_gate_count": 0,
        "evidence_results": [],
        "blockers": [],
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    blockers = contract_blockers(contract)
    report = base_report(args, contract)

    provider_switch_performed = tri_state(args.provider_switch_performed)
    if args.boundary == "pre_switch_abort" and provider_switch_performed is True:
        blockers.append("pre_switch_abort_not_allowed_after_provider_switch")
    if args.boundary in {"post_supabase_failover_forward_recovery", "switch_back_to_backend_postgres"} and provider_switch_performed is False:
        blockers.append("post_failover_boundary_requires_completed_provider_switch")

    if args.boundary == "switch_back_to_backend_postgres":
        evidence_results = [
            evidence_summary(evidence_id, str(getattr(args, argument)))
            for evidence_id, argument in SWITCH_BACK_EVIDENCE.items()
        ]
        report["evidence_results"] = evidence_results
        report["passed_switch_back_gate_count"] = sum(1 for item in evidence_results if item["status"] == "PASS" and not item["blockers"])
        for item in evidence_results:
            blockers.extend(item["blockers"])
        if report["passed_switch_back_gate_count"] != report["required_switch_back_gate_count"]:
            blockers.append("not_all_switch_back_gates_passed")

    report["blockers"] = sorted(set(blockers))
    if not report["blockers"]:
        report["status"] = "dry_run_ready"
    return report


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "boundary": args.boundary,
        "contract_id": CONTRACT_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "failover_attempt_id": args.failover_attempt_id or None,
        "fence_epoch": args.fence_epoch or None,
        "mutation_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "safe_metadata_only": True,
        "blockers": [blocker],
    }


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--boundary", choices=sorted(BOUNDARIES), default="post_supabase_failover_forward_recovery")
    parser.add_argument("--provider-switch-performed", choices=["unknown", "true", "false"], default="unknown")
    parser.add_argument("--failover-attempt-id", default="")
    parser.add_argument("--fence-epoch", default="")
    parser.add_argument("--backend-reconciliation-evidence", default="")
    parser.add_argument("--parity-evidence", default="")
    parser.add_argument("--sequence-evidence", default="")
    parser.add_argument("--no-split-brain-evidence", default="")
    parser.add_argument("--writer-pause-evidence", default="")
    parser.add_argument("--owner-approval-evidence", default="")
    parser.add_argument("--staging-drill-evidence", default="")
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
    if args.summary:
        lines = [
            "# Supabase Standby Recovery Boundaries",
            "",
            f"- Boundary: `{report['boundary']}`",
            f"- Status: `{report['status']}`",
            f"- Authoritative provider: `{report.get('authoritative_provider')}`",
            f"- Backend PostgreSQL reuse: `{report.get('backend_postgres_reuse_allowed')}`",
            f"- Switch-back gates passed: `{report.get('passed_switch_back_gate_count', 0)}/{report.get('required_switch_back_gate_count', 0)}`",
            f"- Blockers: `{', '.join(report['blockers']) if report['blockers'] else 'none'}`",
            "",
            "Safe metadata only; this workflow does not switch providers or mutate production.",
        ]
        Path(args.summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(text)
    return 1 if args.enforce and report["status"] != "dry_run_ready" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
