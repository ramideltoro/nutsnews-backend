#!/usr/bin/env python3
"""Render a staging-only Supabase standby failover drill report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-staging-failover-drill.json"
DRILL_ID = "backend-supabase-standby-staging-failover-drill"
FAILOVER_WORKFLOW_ID = "backend-supabase-standby-failover"
ISSUE = "ramideltoro/nutsnews#503"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_TARGET = "existing_production_supabase_standby"
STAGING_APPLY_CONFIRMATION = "execute-staging-supabase-failover-drill"
DRY_RUN_CONFIRMATION = "plan-staging-supabase-failover-drill"
EXPECTED_MISSING_GO_BLOCKER = "missing_supabase_standby_promotion_decision"


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


def parse_bool(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def parse_int(value: str, blocker: str, blockers: list[str]) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        blockers.append(blocker)
        return None


def contract_blockers(contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if contract.get("schema_version") != 1:
        blockers.append("contract_schema_version_mismatch")
    if contract.get("drill_id") != DRILL_ID:
        blockers.append("drill_id_mismatch")
    if contract.get("tracking_issue") != ISSUE:
        blockers.append("contract_issue_mismatch")
    if contract.get("epic") != EPIC:
        blockers.append("contract_epic_mismatch")
    for dependency in (
        "docs/backend-supabase-standby-failover-workflow.json",
        "docs/backend-supabase-standby-recovery-boundaries.json",
        "docs/backend-database-provider-switch.json",
    ):
        if dependency not in contract.get("depends_on", []):
            blockers.append(f"missing_dependency_{dependency}")

    source = contract.get("source_before_drill", {})
    target = contract.get("target_after_drill", {})
    failover = contract.get("protected_failover_dry_run", {})
    staging_apply = contract.get("staging_apply", {})
    safety = contract.get("safety", {})
    if not isinstance(source, dict) or source.get("label") != "backend_postgres_primary":
        blockers.append("source_policy_mismatch")
    if not isinstance(target, dict) or target.get("label") != EXPECTED_TARGET:
        blockers.append("target_policy_mismatch")
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")

    if not isinstance(failover, dict) or failover.get("workflow_id") != FAILOVER_WORKFLOW_ID:
        blockers.append("protected_failover_workflow_mismatch")
    if not isinstance(failover, dict) or failover.get("issue") != "ramideltoro/nutsnews#502":
        blockers.append("protected_failover_issue_mismatch")
    if not isinstance(failover, dict) or failover.get("accepted_blocker_without_go") != EXPECTED_MISSING_GO_BLOCKER:
        blockers.append("protected_failover_missing_go_policy_mismatch")
    if not isinstance(failover, dict) or failover.get("mutation_performed") is not False:
        blockers.append("protected_failover_mutation_policy_mismatch")
    if not isinstance(failover, dict) or failover.get("provider_switch_performed_by_this_workflow") is not False:
        blockers.append("protected_failover_provider_switch_policy_mismatch")

    if not isinstance(staging_apply, dict) or staging_apply.get("operation") != "staging-apply":
        blockers.append("staging_apply_operation_mismatch")
    if not isinstance(staging_apply, dict) or staging_apply.get("environment") != "staging":
        blockers.append("staging_apply_environment_mismatch")
    if not isinstance(staging_apply, dict) or staging_apply.get("required_confirmation") != STAGING_APPLY_CONFIRMATION:
        blockers.append("staging_apply_confirmation_mismatch")
    if not isinstance(staging_apply, dict) or staging_apply.get("target_database_provider_mode") != "supabase_primary":
        blockers.append("staging_apply_provider_mode_mismatch")
    if not isinstance(staging_apply, dict) or staging_apply.get("production_writes_paused") is not True:
        blockers.append("staging_apply_write_pause_mismatch")
    if not isinstance(staging_apply, dict) or staging_apply.get("production_mutation_performed") is not False:
        blockers.append("staging_apply_production_mutation_policy_mismatch")

    required_smoke_ids = {
        "public_reads_use_supabase",
        "controlled_writes_use_supabase",
        "backend_postgres_receives_no_writes_after_failover",
        "no_split_brain",
        "write_pause_preserved",
        "negative_backend_postgres_unavailable_path",
    }
    smoke_ids = {item.get("id") for item in contract.get("required_smoke_results", []) if isinstance(item, dict)}
    if smoke_ids != required_smoke_ids:
        blockers.append("required_smoke_result_set_mismatch")
    for item in contract.get("required_smoke_results", []):
        if isinstance(item, dict) and item.get("required_status") != "PASS":
            blockers.append(f"{item.get('id', 'unknown')}_required_status_mismatch")

    if not isinstance(safety, dict) or safety.get("protected_environment") != "production-backend":
        blockers.append("protected_environment_mismatch")
    if not isinstance(safety, dict) or safety.get("runs_from") != "main":
        blockers.append("runs_from_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("contract_not_safe_metadata")
    if not isinstance(safety, dict) or safety.get("staging_only") is not True:
        blockers.append("contract_not_staging_only")
    if not isinstance(safety, dict) or safety.get("production_mutation_performed") is not False:
        blockers.append("contract_must_not_mutate_production")
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary_until_approved_production_failover") is not True:
        blockers.append("backend_primary_until_approved_failover_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("target_is_existing_production_supabase") is not True:
        blockers.append("target_existing_supabase_policy_missing")
    if not isinstance(safety, dict) or safety.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(safety, dict) or safety.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(safety, dict) or safety.get("app_worker_writes_to_supabase_before_approved_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked_before_failover")
    return sorted(set(blockers))


def failover_plan_summary(path_value: str, args: argparse.Namespace, blockers: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path_provided": bool(path_value),
        "status": "MISSING",
        "accepted": False,
        "safe_metadata_only": True,
        "blockers": [],
    }
    if not path_value:
        blockers.append("missing_protected_failover_dry_run")
        summary["blockers"].append("missing_protected_failover_dry_run")
        return summary

    try:
        data = load_json(Path(path_value), missing="protected_failover_dry_run_missing", malformed="protected_failover_dry_run_malformed")
    except ValueError as exc:
        blockers.append(str(exc))
        summary["status"] = "INVALID"
        summary["blockers"].append(str(exc))
        return summary

    plan_blockers = data.get("blockers", [])
    if not isinstance(plan_blockers, list):
        plan_blockers = []
    plan_blockers = [str(item) for item in plan_blockers]
    status = data.get("status") if isinstance(data.get("status"), str) else "UNKNOWN"
    summary.update(
        {
            "status": status,
            "operation": data.get("operation"),
            "workflow_id": data.get("workflow_id"),
            "failover_attempt_id": data.get("failover_attempt_id"),
            "candidate_application_revision": data.get("candidate_application_revision"),
            "fence_epoch": data.get("fence_epoch"),
            "target_after_failover": data.get("target_after_failover"),
            "blockers": plan_blockers,
        }
    )

    accepted = status == "dry_run_ready" or (status == "blocked" and EXPECTED_MISSING_GO_BLOCKER in plan_blockers)
    if data.get("workflow_id") != FAILOVER_WORKFLOW_ID:
        blockers.append("protected_failover_dry_run_workflow_mismatch")
    if data.get("operation") != "dry-run":
        blockers.append("protected_failover_dry_run_operation_mismatch")
    if not accepted:
        blockers.append("protected_failover_dry_run_not_accepted")
    if data.get("mutation_performed") is not False:
        blockers.append("protected_failover_dry_run_mutated")
    if data.get("provider_switch_performed_by_this_workflow") is not False:
        blockers.append("protected_failover_dry_run_switched_provider")
    if data.get("target_is_existing_production_supabase") is not True:
        blockers.append("protected_failover_dry_run_target_mismatch")
    if data.get("create_new_supabase_project") is not False:
        blockers.append("protected_failover_dry_run_new_project_not_forbidden")
    if data.get("create_nutsnews_standby_database") is not False:
        blockers.append("protected_failover_dry_run_standby_database_not_forbidden")
    if data.get("safe_metadata_only") is not True:
        blockers.append("protected_failover_dry_run_not_safe_metadata")
    if args.failover_attempt_id and data.get("failover_attempt_id") != args.failover_attempt_id:
        blockers.append("protected_failover_dry_run_attempt_mismatch")
    if args.candidate_application_revision and data.get("candidate_application_revision") != args.candidate_application_revision:
        blockers.append("protected_failover_dry_run_revision_mismatch")
    if args.fence_epoch and data.get("fence_epoch") != args.fence_epoch:
        blockers.append("protected_failover_dry_run_epoch_mismatch")

    summary["accepted"] = accepted
    return summary


def fixture_defaults(args: argparse.Namespace) -> None:
    if args.operation == "staging-apply":
        args.backend_postgres_available = "false"
        args.provider_mode = "supabase_primary"
        args.production_writes_paused = "true"
        args.failover_dry_run_status = "PASS"
        args.staging_apply_status = "PASS"
        args.public_read_status = "PASS"
        args.controlled_write_status = "PASS"
        args.backend_postgres_write_delta = "0"
        args.supabase_controlled_write_count = "1"
        args.write_eligible_provider_count = "1"
        args.eligible_provider = EXPECTED_TARGET
        args.write_pause_status = "PASS"
        args.split_brain_status = "PASS"
        args.negative_path_status = "PASS"
    else:
        args.backend_postgres_available = "false"
        args.provider_mode = "supabase_primary"
        args.production_writes_paused = "true"
        args.failover_dry_run_status = "PASS"


def smoke_result(result_id: str, status: str, details: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    result = {
        "id": result_id,
        "status": status,
        "safe_metadata_only": True,
        "details": details,
        "blockers": [],
    }
    if status != "PASS":
        result["blockers"].append(f"{result_id}_not_pass")
    blockers.extend(result["blockers"])
    return result


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if args.fixture_pass:
        fixture_defaults(args)
    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    blockers = contract_blockers(contract)
    failover_summary = failover_plan_summary(args.failover_plan, args, blockers)

    if args.operation == "staging-apply" and args.confirmation != STAGING_APPLY_CONFIRMATION:
        blockers.append("missing_staging_failover_drill_confirmation")
    if args.operation == "dry-run" and args.confirmation and args.confirmation != DRY_RUN_CONFIRMATION:
        blockers.append("invalid_staging_failover_drill_dry_run_confirmation")

    backend_postgres_available = parse_bool(args.backend_postgres_available)
    production_writes_paused = parse_bool(args.production_writes_paused)
    backend_write_delta = parse_int(args.backend_postgres_write_delta, "backend_postgres_write_delta_malformed", blockers)
    supabase_write_count = parse_int(args.supabase_controlled_write_count, "supabase_controlled_write_count_malformed", blockers)
    write_eligible_count = parse_int(args.write_eligible_provider_count, "write_eligible_provider_count_malformed", blockers)

    if backend_postgres_available is not False:
        blockers.append("backend_postgres_unavailable_not_simulated")
    if args.provider_mode != "supabase_primary":
        blockers.append("provider_mode_not_supabase_primary")
    if production_writes_paused is not True:
        blockers.append("write_pause_not_enabled")
    if args.failover_dry_run_status != "PASS":
        blockers.append("protected_failover_dry_run_smoke_not_pass")

    smoke_results: list[dict[str, Any]] = []
    if args.operation == "staging-apply":
        if args.staging_apply_status != "PASS":
            blockers.append("staging_apply_not_pass")
        if args.public_read_status != "PASS":
            blockers.append("public_read_smoke_failed")
        if args.controlled_write_status != "PASS":
            blockers.append("controlled_write_smoke_failed")
        if backend_write_delta != 0:
            blockers.append("backend_postgres_received_writes_after_failover")
        if supabase_write_count is None or supabase_write_count < 1:
            blockers.append("controlled_supabase_write_missing")
        if write_eligible_count != 1:
            blockers.append("split_brain_write_eligible_count_mismatch")
        if args.eligible_provider != EXPECTED_TARGET:
            blockers.append("split_brain_eligible_provider_mismatch")
        if args.write_pause_status != "PASS":
            blockers.append("write_pause_smoke_failed")
        if args.split_brain_status != "PASS":
            blockers.append("split_brain_smoke_failed")
        if args.negative_path_status != "PASS":
            blockers.append("negative_backend_postgres_unavailable_path_failed")

        smoke_results = [
            smoke_result("public_reads_use_supabase", args.public_read_status, {"provider_mode": args.provider_mode}, blockers),
            smoke_result(
                "controlled_writes_use_supabase",
                args.controlled_write_status,
                {"provider_mode": args.provider_mode, "controlled_write_count": supabase_write_count},
                blockers,
            ),
            smoke_result(
                "backend_postgres_receives_no_writes_after_failover",
                "PASS" if backend_write_delta == 0 else "FAIL",
                {"backend_postgres_write_delta": backend_write_delta},
                blockers,
            ),
            smoke_result(
                "no_split_brain",
                "PASS" if write_eligible_count == 1 and args.eligible_provider == EXPECTED_TARGET and args.split_brain_status == "PASS" else "FAIL",
                {"write_eligible_provider_count": write_eligible_count, "eligible_provider": args.eligible_provider},
                blockers,
            ),
            smoke_result(
                "write_pause_preserved",
                "PASS" if production_writes_paused is True and args.write_pause_status == "PASS" else "FAIL",
                {"production_writes_paused": production_writes_paused},
                blockers,
            ),
            smoke_result(
                "negative_backend_postgres_unavailable_path",
                "PASS" if backend_postgres_available is False and args.negative_path_status == "PASS" else "FAIL",
                {"backend_postgres_unavailable_simulated": backend_postgres_available is False},
                blockers,
            ),
        ]

    blockers = sorted(set(blockers))
    status = "blocked" if blockers else ("PASS" if args.operation == "staging-apply" else "dry_run_ready")
    return {
        "status": status,
        "operation": args.operation,
        "drill_id": DRILL_ID,
        "workflow_id": DRILL_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "failover_attempt_id": args.failover_attempt_id or None,
        "candidate_application_revision": args.candidate_application_revision or None,
        "fence_epoch": args.fence_epoch or None,
        "environment": "staging",
        "source_before_drill": "backend_postgres_primary",
        "target_after_drill": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "backend_postgresql_remains_primary_until_approved_production_failover": True,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "production_mutation_performed": False,
        "mutation_performed": False,
        "staging_only": True,
        "safe_metadata_only": True,
        "protected_failover_dry_run": failover_summary,
        "staging_apply_performed": args.operation == "staging-apply" and status == "PASS",
        "backend_postgres_unavailable_simulated": backend_postgres_available is False,
        "provider_mode": args.provider_mode,
        "production_writes_paused": production_writes_paused,
        "backend_postgres_write_delta_after_failover": backend_write_delta,
        "supabase_controlled_write_count": supabase_write_count,
        "write_eligible_provider_count": write_eligible_count,
        "eligible_provider": args.eligible_provider,
        "smoke_results": smoke_results,
        "blockers": blockers,
    }


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "operation": args.operation,
        "drill_id": DRILL_ID,
        "workflow_id": DRILL_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "production_mutation_performed": False,
        "mutation_performed": False,
        "safe_metadata_only": True,
        "blockers": [blocker],
    }


def write_summary(report: dict[str, Any], path: str) -> None:
    if not path:
        return
    lines = [
        "# Supabase Standby Staging Failover Drill",
        "",
        f"- Operation: `{report['operation']}`",
        f"- Status: `{report['status']}`",
        f"- Attempt: `{report.get('failover_attempt_id')}`",
        f"- Provider mode: `{report.get('provider_mode')}`",
        f"- Production writes paused: `{report.get('production_writes_paused')}`",
        f"- Backend PostgreSQL write delta: `{report.get('backend_postgres_write_delta_after_failover')}`",
        f"- Eligible provider: `{report.get('eligible_provider')}`",
        f"- Blockers: `{', '.join(report['blockers']) if report['blockers'] else 'none'}`",
        "",
        "Safe metadata only; this drill does not create Supabase resources or mutate production.",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--operation", choices=["dry-run", "staging-apply"], default="dry-run")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--failover-plan", default="")
    parser.add_argument("--failover-attempt-id", default="")
    parser.add_argument("--candidate-application-revision", default="")
    parser.add_argument("--fence-epoch", default="")
    parser.add_argument("--backend-postgres-available", choices=["unknown", "true", "false"], default="unknown")
    parser.add_argument("--provider-mode", default="unknown")
    parser.add_argument("--production-writes-paused", choices=["unknown", "true", "false"], default="unknown")
    parser.add_argument("--failover-dry-run-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--staging-apply-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--public-read-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--controlled-write-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--backend-postgres-write-delta", default="-1")
    parser.add_argument("--supabase-controlled-write-count", default="0")
    parser.add_argument("--write-eligible-provider-count", default="0")
    parser.add_argument("--eligible-provider", default="unknown")
    parser.add_argument("--write-pause-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--split-brain-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--negative-path-status", choices=["MISSING", "PASS", "FAIL"], default="MISSING")
    parser.add_argument("--fixture-pass", action="store_true")
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
