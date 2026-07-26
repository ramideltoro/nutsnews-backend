#!/usr/bin/env python3
"""Evaluate backend writer pause and quiet write-position evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "docs" / "backend-supabase-writer-pause-gate.json"
GATE_NAME = "supabase_standby_writer_pause_quiescence"
ISSUE = "ramideltoro/nutsnews#526"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"
EXPECTED_WRITER_CLASS_IDS = {
    "backend_worker_database_api",
    "worker_uplift_runtime_services",
    "backend_mutation_workflows",
    "manual_database_access",
    "standby_sync_relay",
}
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_EVIDENCE_AGE_SECONDS = 300
RESULT_TTL_SECONDS = 300


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def seconds_between(later: str | None, earlier: str | None) -> int | None:
    later_dt = parse_utc(later)
    earlier_dt = parse_utc(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return int((later_dt - earlier_dt).total_seconds())


def iso_add(value: str, seconds: int) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return utc_now()
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def digest_object(value: Any) -> str:
    return "sha256:" + canonical_sha256(value)[:24]


def safe_fingerprint(kind: str, label: str, inventory_id: str, inventory_version: Any) -> str:
    payload = f"{kind}|{label}|{inventory_id}|{inventory_version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


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


def validate_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise ValueError("invalid_failover_attempt_id")


def writer_inventory(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = inventory.get("writer_classes", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError("writer_inventory_missing")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("writer_inventory_malformed")
        if entry["id"] in by_id:
            raise ValueError("writer_inventory_duplicate")
        if entry.get("required") is not True:
            raise ValueError("writer_inventory_required_flag_missing")
        by_id[entry["id"]] = entry
    missing_ids = EXPECTED_WRITER_CLASS_IDS - set(by_id)
    if missing_ids:
        raise ValueError("writer_inventory_incomplete")
    return dict(sorted(by_id.items()))


def base_result(args: argparse.Namespace, measured_at: str | None = None) -> dict[str, Any]:
    measured = measured_at or utc_now()
    return {
        "status": "FAIL",
        "gate": GATE_NAME,
        "issue": ISSUE,
        "epic": EPIC,
        "failover_attempt_id": args.failover_attempt_id,
        "repository_revision": args.repository_revision,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.result_ttl_seconds),
        "max_evidence_age_seconds": args.max_evidence_age_seconds,
        "quiet_window_seconds": args.quiet_window_seconds,
        "actual_quiet_window_seconds": None,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "writer_inventory_fingerprint": None,
        "write_position_fingerprint": None,
        "pause_started_at_utc": None,
        "first_write_position_at_utc": None,
        "second_write_position_at_utc": None,
        "required_writer_count": 0,
        "paused_writer_count": 0,
        "failed_writer_count": 0,
        "writer_classes": [],
        "blockers": [],
        "backend_postgresql_remains_primary": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }


def safe_writer_class(entry: dict[str, Any], observed: dict[str, Any] | None) -> dict[str, Any]:
    blockers: list[str] = []
    if observed is None:
        blockers.append("writer_class_missing")
    else:
        raw_blockers = observed.get("blockers", [])
        if isinstance(raw_blockers, list):
            blockers.extend(str(item) for item in raw_blockers if item)
        if observed.get("paused") is not True:
            blockers.append("writer_class_not_paused")
    return {
        "id": entry["id"],
        "class": entry.get("class"),
        "kind": entry.get("kind"),
        "production_write_path": entry.get("production_write_path"),
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "safe_status_only": True,
    }


def safe_automation_report(path: str) -> dict[str, Any]:
    if not path:
        return {"status": "fail", "blockers": ["automation_report_missing"]}
    return load_json(Path(path), missing="automation_report_missing", malformed="automation_report_malformed")


def position_status(report: dict[str, Any], label: str, blockers: list[str]) -> None:
    if report.get("status") != "pass":
        blockers.append(f"{label}_write_position_failed")
    if report.get("safe_metadata_only") is not True:
        blockers.append(f"{label}_write_position_not_safe_metadata")
    if report.get("source_label") != EXPECTED_SOURCE_LABEL:
        blockers.append(f"{label}_source_label_mismatch")
    if report.get("target_label") != EXPECTED_TARGET_LABEL:
        blockers.append(f"{label}_target_label_mismatch")
    if not isinstance(report.get("write_position_fingerprint"), str):
        blockers.append(f"{label}_write_position_fingerprint_missing")


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.failover_attempt_id)
    inventory = load_json(Path(args.inventory), missing="inventory_unavailable", malformed="inventory_malformed")
    inventory_id = str(inventory.get("gate_id") or "unknown")
    inventory_version = inventory.get("schema_version", "unknown")
    source_label = str(inventory.get("source", {}).get("label") or "")
    target_label = str(inventory.get("target", {}).get("label") or "")
    source_fingerprint = safe_fingerprint("source", source_label, inventory_id, inventory_version)
    target_fingerprint = safe_fingerprint("target", target_label, inventory_id, inventory_version)
    inventory_sha = "sha256:" + canonical_sha256(inventory)[:24]
    required_writers = writer_inventory(inventory)
    missing_inventory_ids = sorted(EXPECTED_WRITER_CLASS_IDS - set(required_writers))

    pause = load_json(Path(args.pause_report), missing="pause_report_missing", malformed="pause_report_malformed")
    first = load_json(Path(args.first_write_position), missing="first_write_position_missing", malformed="first_write_position_malformed")
    second = load_json(Path(args.second_write_position), missing="second_write_position_missing", malformed="second_write_position_malformed")
    automation = safe_automation_report(args.automation_report)

    measured_at = str(second.get("checked_at_utc") or args.now_utc)
    result = base_result(args, measured_at)
    blockers: list[str] = result["blockers"]
    result["source_fingerprint"] = source_fingerprint
    result["target_fingerprint"] = target_fingerprint
    result["writer_inventory_fingerprint"] = inventory_sha
    result["write_position_fingerprint"] = second.get("write_position_fingerprint")
    result["first_write_position_at_utc"] = first.get("checked_at_utc")
    result["second_write_position_at_utc"] = second.get("checked_at_utc")
    result["pause_started_at_utc"] = pause.get("pause_started_at_utc")
    result["required_writer_count"] = len(required_writers)

    if source_label != EXPECTED_SOURCE_LABEL:
        blockers.append("source_policy_mismatch")
    if target_label != EXPECTED_TARGET_LABEL:
        blockers.append("target_policy_mismatch")
    if missing_inventory_ids:
        blockers.append("writer_inventory_incomplete")
    target = inventory.get("target", {})
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    safety = inventory.get("safety", {})
    if not isinstance(safety, dict):
        blockers.append("backend_primary_policy_mismatch")
    else:
        if safety.get("backend_postgresql_remains_primary") is not True:
            blockers.append("backend_primary_policy_mismatch")
        if safety.get("target_is_existing_production_supabase") is not True:
            blockers.append("target_existing_production_supabase_not_confirmed")
        if safety.get("create_new_supabase_project") is not False:
            blockers.append("new_supabase_project_not_forbidden")
        if safety.get("create_nutsnews_standby_database") is not False:
            blockers.append("nutsnews_standby_database_not_forbidden")
        if safety.get("app_worker_writes_to_supabase_before_failover") is not False:
            blockers.append("app_worker_supabase_writes_not_blocked")
        if safety.get("safe_metadata_only") is not True:
            blockers.append("pause_report_not_safe_metadata")

    if pause.get("safe_metadata_only") is not True:
        blockers.append("pause_report_not_safe_metadata")
    if pause.get("status") != "pass":
        blockers.append("pause_report_failed")
    if pause.get("attempt_id") != args.failover_attempt_id:
        blockers.append("pause_attempt_mismatch")
    if pause.get("writer_inventory_fingerprint") != inventory_sha:
        blockers.append("writer_inventory_fingerprint_mismatch")
    if pause.get("all_writers_paused") is not True:
        blockers.append("not_all_writers_paused")
    if pause.get("active_pause_state") is not True:
        blockers.append("pause_state_not_active")
    if pause.get("resumed_at_utc"):
        blockers.append("writer_resumed_during_attempt")
    if pause.get("drain", {}).get("status") != "pass":
        blockers.append("drain_timeout")
    if pause.get("unknown_writers"):
        blockers.append("unknown_writer")

    observed_by_id = {
        item.get("id"): item
        for item in pause.get("writer_classes", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    extra_ids = sorted(set(observed_by_id) - set(required_writers))
    if extra_ids:
        blockers.append("unknown_writer")
    writer_results = [safe_writer_class(entry, observed_by_id.get(writer_id)) for writer_id, entry in required_writers.items()]
    result["writer_classes"] = writer_results
    result["paused_writer_count"] = sum(1 for item in writer_results if item["status"] == "PASS")
    result["failed_writer_count"] = sum(1 for item in writer_results if item["status"] == "FAIL")
    if result["failed_writer_count"]:
        blockers.append("writer_pause_failed")

    position_status(first, "first", blockers)
    position_status(second, "second", blockers)
    if first.get("write_position_fingerprint") != second.get("write_position_fingerprint"):
        blockers.append("observed_write_position_advance")
    if first.get("manifest_fingerprint") != second.get("manifest_fingerprint"):
        blockers.append("write_position_manifest_mismatch")

    actual_quiet = seconds_between(str(second.get("checked_at_utc") or ""), str(first.get("checked_at_utc") or ""))
    result["actual_quiet_window_seconds"] = actual_quiet
    if actual_quiet is None:
        blockers.append("quiet_window_time_malformed")
    elif actual_quiet < args.quiet_window_seconds:
        blockers.append("quiet_window_too_short")

    pause_to_first = seconds_between(str(first.get("checked_at_utc") or ""), str(pause.get("pause_started_at_utc") or ""))
    if pause_to_first is None:
        blockers.append("pause_time_malformed")
    elif pause_to_first < -60:
        blockers.append("write_position_collected_before_pause")

    evidence_age = seconds_between(args.now_utc, str(second.get("checked_at_utc") or ""))
    result["evidence_age_seconds"] = evidence_age
    if evidence_age is None:
        blockers.append("evidence_time_malformed")
    elif evidence_age < -60:
        blockers.append("evidence_time_from_future")
    elif evidence_age > args.max_evidence_age_seconds:
        blockers.append("evidence_stale")

    if automation.get("safe_metadata_only") is not True:
        blockers.append("automation_report_not_safe_metadata")
    if automation.get("status") != "pass":
        blockers.append("writer_automation_freeze_failed")
    if automation.get("manual_freeze_confirmed") is not True:
        blockers.append("manual_freeze_not_confirmed")
    active_workflows = automation.get("active_writer_workflows", [])
    if active_workflows:
        blockers.append("active_writer_workflow")

    result["blockers"] = sorted(set(blockers))
    result["status"] = "PASS" if not result["blockers"] else "FAIL"
    return result


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    result = base_result(args, args.now_utc)
    result["blockers"] = [blocker]
    return result


def write_outputs(result: dict[str, Any], output: str, summary: str) -> None:
    text = json.dumps(result, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    if summary:
        lines = [
            "# Supabase Standby Writer Pause Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Repository revision: `{result.get('repository_revision')}`",
            f"- Required writer classes: `{result.get('required_writer_count')}`",
            f"- Paused writer classes: `{result.get('paused_writer_count')}`",
            f"- Failed writer classes: `{result.get('failed_writer_count')}`",
            f"- Quiet window seconds: `{result.get('actual_quiet_window_seconds')}`",
            f"- Measured at: `{result.get('measured_at_utc')}`",
            f"- Expires at: `{result.get('expires_at_utc')}`",
            f"- Writer inventory fingerprint: `{result.get('writer_inventory_fingerprint')}`",
            f"- Write position fingerprint: `{result.get('write_position_fingerprint')}`",
            f"- Source fingerprint: `{result.get('source_fingerprint')}`",
            f"- Target fingerprint: `{result.get('target_fingerprint')}`",
            f"- Blockers: `{', '.join(result.get('blockers', [])) or 'none'}`",
            "",
            "Report policy: safe metadata only; no credentials, SQL text, PostgreSQL errors, or row data are printed.",
        ]
        Path(summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    parser.add_argument("--pause-report", required=True)
    parser.add_argument("--first-write-position", required=True)
    parser.add_argument("--second-write-position", required=True)
    parser.add_argument("--automation-report", required=True)
    parser.add_argument("--failover-attempt-id", required=True)
    parser.add_argument("--repository-revision", default="")
    parser.add_argument("--quiet-window-seconds", type=int, default=120)
    parser.add_argument("--max-evidence-age-seconds", type=int, default=MAX_EVIDENCE_AGE_SECONDS)
    parser.add_argument("--result-ttl-seconds", type=int, default=RESULT_TTL_SECONDS)
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    if args.quiet_window_seconds < 30 or args.quiet_window_seconds > 600:
        raise SystemExit("quiet_window_seconds_out_of_bounds")
    if args.max_evidence_age_seconds < 1 or args.max_evidence_age_seconds > 900:
        raise SystemExit("max_evidence_age_seconds_out_of_bounds")
    if args.result_ttl_seconds < 1 or args.result_ttl_seconds > 900:
        raise SystemExit("result_ttl_seconds_out_of_bounds")
    return args


def main_args(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = evaluate(args)
    except ValueError as exc:
        result = fail_result(args, str(exc))
    write_outputs(result, args.output, args.summary)
    return 1 if args.enforce and result["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
