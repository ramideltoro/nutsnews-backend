#!/usr/bin/env python3
"""Evaluate standby schema compatibility from safe relay metadata."""

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
GATE_NAME = "supabase_standby_schema_compatibility"
ISSUE = "ramideltoro/nutsnews#524"
EPIC = "ramideltoro/nutsnews#521"
MAX_TELEMETRY_AGE_SECONDS = 300
RESULT_TTL_SECONDS = 300
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REVISION_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"
SCHEMA_DIFF_LIMIT = 25


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


def validate_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise ValueError("invalid_failover_attempt_id")


def validate_revision(value: str) -> None:
    if not REVISION_RE.fullmatch(value):
        raise ValueError("invalid_candidate_application_revision")


def extract_named_items(value: Any, *, container_field: str = "items") -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get(container_field), list):
        value = value[container_field]
    if not isinstance(value, list):
        raise ValueError("candidate_manifest_structural_objects_malformed")
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"]
        else:
            raise ValueError("candidate_manifest_structural_objects_malformed")
        names.append(name)
    return sorted(names)


def required_tables(contract: dict[str, Any]) -> list[str]:
    tables = contract.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("manifest_tables_missing")
    names: list[str] = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            raise ValueError("manifest_tables_malformed")
        names.append(str(table["name"]))
    return sorted(names)


def required_sequences(contract: dict[str, Any]) -> dict[str, dict[str, str]]:
    sequences = contract.get("sequences", [])
    if not isinstance(sequences, list):
        raise ValueError("manifest_sequences_malformed")
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


def contract_manifest_summary(contract: dict[str, Any]) -> dict[str, Any]:
    fingerprint = contract.get("manifest_schema_fingerprint") or contract.get("source_manifest", {}).get("schema_fingerprint")
    if not isinstance(fingerprint, str):
        raise ValueError("manifest_schema_fingerprint_missing")
    tables = required_tables(contract)
    sequences = sorted(required_sequences(contract))
    functions = extract_named_items(contract.get("functions"))
    views = extract_named_items(contract.get("views"))
    return {
        "source": "relay_contract",
        "manifest_version": contract.get("version") if isinstance(contract.get("version"), int) else None,
        "schema_fingerprint": fingerprint,
        "migration_head": None,
        "migration_source_fingerprint": None,
        "table_count": len(tables),
        "table_set_fingerprint": digest_object(tables),
        "sequence_count": len(sequences),
        "sequence_set_fingerprint": digest_object(sequences),
        "function_count": len(functions),
        "view_count": len(views),
        "blockers": [],
        "safe_metadata_only": True,
    }


def candidate_manifest_summary(path: str, contract: dict[str, Any]) -> dict[str, Any]:
    if not path:
        return contract_manifest_summary(contract)
    manifest = load_json(Path(path), missing="candidate_manifest_unavailable", malformed="candidate_manifest_malformed")
    fingerprint = manifest.get("schemaFingerprint") or manifest.get("schema_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("candidate_manifest_fingerprint_missing")

    replication = manifest.get("replication", {})
    if not isinstance(replication, dict):
        raise ValueError("candidate_manifest_structural_objects_malformed")
    candidate_tables = extract_named_items(replication.get("tables"))
    candidate_sequences = extract_named_items(manifest.get("sequences"))
    candidate_functions = extract_named_items(manifest.get("functions"))
    candidate_views = extract_named_items(manifest.get("views"))

    expected_tables = required_tables(contract)
    expected_sequences = sorted(required_sequences(contract))
    expected_fingerprint = contract.get("manifest_schema_fingerprint") or contract.get("source_manifest", {}).get("schema_fingerprint")
    blockers: list[str] = []
    if fingerprint != expected_fingerprint:
        blockers.append("candidate_manifest_fingerprint_mismatch")
    if candidate_tables != expected_tables:
        blockers.append("candidate_manifest_table_set_mismatch")
    if candidate_sequences != expected_sequences:
        blockers.append("candidate_manifest_sequence_set_mismatch")

    safety = manifest.get("safety", {})
    if not isinstance(safety, dict):
        blockers.append("candidate_manifest_safety_malformed")
    else:
        if safety.get("existingProductionSupabaseProject") is not True:
            blockers.append("candidate_manifest_existing_supabase_not_confirmed")
        if safety.get("createNewSupabaseProject") is not False:
            blockers.append("candidate_manifest_new_supabase_project_not_forbidden")
        if safety.get("createNutsnewsStandbyDatabase") is not False:
            blockers.append("candidate_manifest_standby_database_not_forbidden")
        if safety.get("appWorkerSupabaseWritesBeforeApprovedFailover") is not False:
            blockers.append("candidate_manifest_supabase_writes_not_blocked")
        if safety.get("safeMetadataOnly") is not True:
            blockers.append("candidate_manifest_not_safe_metadata")

    if candidate_functions:
        blockers.append("required_function_validation_unavailable")
    if candidate_views:
        blockers.append("required_view_validation_unavailable")

    source = manifest.get("source", {}) if isinstance(manifest.get("source"), dict) else {}
    return {
        "source": "candidate_standby_manifest",
        "manifest_version": manifest.get("manifestVersion") if isinstance(manifest.get("manifestVersion"), int) else None,
        "schema_fingerprint": fingerprint,
        "migration_head": source.get("migrationHead") if isinstance(source.get("migrationHead"), str) else None,
        "migration_source_fingerprint": source.get("migrationSourceFingerprint")
        if isinstance(source.get("migrationSourceFingerprint"), str)
        else None,
        "table_count": len(candidate_tables),
        "table_set_fingerprint": digest_object(candidate_tables),
        "sequence_count": len(candidate_sequences),
        "sequence_set_fingerprint": digest_object(candidate_sequences),
        "function_count": len(candidate_functions),
        "view_count": len(candidate_views),
        "blockers": sorted(set(blockers)),
        "safe_metadata_only": True,
    }


def checks_by_id(relay_report: dict[str, Any], section_name: str) -> dict[str, dict[str, Any]]:
    section = relay_report.get(section_name, {})
    checks = section.get("checks") if isinstance(section, dict) else None
    if not isinstance(checks, list):
        raise ValueError(f"{section_name}_checks_missing")
    by_id: dict[str, dict[str, Any]] = {}
    for check in checks:
        if isinstance(check, dict) and isinstance(check.get("id"), str):
            by_id[str(check["id"])] = check
    return by_id


def check_blockers(check: dict[str, Any] | None, *, missing: str, prefix: str) -> list[str]:
    if check is None:
        return [missing]
    blockers: list[str] = []
    if check.get("status") != "pass":
        blockers.append(f"{prefix}_status_not_pass")
    errors = check.get("errors")
    if isinstance(errors, dict) and errors:
        blockers.append(f"{prefix}_metadata_unavailable")
        blockers.extend(str(key) for key in sorted(errors))
    reasons = check.get("reasons")
    if isinstance(reasons, list):
        blockers.extend(str(reason) for reason in reasons if reason)
    return sorted(set(blockers))


def bounded_diff(diff: Any) -> dict[str, Any] | None:
    if not isinstance(diff, dict):
        return None
    bounded: dict[str, Any] = {}
    for section in ("columns", "constraints", "indexes"):
        item = diff.get(section)
        if not isinstance(item, dict):
            continue
        bounded[section] = {
            "missing_in_target_count": item.get("missing_in_target_count"),
            "extra_in_target_count": item.get("extra_in_target_count"),
            "different_count": item.get("different_count"),
            "missing_in_target": list(item.get("missing_in_target", []))[:SCHEMA_DIFF_LIMIT],
            "extra_in_target": list(item.get("extra_in_target", []))[:SCHEMA_DIFF_LIMIT],
            "different": list(item.get("different", []))[:SCHEMA_DIFF_LIMIT],
            "truncated": bool(item.get("truncated")),
        }
    return bounded or None


def evaluate_schema_check(check: dict[str, Any] | None, expected_manifest_fingerprint: str | None) -> dict[str, Any]:
    if check is None:
        return {
            "status": "FAIL",
            "blockers": ["schema_check_missing"],
            "safe_metadata_only": True,
        }

    blockers = check_blockers(check, missing="schema_check_missing", prefix="schema")
    source_schema = check.get("source_schema_sha256")
    target_schema = check.get("target_schema_sha256")
    source_contract = check.get("source_migration_contract_sha256")
    target_contract = check.get("target_migration_contract_sha256")
    manifest_schema = check.get("manifest_schema_fingerprint")

    if not isinstance(source_schema, str) or not isinstance(target_schema, str):
        blockers.append("schema_fingerprint_missing")
    elif source_schema != target_schema:
        blockers.append("schema_fingerprint_mismatch")
    if not isinstance(source_contract, str) or not isinstance(target_contract, str):
        blockers.append("migration_contract_fingerprint_missing")
    elif source_contract != target_contract:
        blockers.append("migration_contract_fingerprint_mismatch")
    if not isinstance(manifest_schema, str):
        blockers.append("manifest_schema_fingerprint_missing")
    elif expected_manifest_fingerprint and manifest_schema != expected_manifest_fingerprint:
        blockers.append("manifest_schema_fingerprint_mismatch")

    result = {
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "source_schema_sha256": source_schema if isinstance(source_schema, str) else None,
        "target_schema_sha256": target_schema if isinstance(target_schema, str) else None,
        "source_migration_contract_sha256": source_contract if isinstance(source_contract, str) else None,
        "target_migration_contract_sha256": target_contract if isinstance(target_contract, str) else None,
        "manifest_schema_fingerprint": manifest_schema if isinstance(manifest_schema, str) else None,
        "source_schema_bytes": check.get("source_schema_bytes") if isinstance(check.get("source_schema_bytes"), int) else None,
        "target_schema_bytes": check.get("target_schema_bytes") if isinstance(check.get("target_schema_bytes"), int) else None,
        "safe_metadata_only": True,
    }
    schema_diff = bounded_diff(check.get("schema_diff"))
    if schema_diff:
        result["schema_diff"] = schema_diff
    migration_diff = check.get("migration_contract_diff")
    if isinstance(migration_diff, dict):
        result["migration_contract_diff"] = {
            key: migration_diff.get(key)
            for key in ("missing_in_target_count", "extra_in_target_count", "different_count", "truncated")
            if key in migration_diff
        }
    return result


def evaluate_identity(name: str, checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    manifest = checks.get(f"manifest-identity.{name}")
    live = checks.get(f"live-identity.{name}")
    blockers = check_blockers(manifest, missing="manifest_identity_check_missing", prefix="manifest_identity")
    blockers.extend(check_blockers(live, missing="live_identity_check_missing", prefix="live_identity"))
    expected_primary_key = live.get("expected_primary_key") if isinstance(live, dict) else None
    source_primary_key = live.get("source_primary_key") if isinstance(live, dict) else None
    target_primary_key = live.get("target_primary_key") if isinstance(live, dict) else None
    return {
        "name": name,
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "expected_primary_key_fingerprint": digest_object(expected_primary_key) if isinstance(expected_primary_key, list) else None,
        "source_primary_key_fingerprint": digest_object(source_primary_key) if isinstance(source_primary_key, list) else None,
        "target_primary_key_fingerprint": digest_object(target_primary_key) if isinstance(target_primary_key, list) else None,
        "source_relation_kind": live.get("source_relation_kind") if isinstance(live, dict) else None,
        "target_relation_kind": live.get("target_relation_kind") if isinstance(live, dict) else None,
        "replica_identity_type": manifest.get("replica_identity_type") if isinstance(manifest, dict) else None,
        "safe_metadata_only": True,
    }


def evaluate_sequence_binding(name: str, expected: dict[str, str], checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    check = checks.get(f"sequence.{name}")
    blockers: list[str] = []
    if check is None:
        blockers.append("sequence_binding_check_missing")
    else:
        errors = check.get("errors")
        if isinstance(errors, dict) and errors:
            blockers.append("sequence_metadata_unavailable")
            blockers.extend(str(key) for key in sorted(errors))
        if check.get("table") != expected["table"] or check.get("column") != expected["column"]:
            blockers.append("sequence_binding_mismatch")
    return {
        "name": name,
        "status": "PASS" if not blockers else "FAIL",
        "blockers": sorted(set(blockers)),
        "binding_fingerprint": digest_object({"table": expected["table"], "column": expected["column"]}),
        "position_safety_status": check.get("status") if isinstance(check, dict) else None,
        "safe_metadata_only": True,
    }


def base_result(args: argparse.Namespace, measured_at: str | None) -> dict[str, Any]:
    measured = measured_at or utc_now()
    return {
        "status": "FAIL",
        "gate": GATE_NAME,
        "issue": ISSUE,
        "epic": EPIC,
        "failover_attempt_id": args.failover_attempt_id,
        "candidate_application_revision": args.candidate_application_revision,
        "repository_revision": args.repository_revision,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.result_ttl_seconds),
        "max_telemetry_age_seconds": args.max_telemetry_age_seconds,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "manifest_fingerprint": None,
        "candidate_manifest": None,
        "relay_contract_fingerprint": None,
        "relay_checked_at_utc": None,
        "relay_schema_age_seconds": None,
        "preflight_status": "unknown",
        "schema": None,
        "required_table_count": 0,
        "passed_identity_count": 0,
        "failed_identity_count": 0,
        "identity_checks": [],
        "required_sequence_count": 0,
        "passed_sequence_binding_count": 0,
        "failed_sequence_binding_count": 0,
        "sequence_bindings": [],
        "required_function_count": 0,
        "required_view_count": 0,
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
    validate_revision(args.candidate_application_revision)
    if args.expected_application_revision:
        validate_revision(args.expected_application_revision)
        if args.candidate_application_revision != args.expected_application_revision:
            raise ValueError("candidate_application_revision_mismatch")

    contract = load_json(Path(args.contract), missing="contract_unavailable", malformed="contract_malformed")
    contract_id = str(contract.get("contract_id") or "unknown")
    contract_version = contract.get("version", "unknown")
    expected_source_label = str(contract.get("source", {}).get("label") or "")
    expected_target_label = str(contract.get("target", {}).get("label") or "")
    safety = contract.get("safety", {})
    target = contract.get("target", {})
    expected_source_fingerprint = safe_fingerprint("source", expected_source_label, contract_id, contract_version)
    expected_target_fingerprint = safe_fingerprint("target", expected_target_label, contract_id, contract_version)
    table_names = required_tables(contract)
    sequence_contract = required_sequences(contract)
    manifest_fingerprint = contract.get("manifest_schema_fingerprint") or contract.get("source_manifest", {}).get("schema_fingerprint")
    if not isinstance(manifest_fingerprint, str):
        manifest_fingerprint = None

    candidate_manifest = candidate_manifest_summary(args.candidate_standby_manifest, contract)

    health_report = load_json(Path(args.health_report))
    measured_at = str(health_report.get("last_report_run_at") or "")
    result = base_result(args, measured_at)
    blockers: list[str] = result["blockers"]
    result["source_fingerprint"] = expected_source_fingerprint
    result["target_fingerprint"] = expected_target_fingerprint
    result["manifest_fingerprint"] = manifest_fingerprint
    result["candidate_manifest"] = candidate_manifest
    result["relay_contract_fingerprint"] = "sha256:" + canonical_sha256(contract)[:24]
    result["required_table_count"] = len(table_names)
    result["required_sequence_count"] = len(sequence_contract)
    result["required_function_count"] = int(candidate_manifest["function_count"])
    result["required_view_count"] = int(candidate_manifest["view_count"])
    blockers.extend(candidate_manifest["blockers"])

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
            blockers.append("relay_schema_time_malformed")
        elif relay_age < -60:
            blockers.append("relay_schema_time_from_future")
        elif relay_age > args.max_telemetry_age_seconds:
            blockers.append("relay_schema_stale")
        result["relay_schema_age_seconds"] = relay_age

        observed_source_label = str(relay_report.get("source_label") or "")
        observed_target_label = str(relay_report.get("target_label") or "")
        observed_source_fingerprint = safe_fingerprint("source", observed_source_label, contract_id, contract_version)
        observed_target_fingerprint = safe_fingerprint("target", observed_target_label, contract_id, contract_version)
        if observed_source_fingerprint != expected_source_fingerprint:
            blockers.append("source_fingerprint_mismatch")
        if observed_target_fingerprint != expected_target_fingerprint:
            blockers.append("target_fingerprint_mismatch")

        preflight = relay_report.get("preflight", {})
        result["preflight_status"] = str(preflight.get("status") or "unknown") if isinstance(preflight, dict) else "unknown"
        try:
            preflight_checks = checks_by_id(relay_report, "preflight")
        except ValueError as exc:
            preflight_checks = {}
            blockers.append(str(exc))
        try:
            post_sync_checks = checks_by_id(relay_report, "post_sync")
        except ValueError as exc:
            post_sync_checks = {}
            blockers.append(str(exc))

        schema_result = evaluate_schema_check(preflight_checks.get("schema-fingerprint"), manifest_fingerprint)
        result["schema"] = schema_result
        if schema_result["status"] == "FAIL":
            blockers.append("schema_compatibility_failed")

        identities = [evaluate_identity(name, preflight_checks) for name in table_names]
        result["identity_checks"] = identities
        result["passed_identity_count"] = sum(1 for item in identities if item["status"] == "PASS")
        result["failed_identity_count"] = sum(1 for item in identities if item["status"] == "FAIL")
        if result["failed_identity_count"]:
            blockers.append("identity_compatibility_failed")

        sequence_bindings = [
            evaluate_sequence_binding(name, expected, post_sync_checks)
            for name, expected in sequence_contract.items()
        ]
        result["sequence_bindings"] = sequence_bindings
        result["passed_sequence_binding_count"] = sum(1 for item in sequence_bindings if item["status"] == "PASS")
        result["failed_sequence_binding_count"] = sum(1 for item in sequence_bindings if item["status"] == "FAIL")
        if result["failed_sequence_binding_count"]:
            blockers.append("sequence_binding_failed")

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
            "# Supabase Standby Schema Compatibility Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Candidate revision: `{result['candidate_application_revision']}`",
            f"- Repository revision: `{result.get('repository_revision')}`",
            f"- Required tables: `{result['required_table_count']}`",
            f"- Passed identity checks: `{result['passed_identity_count']}`",
            f"- Failed identity checks: `{result['failed_identity_count']}`",
            f"- Required sequence bindings: `{result['required_sequence_count']}`",
            f"- Passed sequence bindings: `{result['passed_sequence_binding_count']}`",
            f"- Failed sequence bindings: `{result['failed_sequence_binding_count']}`",
            f"- Measured at: `{result['measured_at_utc']}`",
            f"- Expires at: `{result['expires_at_utc']}`",
            f"- Relay schema age seconds: `{result.get('relay_schema_age_seconds')}`",
            f"- Manifest fingerprint: `{result.get('manifest_fingerprint')}`",
            f"- Candidate manifest fingerprint: `{(result.get('candidate_manifest') or {}).get('schema_fingerprint')}`",
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
    parser.add_argument("--candidate-application-revision", required=True)
    parser.add_argument("--candidate-standby-manifest", default="")
    parser.add_argument("--expected-application-revision", default="")
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
