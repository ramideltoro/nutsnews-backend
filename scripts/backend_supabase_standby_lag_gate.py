#!/usr/bin/env python3
"""Evaluate the Supabase standby lag promotion gate from safe relay telemetry."""

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
GATE_NAME = "supabase_standby_lag"
ISSUE = "ramideltoro/nutsnews#522"
EPIC = "ramideltoro/nutsnews#521"
MAX_ALLOWED_LAG_SECONDS = 30
MAX_TELEMETRY_AGE_SECONDS = 120
RESULT_TTL_SECONDS = 300
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


def safe_fingerprint(kind: str, label: str, contract_id: str, contract_version: Any) -> str:
    payload = f"{kind}|{label}|{contract_id}|{contract_version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def standby_binding_fingerprint(kind: str, label: str) -> str:
    payload = f"supabase-standby-binding-v1|{kind}|{label}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def find_check(health_report: dict[str, Any], name: str) -> dict[str, Any] | None:
    checks = health_report.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if isinstance(check, dict) and check.get("name") == name:
            return check
    return None


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
        "max_allowed_lag_seconds": args.max_allowed_lag_seconds,
        "max_telemetry_age_seconds": args.max_telemetry_age_seconds,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "source_binding_fingerprint": None,
        "target_binding_fingerprint": None,
        "observed_lag_seconds": None,
        "relay_health_status": "unknown",
        "relay_timer_state": "unknown",
        "relay_service_result": "unknown",
        "blockers": [],
        "backend_postgresql_remains_primary": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }


def validate_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise ValueError("invalid_failover_attempt_id")


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.failover_attempt_id)
    contract = load_json(Path(args.contract))
    expected_source_label = str(contract.get("source", {}).get("label") or "")
    expected_target_label = str(contract.get("target", {}).get("label") or "")
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    contract_id = str(contract.get("contract_id") or "unknown")
    contract_version = contract.get("version", "unknown")
    expected_source_fingerprint = safe_fingerprint("source", expected_source_label, contract_id, contract_version)
    expected_target_fingerprint = safe_fingerprint("target", expected_target_label, contract_id, contract_version)

    health_report = load_json(Path(args.health_report))
    measured_at = str(health_report.get("last_report_run_at") or "")
    result = base_result(args, measured_at)
    result["source_fingerprint"] = expected_source_fingerprint
    result["target_fingerprint"] = expected_target_fingerprint
    result["source_binding_fingerprint"] = standby_binding_fingerprint("source", expected_source_label)
    result["target_binding_fingerprint"] = standby_binding_fingerprint("target", expected_target_label)
    blockers: list[str] = result["blockers"]

    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary") is not True:
        blockers.append("backend_primary_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("target_is_existing_production_supabase") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(safety, dict) or safety.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked")

    telemetry_age = seconds_between(args.now_utc, measured_at)
    if telemetry_age is None:
        blockers.append("telemetry_measurement_time_malformed")
    elif telemetry_age < -60:
        blockers.append("telemetry_measurement_time_from_future")
    elif telemetry_age > args.max_telemetry_age_seconds:
        blockers.append("telemetry_stale")
    result["telemetry_age_seconds"] = telemetry_age

    relay_check = find_check(health_report, "supabase_sync_relay_health")
    if relay_check is None:
        blockers.append("relay_health_telemetry_missing")
    else:
        relay_status = str(relay_check.get("status") or "unknown")
        result["relay_health_status"] = relay_status
        result["relay_timer_state"] = str(relay_check.get("timer_state") or "unknown")
        result["relay_service_result"] = str(relay_check.get("service_result") or "unknown")
        lag_seconds = int_or_none(relay_check.get("lag_seconds"))
        result["observed_lag_seconds"] = lag_seconds
        if lag_seconds is None:
            blockers.append("lag_seconds_missing")
        elif lag_seconds > args.max_allowed_lag_seconds:
            blockers.append("lag_exceeds_threshold")
        if relay_status != "healthy":
            blockers.append("relay_unhealthy")
        relay_blockers = relay_check.get("blockers")
        if isinstance(relay_blockers, list) and relay_blockers:
            result["relay_health_blockers"] = sorted(str(item) for item in relay_blockers)
        if relay_check.get("safe_metadata_only") is not True:
            blockers.append("relay_health_not_safe_metadata")

    try:
        relay_report = extract_relay_report(health_report)
    except ValueError as exc:
        relay_report = None
        blockers.append(str(exc))

    if relay_report is None:
        blockers.append("relay_telemetry_unavailable")
    else:
        observed_source_label = str(relay_report.get("source_label") or "")
        observed_target_label = str(relay_report.get("target_label") or "")
        observed_source_fingerprint = safe_fingerprint("source", observed_source_label, contract_id, contract_version)
        observed_target_fingerprint = safe_fingerprint("target", observed_target_label, contract_id, contract_version)
        if observed_source_fingerprint != expected_source_fingerprint:
            blockers.append("source_fingerprint_mismatch")
        if observed_target_fingerprint != expected_target_fingerprint:
            blockers.append("target_fingerprint_mismatch")
        if relay_report.get("safe_metadata_only") is not True:
            blockers.append("relay_telemetry_not_safe_metadata")

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
            "# Supabase Standby Lag Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Observed lag seconds: `{result.get('observed_lag_seconds')}`",
            f"- Max allowed lag seconds: `{result['max_allowed_lag_seconds']}`",
            f"- Measured at: `{result['measured_at_utc']}`",
            f"- Expires at: `{result['expires_at_utc']}`",
            f"- Source fingerprint: `{result.get('source_fingerprint')}`",
            f"- Target fingerprint: `{result.get('target_fingerprint')}`",
            f"- Relay health: `{result.get('relay_health_status')}`",
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
    parser.add_argument("--max-allowed-lag-seconds", type=int, default=MAX_ALLOWED_LAG_SECONDS)
    parser.add_argument("--max-telemetry-age-seconds", type=int, default=MAX_TELEMETRY_AGE_SECONDS)
    parser.add_argument("--result-ttl-seconds", type=int, default=RESULT_TTL_SECONDS)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    if args.max_allowed_lag_seconds != MAX_ALLOWED_LAG_SECONDS:
        raise SystemExit("max_allowed_lag_seconds_is_fixed_at_30")
    if args.max_telemetry_age_seconds < 1 or args.max_telemetry_age_seconds > 300:
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
