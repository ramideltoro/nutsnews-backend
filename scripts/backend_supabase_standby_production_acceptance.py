#!/usr/bin/env python3
"""Render a production Supabase standby soak and acceptance decision."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-production-acceptance.json"
ACCEPTANCE_ID = "backend-supabase-standby-production-acceptance"
ISSUE = "ramideltoro/nutsnews#505"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_TARGET = "existing_production_supabase_standby"
EXPECTED_FAILOVER_WORKFLOW_ID = "backend-supabase-standby-failover"
EXPECTED_STAGING_DRILL_ID = "backend-supabase-standby-staging-failover-drill"
EXPECTED_MISSING_GO_BLOCKER = "missing_supabase_standby_promotion_decision"
ACCEPTANCE_CONFIRMATION = "record-production-standby-acceptance"
DRY_RUN_CONFIRMATION = "plan-production-standby-acceptance"


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


def number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def int_value(value: Any) -> int | None:
    numeric = number_value(value)
    if numeric is None:
        return None
    return int(numeric)


def is_truthy_bool(value: Any) -> bool:
    return value is True


def fixture_soak_report(minimum_soak_hours: float, max_lag_seconds: int) -> dict[str, Any]:
    return {
        "status": "PASS",
        "observed_window_hours": minimum_soak_hours,
        "relay_health_status": "healthy",
        "max_observed_lag_seconds": max_lag_seconds,
        "critical_backend_health_count": 0,
        "critical_standby_failure_count": 0,
        "parity_status": "PASS",
        "target": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "safe_metadata_only": True,
    }


def fixture_failover_plan(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": "blocked",
        "operation": "dry-run",
        "workflow_id": EXPECTED_FAILOVER_WORKFLOW_ID,
        "failover_attempt_id": args.failover_attempt_id or None,
        "candidate_application_revision": args.candidate_application_revision or None,
        "fence_epoch": args.fence_epoch or None,
        "target_after_failover": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "mutation_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "safe_metadata_only": True,
        "blockers": [EXPECTED_MISSING_GO_BLOCKER],
    }


def fixture_staging_drill(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": "PASS",
        "operation": "staging-apply",
        "drill_id": EXPECTED_STAGING_DRILL_ID,
        "workflow_id": EXPECTED_STAGING_DRILL_ID,
        "failover_attempt_id": args.failover_attempt_id or None,
        "candidate_application_revision": args.candidate_application_revision or None,
        "fence_epoch": args.fence_epoch or None,
        "provider_mode": "supabase_primary",
        "production_writes_paused": True,
        "backend_postgres_write_delta_after_failover": 0,
        "supabase_controlled_write_count": 1,
        "write_eligible_provider_count": 1,
        "eligible_provider": EXPECTED_TARGET,
        "target_after_drill": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "production_mutation_performed": False,
        "mutation_performed": False,
        "staging_only": True,
        "safe_metadata_only": True,
        "blockers": [],
    }


def contract_blockers(contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if contract.get("schema_version") != 1:
        blockers.append("contract_schema_version_mismatch")
    if contract.get("acceptance_id") != ACCEPTANCE_ID:
        blockers.append("acceptance_id_mismatch")
    if contract.get("tracking_issue") != ISSUE:
        blockers.append("contract_issue_mismatch")
    if contract.get("epic") != EPIC:
        blockers.append("contract_epic_mismatch")
    for dependency in (
        "docs/backend-supabase-sync-relay.json",
        "docs/backend-supabase-standby-lag-gate.json",
        "docs/backend-supabase-standby-parity-gate.json",
        "docs/backend-supabase-standby-failover-workflow.json",
        "docs/backend-supabase-standby-staging-failover-drill.json",
    ):
        if dependency not in contract.get("depends_on", []):
            blockers.append(f"missing_dependency_{dependency}")

    source = contract.get("source_before_acceptance", {})
    target = contract.get("target_after_acceptance", {})
    soak = contract.get("soak_requirements", {})
    failover = contract.get("required_production_dry_run", {})
    staging = contract.get("required_staging_drill", {})
    decision = contract.get("decision_policy", {})
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

    if not isinstance(soak, dict) or number_value(soak.get("minimum_window_hours")) != 24:
        blockers.append("soak_minimum_window_policy_mismatch")
    if not isinstance(soak, dict) or soak.get("relay_health_status") != "healthy":
        blockers.append("soak_relay_health_policy_mismatch")
    if not isinstance(soak, dict) or int_value(soak.get("max_observed_lag_seconds")) != 30:
        blockers.append("soak_lag_policy_mismatch")
    if not isinstance(soak, dict) or int_value(soak.get("critical_backend_health_count")) != 0:
        blockers.append("soak_critical_backend_health_policy_mismatch")
    if not isinstance(soak, dict) or soak.get("parity_status") != "PASS":
        blockers.append("soak_parity_policy_mismatch")
    if not isinstance(soak, dict) or soak.get("safe_metadata_only") is not True:
        blockers.append("soak_safe_metadata_policy_mismatch")

    if not isinstance(failover, dict) or failover.get("workflow_id") != EXPECTED_FAILOVER_WORKFLOW_ID:
        blockers.append("production_dry_run_workflow_mismatch")
    if not isinstance(failover, dict) or failover.get("issue") != "ramideltoro/nutsnews#502":
        blockers.append("production_dry_run_issue_mismatch")
    if not isinstance(failover, dict) or failover.get("operation") != "dry-run":
        blockers.append("production_dry_run_operation_policy_mismatch")
    if not isinstance(failover, dict) or failover.get("accepted_blocker_without_go") != EXPECTED_MISSING_GO_BLOCKER:
        blockers.append("production_dry_run_missing_go_policy_mismatch")
    if not isinstance(failover, dict) or failover.get("mutation_performed") is not False:
        blockers.append("production_dry_run_mutation_policy_mismatch")
    if not isinstance(failover, dict) or failover.get("provider_switch_performed_by_this_workflow") is not False:
        blockers.append("production_dry_run_provider_switch_policy_mismatch")

    if not isinstance(staging, dict) or staging.get("drill_id") != EXPECTED_STAGING_DRILL_ID:
        blockers.append("staging_drill_id_policy_mismatch")
    if not isinstance(staging, dict) or staging.get("operation") != "staging-apply":
        blockers.append("staging_drill_operation_policy_mismatch")
    if not isinstance(staging, dict) or staging.get("status") != "PASS":
        blockers.append("staging_drill_status_policy_mismatch")
    if not isinstance(staging, dict) or staging.get("target_database_provider_mode") != "supabase_primary":
        blockers.append("staging_drill_provider_mode_policy_mismatch")
    if not isinstance(staging, dict) or staging.get("production_writes_paused") is not True:
        blockers.append("staging_drill_write_pause_policy_mismatch")
    if not isinstance(staging, dict) or int_value(staging.get("backend_postgres_write_delta_after_failover")) != 0:
        blockers.append("staging_drill_backend_write_delta_policy_mismatch")
    if not isinstance(staging, dict) or int_value(staging.get("write_eligible_provider_count")) != 1:
        blockers.append("staging_drill_write_eligible_count_policy_mismatch")
    if not isinstance(staging, dict) or staging.get("eligible_provider") != EXPECTED_TARGET:
        blockers.append("staging_drill_eligible_provider_policy_mismatch")

    if not isinstance(decision, dict) or decision.get("accepted_decision") != "GO":
        blockers.append("decision_go_policy_mismatch")
    if not isinstance(decision, dict) or decision.get("rejected_decision") != "NO-GO":
        blockers.append("decision_no_go_policy_mismatch")
    if not isinstance(decision, dict) or decision.get("requires_explicit_owner_decision") is not True:
        blockers.append("explicit_owner_decision_policy_missing")
    if not isinstance(decision, dict) or decision.get("go_does_not_execute_failover") is not True:
        blockers.append("go_must_not_execute_failover_policy_missing")
    if not isinstance(decision, dict) or decision.get("failover_execution_still_requires_fresh_528_go") is not True:
        blockers.append("fresh_528_go_policy_missing")

    if not isinstance(safety, dict) or safety.get("protected_environment") != "production-backend":
        blockers.append("protected_environment_mismatch")
    if not isinstance(safety, dict) or safety.get("runs_from") != "main":
        blockers.append("runs_from_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("requires_typed_confirmation_for_acceptance") is not True:
        blockers.append("acceptance_confirmation_policy_missing")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("contract_not_safe_metadata")
    if not isinstance(safety, dict) or safety.get("production_mutation_performed") is not False:
        blockers.append("contract_must_not_mutate_production")
    if not isinstance(safety, dict) or safety.get("provider_switch_performed") is not False:
        blockers.append("provider_switch_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("approved_for_production_provider_switch") is not False:
        blockers.append("provider_switch_approval_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary_until_approved_failover") is not True:
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


def soak_report_summary(args: argparse.Namespace, blockers: list[str]) -> dict[str, Any]:
    if args.soak_report:
        try:
            report = load_json(Path(args.soak_report), missing="soak_report_missing", malformed="soak_report_malformed")
        except ValueError as exc:
            blockers.append(str(exc))
            return {"status": "INVALID", "accepted": False, "blockers": [str(exc)]}
    elif args.fixture_pass:
        report = fixture_soak_report(args.minimum_soak_hours, args.max_lag_seconds)
    else:
        blockers.append("missing_soak_report")
        return {"status": "MISSING", "accepted": False, "blockers": ["missing_soak_report"]}

    local_blockers: list[str] = []
    observed_hours = number_value(report.get("observed_window_hours"))
    max_lag = number_value(report.get("max_observed_lag_seconds"))
    critical_backend_count = int_value(report.get("critical_backend_health_count"))
    critical_standby_count = int_value(report.get("critical_standby_failure_count", 0))

    if observed_hours is None or observed_hours < args.minimum_soak_hours:
        local_blockers.append("soak_window_incomplete")
    if report.get("relay_health_status") != "healthy":
        local_blockers.append("relay_unhealthy")
    if max_lag is None or max_lag > args.max_lag_seconds:
        local_blockers.append("lag_exceeds_threshold")
    if critical_backend_count is None or critical_backend_count != 0:
        local_blockers.append("critical_backend_health_present")
    if critical_standby_count is None or critical_standby_count != 0:
        local_blockers.append("critical_standby_failure_present")
    if report.get("parity_status") != "PASS":
        local_blockers.append("parity_not_pass")
    if report.get("safe_metadata_only") is not True:
        local_blockers.append("soak_report_not_safe_metadata")
    if report.get("target") not in (None, EXPECTED_TARGET):
        local_blockers.append("soak_target_mismatch")
    if report.get("target_is_existing_production_supabase") is not True:
        local_blockers.append("soak_target_existing_production_supabase_not_confirmed")
    if report.get("create_new_supabase_project") is not False:
        local_blockers.append("soak_new_supabase_project_not_forbidden")
    if report.get("create_nutsnews_standby_database") is not False:
        local_blockers.append("soak_standby_database_not_forbidden")
    if report.get("app_worker_writes_to_supabase_before_approved_failover") is not False:
        local_blockers.append("soak_app_worker_supabase_writes_not_blocked")

    blockers.extend(local_blockers)
    return {
        "status": report.get("status", "UNKNOWN"),
        "accepted": not local_blockers,
        "observed_window_hours": observed_hours,
        "relay_health_status": report.get("relay_health_status"),
        "max_observed_lag_seconds": max_lag,
        "critical_backend_health_count": critical_backend_count,
        "critical_standby_failure_count": critical_standby_count,
        "parity_status": report.get("parity_status"),
        "target": report.get("target", EXPECTED_TARGET),
        "safe_metadata_only": report.get("safe_metadata_only") is True,
        "blockers": sorted(set(local_blockers)),
    }


def failover_plan_summary(args: argparse.Namespace, blockers: list[str]) -> dict[str, Any]:
    if args.failover_plan:
        try:
            data = load_json(
                Path(args.failover_plan),
                missing="protected_failover_dry_run_missing",
                malformed="protected_failover_dry_run_malformed",
            )
        except ValueError as exc:
            blockers.append(str(exc))
            return {"status": "INVALID", "accepted": False, "blockers": [str(exc)]}
    elif args.fixture_pass:
        data = fixture_failover_plan(args)
    else:
        blockers.append("missing_protected_failover_dry_run")
        return {"status": "MISSING", "accepted": False, "blockers": ["missing_protected_failover_dry_run"]}

    local_blockers: list[str] = []
    plan_blockers = data.get("blockers", [])
    if not isinstance(plan_blockers, list):
        plan_blockers = []
    plan_blockers = [str(item) for item in plan_blockers]
    status = data.get("status") if isinstance(data.get("status"), str) else "UNKNOWN"
    accepted = status == "dry_run_ready" or (status == "blocked" and EXPECTED_MISSING_GO_BLOCKER in plan_blockers)

    if data.get("workflow_id") != EXPECTED_FAILOVER_WORKFLOW_ID:
        local_blockers.append("protected_failover_dry_run_workflow_mismatch")
    if data.get("operation") != "dry-run":
        local_blockers.append("protected_failover_dry_run_operation_mismatch")
    if not accepted:
        local_blockers.append("protected_failover_dry_run_not_accepted")
    if data.get("mutation_performed") is not False:
        local_blockers.append("protected_failover_dry_run_mutated")
    if data.get("provider_switch_performed_by_this_workflow") is not False:
        local_blockers.append("protected_failover_dry_run_switched_provider")
    if data.get("target_is_existing_production_supabase") is not True:
        local_blockers.append("protected_failover_dry_run_target_mismatch")
    if data.get("create_new_supabase_project") is not False:
        local_blockers.append("protected_failover_dry_run_new_project_not_forbidden")
    if data.get("create_nutsnews_standby_database") is not False:
        local_blockers.append("protected_failover_dry_run_standby_database_not_forbidden")
    if data.get("safe_metadata_only") is not True:
        local_blockers.append("protected_failover_dry_run_not_safe_metadata")
    if args.failover_attempt_id and data.get("failover_attempt_id") != args.failover_attempt_id:
        local_blockers.append("protected_failover_dry_run_attempt_mismatch")
    if args.candidate_application_revision and data.get("candidate_application_revision") != args.candidate_application_revision:
        local_blockers.append("protected_failover_dry_run_revision_mismatch")
    if args.fence_epoch and data.get("fence_epoch") != args.fence_epoch:
        local_blockers.append("protected_failover_dry_run_epoch_mismatch")

    blockers.extend(local_blockers)
    return {
        "status": status,
        "operation": data.get("operation"),
        "workflow_id": data.get("workflow_id"),
        "accepted": accepted and not local_blockers,
        "failover_attempt_id": data.get("failover_attempt_id"),
        "candidate_application_revision": data.get("candidate_application_revision"),
        "fence_epoch": data.get("fence_epoch"),
        "target_after_failover": data.get("target_after_failover"),
        "safe_metadata_only": data.get("safe_metadata_only") is True,
        "blockers": sorted(set([*plan_blockers, *local_blockers])),
    }


def staging_drill_summary(args: argparse.Namespace, blockers: list[str]) -> dict[str, Any]:
    if args.staging_drill:
        try:
            data = load_json(
                Path(args.staging_drill),
                missing="staging_failover_drill_missing",
                malformed="staging_failover_drill_malformed",
            )
        except ValueError as exc:
            blockers.append(str(exc))
            return {"status": "INVALID", "accepted": False, "blockers": [str(exc)]}
    elif args.fixture_pass:
        data = fixture_staging_drill(args)
    else:
        blockers.append("missing_staging_failover_drill")
        return {"status": "MISSING", "accepted": False, "blockers": ["missing_staging_failover_drill"]}

    local_blockers: list[str] = []
    report_blockers = data.get("blockers", [])
    if not isinstance(report_blockers, list):
        report_blockers = []
    report_blockers = [str(item) for item in report_blockers]

    workflow_id = data.get("drill_id") or data.get("workflow_id")
    if workflow_id != EXPECTED_STAGING_DRILL_ID:
        local_blockers.append("staging_failover_drill_id_mismatch")
    if data.get("status") != "PASS":
        local_blockers.append("staging_failover_drill_not_pass")
    if data.get("operation") != "staging-apply":
        local_blockers.append("staging_failover_drill_operation_mismatch")
    if data.get("provider_mode") != "supabase_primary":
        local_blockers.append("staging_failover_drill_provider_mode_mismatch")
    if data.get("production_writes_paused") is not True:
        local_blockers.append("staging_failover_drill_write_pause_missing")
    if int_value(data.get("backend_postgres_write_delta_after_failover")) != 0:
        local_blockers.append("staging_failover_drill_backend_write_delta_not_zero")
    if int_value(data.get("write_eligible_provider_count")) != 1:
        local_blockers.append("staging_failover_drill_write_eligible_count_mismatch")
    if data.get("eligible_provider") != EXPECTED_TARGET:
        local_blockers.append("staging_failover_drill_eligible_provider_mismatch")
    if data.get("target_is_existing_production_supabase") is not True:
        local_blockers.append("staging_failover_drill_target_mismatch")
    if data.get("create_new_supabase_project") is not False:
        local_blockers.append("staging_failover_drill_new_project_not_forbidden")
    if data.get("create_nutsnews_standby_database") is not False:
        local_blockers.append("staging_failover_drill_standby_database_not_forbidden")
    if data.get("production_mutation_performed") is not False:
        local_blockers.append("staging_failover_drill_mutated_production")
    if data.get("mutation_performed") is not False:
        local_blockers.append("staging_failover_drill_mutated")
    if data.get("safe_metadata_only") is not True:
        local_blockers.append("staging_failover_drill_not_safe_metadata")
    if args.failover_attempt_id and data.get("failover_attempt_id") != args.failover_attempt_id:
        local_blockers.append("staging_failover_drill_attempt_mismatch")
    if args.candidate_application_revision and data.get("candidate_application_revision") != args.candidate_application_revision:
        local_blockers.append("staging_failover_drill_revision_mismatch")
    if args.fence_epoch and data.get("fence_epoch") != args.fence_epoch:
        local_blockers.append("staging_failover_drill_epoch_mismatch")

    blockers.extend(local_blockers)
    return {
        "status": data.get("status", "UNKNOWN"),
        "operation": data.get("operation"),
        "drill_id": workflow_id,
        "accepted": not local_blockers,
        "provider_mode": data.get("provider_mode"),
        "production_writes_paused": data.get("production_writes_paused"),
        "backend_postgres_write_delta_after_failover": int_value(data.get("backend_postgres_write_delta_after_failover")),
        "write_eligible_provider_count": int_value(data.get("write_eligible_provider_count")),
        "eligible_provider": data.get("eligible_provider"),
        "safe_metadata_only": data.get("safe_metadata_only") is True,
        "blockers": sorted(set([*report_blockers, *local_blockers])),
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    blockers = contract_blockers(contract)
    if args.operation == "acceptance" and args.confirmation != ACCEPTANCE_CONFIRMATION:
        blockers.append("missing_production_acceptance_confirmation")
    if args.operation == "dry-run" and args.confirmation and args.confirmation != DRY_RUN_CONFIRMATION:
        blockers.append("invalid_production_acceptance_dry_run_confirmation")

    soak = soak_report_summary(args, blockers)
    failover = failover_plan_summary(args, blockers)
    staging = staging_drill_summary(args, blockers)

    if args.operation == "acceptance" and args.owner_decision != "GO":
        blockers.append("owner_recorded_no_go")
    blockers = sorted(set(blockers))

    if args.operation == "dry-run":
        status = "blocked" if blockers else "dry_run_ready"
        decision = "NO-GO"
    else:
        decision = "GO" if not blockers and args.owner_decision == "GO" else "NO-GO"
        status = decision

    official_backup_accepted = args.operation == "acceptance" and decision == "GO"
    return {
        "status": status,
        "decision": decision,
        "operation": args.operation,
        "acceptance_id": ACCEPTANCE_ID,
        "workflow_id": ACCEPTANCE_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "failover_attempt_id": args.failover_attempt_id or None,
        "candidate_application_revision": args.candidate_application_revision or None,
        "fence_epoch": args.fence_epoch or None,
        "owner_decision": args.owner_decision if args.operation == "acceptance" else None,
        "official_backup_accepted": official_backup_accepted,
        "production_soak_accepted": is_truthy_bool(soak.get("accepted")),
        "production_failover_dry_run_accepted": is_truthy_bool(failover.get("accepted")),
        "staging_failover_drill_accepted": is_truthy_bool(staging.get("accepted")),
        "source_before_acceptance": "backend_postgres_primary",
        "target_after_acceptance": EXPECTED_TARGET,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "backend_postgresql_remains_primary_until_approved_failover": True,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "requires_fresh_528_go_for_failover": True,
        "approved_for_production_provider_switch": False,
        "provider_switch_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "production_mutation_performed": False,
        "mutation_performed": False,
        "safe_metadata_only": True,
        "soak_report": soak,
        "protected_failover_dry_run": failover,
        "staging_failover_drill": staging,
        "blockers": blockers,
    }


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "NO-GO",
        "decision": "NO-GO",
        "operation": args.operation,
        "acceptance_id": ACCEPTANCE_ID,
        "workflow_id": ACCEPTANCE_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "official_backup_accepted": False,
        "production_mutation_performed": False,
        "mutation_performed": False,
        "provider_switch_performed": False,
        "safe_metadata_only": True,
        "blockers": [blocker],
    }


def write_summary(report: dict[str, Any], path: str) -> None:
    if not path:
        return
    soak = report.get("soak_report", {})
    lines = [
        "# Supabase Standby Production Acceptance",
        "",
        f"- Operation: `{report['operation']}`",
        f"- Status: `{report['status']}`",
        f"- Decision: `{report['decision']}`",
        f"- Attempt: `{report.get('failover_attempt_id')}`",
        f"- Official backup accepted: `{report.get('official_backup_accepted')}`",
        f"- Soak hours: `{soak.get('observed_window_hours')}`",
        f"- Max lag seconds: `{soak.get('max_observed_lag_seconds')}`",
        f"- Relay health: `{soak.get('relay_health_status')}`",
        f"- Parity: `{soak.get('parity_status')}`",
        f"- Protected failover dry-run accepted: `{report.get('production_failover_dry_run_accepted')}`",
        f"- Staging failover drill accepted: `{report.get('staging_failover_drill_accepted')}`",
        f"- Blockers: `{', '.join(report['blockers']) if report['blockers'] else 'none'}`",
        "",
        "Safe metadata only; this acceptance does not switch providers, create Supabase resources, or mutate production.",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--operation", choices=["dry-run", "acceptance"], default="dry-run")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--owner-decision", choices=["GO", "NO-GO"], default="NO-GO")
    parser.add_argument("--soak-report", default="")
    parser.add_argument("--failover-plan", default="")
    parser.add_argument("--staging-drill", default="")
    parser.add_argument("--failover-attempt-id", default="")
    parser.add_argument("--candidate-application-revision", default="")
    parser.add_argument("--fence-epoch", default="")
    parser.add_argument("--minimum-soak-hours", type=float, default=24.0)
    parser.add_argument("--max-lag-seconds", type=int, default=30)
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
    return 1 if args.enforce and report["status"] in {"blocked", "NO-GO"} else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
