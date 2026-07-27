#!/usr/bin/env python3
"""Evaluate standby sequence safety from safe relay metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-sync-relay.json"
GATE_NAME = "supabase_standby_sequence_safety"
ISSUE = "ramideltoro/nutsnews#525"
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


def load_json(path: Path, *, missing: str = "telemetry_unavailable", malformed: str = "telemetry_malformed") -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(missing) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(malformed) from exc
    if not isinstance(data, dict):
        raise ValueError(malformed)
    return data


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def safe_fingerprint(kind: str, label: str, contract_id: str, contract_version: Any) -> str:
    payload = f"{kind}|{label}|{contract_id}|{contract_version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def standby_binding_fingerprint(kind: str, label: str) -> str:
    payload = f"supabase-standby-binding-v1|{kind}|{label}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


def digest_object(value: Any) -> str:
    return "sha256:" + canonical_sha256(value)[:24]


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


def bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def required_sequences(contract: dict[str, Any]) -> dict[str, dict[str, str]]:
    sequences = contract.get("sequences", [])
    if not isinstance(sequences, list) or not sequences:
        raise ValueError("manifest_sequences_missing")
    by_name: dict[str, dict[str, str]] = {}
    for sequence in sequences:
        if not isinstance(sequence, dict):
            raise ValueError("manifest_sequences_malformed")
        name = sequence.get("name")
        table = sequence.get("table")
        column = sequence.get("column")
        if not isinstance(name, str) or not isinstance(table, str) or not isinstance(column, str):
            raise ValueError("manifest_sequences_malformed")
        by_name[name] = {"name": name, "table": table, "column": column}
    return dict(sorted(by_name.items()))


def checks_by_id(relay_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    post_sync = relay_report.get("post_sync", {})
    checks = post_sync.get("checks") if isinstance(post_sync, dict) else None
    if not isinstance(checks, list):
        raise ValueError("post_sync_checks_missing")
    by_id: dict[str, dict[str, Any]] = {}
    for check in checks:
        if isinstance(check, dict) and isinstance(check.get("id"), str):
            by_id[str(check["id"])] = check
    return by_id


def prefixed_int(check: dict[str, Any], prefix: str, field: str, blockers: list[str]) -> int | None:
    value = int_or_none(check.get(f"{prefix}_{field}"))
    if value is None:
        blockers.append(f"{prefix}_{field}_missing")
    return value


def prefixed_bool(check: dict[str, Any], prefix: str, field: str, blockers: list[str]) -> bool | None:
    value = bool_or_none(check.get(f"{prefix}_{field}"))
    if value is None:
        blockers.append(f"{prefix}_{field}_missing")
    return value


def sequence_side_blockers(check: dict[str, Any], prefix: str, expected: dict[str, str]) -> list[str]:
    blockers: list[str] = []
    next_value = prefixed_int(check, prefix, "next_value", blockers)
    increment_by = prefixed_int(check, prefix, "increment_by", blockers)
    min_value = prefixed_int(check, prefix, "sequence_min_value", blockers)
    max_value = prefixed_int(check, prefix, "sequence_max_value", blockers)
    cycle = prefixed_bool(check, prefix, "sequence_cycle", blockers)
    prefixed_bool(check, prefix, "is_called", blockers)
    owned_by_count = prefixed_int(check, prefix, "owned_by_count", blockers)
    expected_binding_matches = prefixed_bool(check, prefix, "expected_binding_matches", blockers)
    owned_by_table = check.get(f"{prefix}_owned_by_table")
    owned_by_column = check.get(f"{prefix}_owned_by_column")

    if increment_by is not None and increment_by != 1:
        blockers.append(f"{prefix}_sequence_increment_not_one")
    if cycle is True:
        blockers.append(f"{prefix}_sequence_cycle_enabled")
    if owned_by_count == 0:
        blockers.append(f"{prefix}_sequence_unowned")
    elif owned_by_count is not None and owned_by_count > 1:
        blockers.append(f"{prefix}_sequence_multiple_owners")
    if expected_binding_matches is not True:
        blockers.append(f"{prefix}_sequence_misbound")
    if (
        isinstance(owned_by_table, str)
        and isinstance(owned_by_column, str)
        and (owned_by_table != expected["table"] or owned_by_column != expected["column"])
    ):
        blockers.append(f"{prefix}_sequence_misbound")
    if next_value is not None and min_value is not None and max_value is not None:
        if next_value < min_value or next_value > max_value:
            blockers.append(f"{prefix}_sequence_exhausted")
    return sorted(set(blockers))


def evaluate_sequence(name: str, expected: dict[str, str], check: dict[str, Any] | None) -> dict[str, Any]:
    if check is None:
        return {
            "name": name,
            "table": expected["table"],
            "column": expected["column"],
            "status": "FAIL",
            "blockers": ["sequence_check_missing"],
            "safe_metadata_only": True,
        }

    blockers: list[str] = []
    if check.get("status") != "pass":
        blockers.append("sequence_status_not_pass")
    if check.get("sensitivity") != "sequence_metadata_only":
        blockers.append("sequence_metadata_sensitivity_mismatch")
    if check.get("name") != name or check.get("table") != expected["table"] or check.get("column") != expected["column"]:
        blockers.append("sequence_contract_mismatch")
    errors = check.get("errors")
    if isinstance(errors, dict) and errors:
        blockers.append("sequence_metadata_unavailable")
        blockers.extend(str(key) for key in sorted(errors))
    reasons = check.get("reasons")
    if isinstance(reasons, list):
        blockers.extend(str(reason) for reason in reasons if reason)

    blockers.extend(sequence_side_blockers(check, "source", expected))
    blockers.extend(sequence_side_blockers(check, "target", expected))

    source_next = int_or_none(check.get("source_next_value"))
    target_next = int_or_none(check.get("target_next_value"))
    source_max_id = int_or_none(check.get("source_max_id"))
    target_max_id = int_or_none(check.get("target_max_id"))
    source_increment = int_or_none(check.get("source_increment_by"))
    target_increment = int_or_none(check.get("target_increment_by"))
    if source_next is not None and source_max_id is not None and source_next <= source_max_id:
        blockers.append("source_next_value_not_above_source_max_id")
    if target_next is not None and target_max_id is not None and target_next <= target_max_id:
        blockers.append("target_next_value_not_above_target_max_id")
    if target_next is not None and source_max_id is not None and target_next <= source_max_id:
        blockers.append("target_next_value_not_above_source_max_id")
    if target_next is not None and source_next is not None and target_next < source_next:
        blockers.append("target_next_value_lt_source_next_value")
    if source_increment is not None and target_increment is not None and source_increment != target_increment:
        blockers.append("sequence_increment_mismatch")

    return {
        "name": name,
        "table": expected["table"],
        "column": expected["column"],
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "source_last_value": int_or_none(check.get("source_last_value")),
        "source_next_value": source_next,
        "source_is_called": bool_or_none(check.get("source_is_called")),
        "source_increment_by": source_increment,
        "source_sequence_min_value": int_or_none(check.get("source_sequence_min_value")),
        "source_sequence_max_value": int_or_none(check.get("source_sequence_max_value")),
        "source_sequence_cycle": bool_or_none(check.get("source_sequence_cycle")),
        "source_max_id": source_max_id,
        "target_last_value": int_or_none(check.get("target_last_value")),
        "target_next_value": target_next,
        "target_is_called": bool_or_none(check.get("target_is_called")),
        "target_increment_by": target_increment,
        "target_sequence_min_value": int_or_none(check.get("target_sequence_min_value")),
        "target_sequence_max_value": int_or_none(check.get("target_sequence_max_value")),
        "target_sequence_cycle": bool_or_none(check.get("target_sequence_cycle")),
        "target_max_id": target_max_id,
        "binding_fingerprint": digest_object({"table": expected["table"], "column": expected["column"]}),
        "safe_metadata_only": True,
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
        "repository_revision": args.repository_revision,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.result_ttl_seconds),
        "max_telemetry_age_seconds": args.max_telemetry_age_seconds,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "source_binding_fingerprint": None,
        "target_binding_fingerprint": None,
        "manifest_fingerprint": None,
        "relay_contract_fingerprint": None,
        "relay_checked_at_utc": None,
        "relay_sequence_age_seconds": None,
        "post_sync_status": "unknown",
        "required_sequence_count": 0,
        "passed_sequence_count": 0,
        "failed_sequence_count": 0,
        "sequences": [],
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
    contract = load_json(Path(args.contract), missing="contract_unavailable", malformed="contract_malformed")
    contract_id = str(contract.get("contract_id") or "unknown")
    contract_version = contract.get("version", "unknown")
    expected_source_label = str(contract.get("source", {}).get("label") or "")
    expected_target_label = str(contract.get("target", {}).get("label") or "")
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    expected_source_fingerprint = safe_fingerprint("source", expected_source_label, contract_id, contract_version)
    expected_target_fingerprint = safe_fingerprint("target", expected_target_label, contract_id, contract_version)
    sequence_contract = required_sequences(contract)

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
    result["required_sequence_count"] = len(sequence_contract)

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
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary") is not True:
        blockers.append("backend_primary_policy_mismatch")
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
            blockers.append("relay_sequence_time_malformed")
        elif relay_age < -60:
            blockers.append("relay_sequence_time_from_future")
        elif relay_age > args.max_telemetry_age_seconds:
            blockers.append("relay_sequence_stale")
        result["relay_sequence_age_seconds"] = relay_age

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
            checks = checks_by_id(relay_report)
        except ValueError as exc:
            checks = {}
            blockers.append(str(exc))
        sequence_results = [
            evaluate_sequence(name, expected, checks.get(f"sequence.{name}"))
            for name, expected in sequence_contract.items()
        ]
        result["sequences"] = sequence_results
        result["passed_sequence_count"] = sum(1 for item in sequence_results if item["status"] == "PASS")
        result["failed_sequence_count"] = sum(1 for item in sequence_results if item["status"] == "FAIL")
        if result["failed_sequence_count"]:
            blockers.append("sequence_safety_failed")

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
            "# Supabase Standby Sequence Safety Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Repository revision: `{result.get('repository_revision')}`",
            f"- Required sequences: `{result['required_sequence_count']}`",
            f"- Passed sequences: `{result['passed_sequence_count']}`",
            f"- Failed sequences: `{result['failed_sequence_count']}`",
            f"- Measured at: `{result['measured_at_utc']}`",
            f"- Expires at: `{result['expires_at_utc']}`",
            f"- Relay sequence age seconds: `{result.get('relay_sequence_age_seconds')}`",
            f"- Manifest fingerprint: `{result.get('manifest_fingerprint')}`",
            f"- Relay contract fingerprint: `{result.get('relay_contract_fingerprint')}`",
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
    parser.add_argument("--repository-revision", default="")
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
