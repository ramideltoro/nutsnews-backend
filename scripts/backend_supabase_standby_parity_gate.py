#!/usr/bin/env python3
"""Evaluate required-table parity for the Supabase standby from safe relay telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-sync-relay.json"
GATE_NAME = "supabase_standby_required_table_parity"
ISSUE = "ramideltoro/nutsnews#523"
EPIC = "ramideltoro/nutsnews#521"
MAX_TELEMETRY_AGE_SECONDS = 300
RESULT_TTL_SECONDS = 300
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def iso_add(value: str, seconds: int) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return utc_now()
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_between(later: str | None, earlier: str | None) -> int | None:
    later_dt = parse_utc(later)
    earlier_dt = parse_utc(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return int((later_dt - earlier_dt).total_seconds())


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("telemetry_unavailable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("telemetry_malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("telemetry_malformed")
    return data


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def safe_fingerprint(kind: str, label: str, contract_id: str, contract_version: Any) -> str:
    payload = f"{kind}|{label}|{contract_id}|{contract_version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def standby_binding_fingerprint(kind: str, label: str) -> str:
    payload = f"supabase-standby-binding-v1|{kind}|{label}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def command_stdout(health_report: dict[str, Any], command: str) -> str:
    commands = health_report.get("ssh", {}).get("commands", {})
    if not isinstance(commands, dict):
        return ""
    item = commands.get(command, {})
    if not isinstance(item, dict):
        return ""
    return str(item.get("stdout", ""))


def extract_relay_report(health_report: dict[str, Any]) -> dict[str, Any] | None:
    raw = command_stdout(health_report, "supabase_sync_relay_status").strip()
    if not raw or raw == "not_configured":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("relay_telemetry_malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("relay_telemetry_malformed")
    return data


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def required_tables(contract: dict[str, Any]) -> list[str]:
    tables = contract.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("manifest_tables_missing")
    names: list[str] = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            raise ValueError("manifest_tables_malformed")
        names.append(table["name"])
    return sorted(names)


def table_checks_by_name(relay_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    post_sync = relay_report.get("post_sync", {})
    checks = post_sync.get("checks") if isinstance(post_sync, dict) else None
    if not isinstance(checks, list):
        raise ValueError("comparison_checks_missing")
    by_name: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id", ""))
        name = str(check.get("name") or check_id.removeprefix("table."))
        if check_id.startswith("table.") and name:
            by_name[name] = check
    return by_name


def evaluate_table(name: str, check: dict[str, Any] | None) -> dict[str, Any]:
    if check is None:
        return {
            "name": name,
            "status": "FAIL",
            "blockers": ["table_comparison_missing"],
            "source_count": None,
            "target_count": None,
            "source_row_checksum": None,
            "target_row_checksum": None,
        }

    blockers: list[str] = []
    source_count = int_or_none(check.get("source_count"))
    target_count = int_or_none(check.get("target_count"))
    source_checksum = check.get("source_row_checksum")
    target_checksum = check.get("target_row_checksum")
    target_lag_rows = int_or_none(check.get("target_lag_rows"))
    if check.get("status") != "pass":
        blockers.append("table_status_not_pass")
    if source_count is None or target_count is None:
        blockers.append("table_count_missing")
    elif source_count != target_count:
        blockers.append("row_count_mismatch")
    if not isinstance(source_checksum, str) or not isinstance(target_checksum, str):
        blockers.append("table_checksum_missing")
    elif source_checksum != target_checksum:
        blockers.append("row_checksum_mismatch")
    if target_lag_rows is None:
        blockers.append("target_lag_rows_missing")
    elif target_lag_rows != 0:
        blockers.append("target_lag_rows_nonzero")
    if check.get("checksum_source_error") or check.get("checksum_target_error"):
        blockers.append("checksum_query_error")

    return {
        "name": name,
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "source_count": source_count,
        "target_count": target_count,
        "source_row_checksum": source_checksum if isinstance(source_checksum, str) else None,
        "target_row_checksum": target_checksum if isinstance(target_checksum, str) else None,
        "target_lag_rows": target_lag_rows,
    }


def validate_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise ValueError("invalid_failover_attempt_id")


def base_result(args: argparse.Namespace, measured_at: str | None) -> dict[str, Any]:
    measured = measured_at or utc_now()
    return {
        "status": "FAIL",
        "gate": GATE_NAME,
        "issue": ISSUE,
        "epic": EPIC,
        "failover_attempt_id": args.failover_attempt_id,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.result_ttl_seconds),
        "max_telemetry_age_seconds": args.max_telemetry_age_seconds,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "source_binding_fingerprint": None,
        "target_binding_fingerprint": None,
        "manifest_fingerprint": None,
        "relay_contract_fingerprint": None,
        "required_table_count": 0,
        "passed_table_count": 0,
        "failed_table_count": 0,
        "relay_checked_at_utc": None,
        "relay_comparison_age_seconds": None,
        "post_sync_status": "unknown",
        "tables": [],
        "blockers": [],
        "backend_postgresql_remains_primary": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.failover_attempt_id)
    contract = load_json(Path(args.contract))
    contract_id = str(contract.get("contract_id") or "unknown")
    contract_version = contract.get("version", "unknown")
    expected_source_label = str(contract.get("source", {}).get("label") or "")
    expected_target_label = str(contract.get("target", {}).get("label") or "")
    safety = contract.get("safety", {})
    target = contract.get("target", {})
    expected_source_fingerprint = safe_fingerprint("source", expected_source_label, contract_id, contract_version)
    expected_target_fingerprint = safe_fingerprint("target", expected_target_label, contract_id, contract_version)
    tables = required_tables(contract)

    health_report = load_json(Path(args.health_report))
    measured_at = str(health_report.get("last_report_run_at") or "")
    result = base_result(args, measured_at)
    blockers: list[str] = result["blockers"]
    result["source_fingerprint"] = expected_source_fingerprint
    result["target_fingerprint"] = expected_target_fingerprint
    result["source_binding_fingerprint"] = standby_binding_fingerprint("source", expected_source_label)
    result["target_binding_fingerprint"] = standby_binding_fingerprint("target", expected_target_label)
    result["manifest_fingerprint"] = contract.get("source_manifest", {}).get("schema_fingerprint")
    result["relay_contract_fingerprint"] = "sha256:" + canonical_sha256(contract)[:24]
    result["required_table_count"] = len(tables)
    if expected_source_label != EXPECTED_SOURCE_LABEL:
        blockers.append("source_policy_mismatch")
    if expected_target_label != EXPECTED_TARGET_LABEL:
        blockers.append("target_policy_mismatch")
    if target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if safety.get("backend_postgresql_remains_primary") is not True:
        blockers.append("backend_primary_policy_mismatch")
    if safety.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked")

    telemetry_age = seconds_between(args.now_utc, measured_at)
    if telemetry_age is None:
        blockers.append("telemetry_measurement_time_malformed")
    elif telemetry_age < -60:
        blockers.append("telemetry_measurement_time_from_future")
    elif telemetry_age > args.max_telemetry_age_seconds:
        blockers.append("telemetry_stale")
    result["telemetry_age_seconds"] = telemetry_age

    try:
        relay_report = extract_relay_report(health_report)
    except ValueError as exc:
        relay_report = None
        blockers.append(str(exc))

    if relay_report is None:
        blockers.append("relay_telemetry_unavailable")
    else:
        if relay_report.get("safe_metadata_only") is not True:
            blockers.append("relay_telemetry_not_safe_metadata")
        relay_checked_at = str(relay_report.get("checked_at_utc") or "")
        result["relay_checked_at_utc"] = relay_checked_at or None
        relay_age = seconds_between(args.now_utc, relay_checked_at)
        if relay_age is None:
            blockers.append("relay_comparison_time_malformed")
        elif relay_age < -60:
            blockers.append("relay_comparison_time_from_future")
        elif relay_age > args.max_telemetry_age_seconds:
            blockers.append("relay_comparison_stale")
        result["relay_comparison_age_seconds"] = relay_age
        observed_source_label = str(relay_report.get("source_label") or "")
        observed_target_label = str(relay_report.get("target_label") or "")
        observed_source_fingerprint = safe_fingerprint("source", observed_source_label, contract_id, contract_version)
        observed_target_fingerprint = safe_fingerprint("target", observed_target_label, contract_id, contract_version)
        if observed_source_fingerprint != expected_source_fingerprint:
            blockers.append("source_fingerprint_mismatch")
        if observed_target_fingerprint != expected_target_fingerprint:
            blockers.append("target_fingerprint_mismatch")
        post_sync = relay_report.get("post_sync", {})
        result["post_sync_status"] = str(post_sync.get("status") or "unknown") if isinstance(post_sync, dict) else "unknown"
        try:
            checks = table_checks_by_name(relay_report)
        except ValueError as exc:
            checks = {}
            blockers.append(str(exc))
        table_results = [evaluate_table(name, checks.get(name)) for name in tables]
        result["tables"] = table_results
        result["passed_table_count"] = sum(1 for table in table_results if table["status"] == "PASS")
        result["failed_table_count"] = sum(1 for table in table_results if table["status"] == "FAIL")
        if result["failed_table_count"]:
            blockers.append("table_parity_failed")

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
        summary_lines = [
            "# Supabase Standby Required-Table Parity Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Required tables: `{result['required_table_count']}`",
            f"- Passed tables: `{result['passed_table_count']}`",
            f"- Failed tables: `{result['failed_table_count']}`",
            f"- Measured at: `{result['measured_at_utc']}`",
            f"- Expires at: `{result['expires_at_utc']}`",
            f"- Manifest fingerprint: `{result.get('manifest_fingerprint')}`",
            f"- Source fingerprint: `{result.get('source_fingerprint')}`",
            f"- Target fingerprint: `{result.get('target_fingerprint')}`",
            f"- Blockers: `{', '.join(result.get('blockers', [])) or 'none'}`",
        ]
        Path(summary).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-report", required=True)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--failover-attempt-id", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--max-telemetry-age-seconds", type=int, default=MAX_TELEMETRY_AGE_SECONDS)
    parser.add_argument("--result-ttl-seconds", type=int, default=RESULT_TTL_SECONDS)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    if args.max_telemetry_age_seconds < 1 or args.max_telemetry_age_seconds > 900:
        raise SystemExit("max_telemetry_age_seconds_out_of_bounds")
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
