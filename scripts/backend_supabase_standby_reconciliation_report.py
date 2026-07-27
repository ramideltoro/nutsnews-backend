#!/usr/bin/env python3
"""Aggregate safe standby reconciliation gates for issue #501."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPORT_ID = "backend-supabase-standby-reconciliation-report"
ISSUE = "ramideltoro/nutsnews#501"
EPIC = "ramideltoro/nutsnews#223"
GATE_EPIC = "ramideltoro/nutsnews#521"
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REVISION_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
GATE_DEFS = {
    "parity": {
        "issue": "ramideltoro/nutsnews#523",
        "path_arg": "parity_gate",
        "status_blocker": "parity_gate_failed",
        "attempt_blocker": "parity_gate_attempt_mismatch",
        "expired_blocker": "parity_gate_expired",
    },
    "schema": {
        "issue": "ramideltoro/nutsnews#524",
        "path_arg": "schema_gate",
        "status_blocker": "schema_gate_failed",
        "attempt_blocker": "schema_gate_attempt_mismatch",
        "expired_blocker": "schema_gate_expired",
    },
    "sequence": {
        "issue": "ramideltoro/nutsnews#525",
        "path_arg": "sequence_gate",
        "status_blocker": "sequence_gate_failed",
        "attempt_blocker": "sequence_gate_attempt_mismatch",
        "expired_blocker": "sequence_gate_expired",
    },
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def load_json(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"gate_artifact_unavailable:{Path(path).name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"gate_artifact_malformed:{Path(path).name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"gate_artifact_malformed:{Path(path).name}")
    return data


def validate_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise ValueError("invalid_reconciliation_attempt_id")


def validate_revision(value: str) -> None:
    if value and not REVISION_RE.fullmatch(value):
        raise ValueError("invalid_repository_revision")


def status_from_blockers(blockers: list[str]) -> str:
    return "PASS" if not blockers else "FAIL"


def summarize_tables(parity: dict[str, Any]) -> list[dict[str, Any]]:
    tables = parity.get("tables", [])
    if not isinstance(tables, list):
        return []
    summary: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        summary.append(
            {
                "name": table.get("name") if isinstance(table.get("name"), str) else "unknown",
                "status": table.get("status") if isinstance(table.get("status"), str) else "FAIL",
                "source_count": table.get("source_count") if isinstance(table.get("source_count"), int) else None,
                "target_count": table.get("target_count") if isinstance(table.get("target_count"), int) else None,
                "target_lag_rows": table.get("target_lag_rows") if isinstance(table.get("target_lag_rows"), int) else None,
                "blockers": sorted(str(item) for item in table.get("blockers", []) if item)
                if isinstance(table.get("blockers"), list)
                else [],
            }
        )
    return summary


def gate_summary(name: str, result: dict[str, Any], now: datetime, attempt_id: str) -> tuple[dict[str, Any], list[str]]:
    definition = GATE_DEFS[name]
    blockers: list[str] = []
    gate_blockers = result.get("blockers", [])
    if not isinstance(gate_blockers, list):
        gate_blockers = ["gate_blockers_malformed"]
    gate_status = result.get("status")
    if gate_status != "PASS":
        blockers.append(definition["status_blocker"])
    if result.get("issue") != definition["issue"]:
        blockers.append(f"{name}_gate_issue_mismatch")
    if result.get("epic") != GATE_EPIC:
        blockers.append(f"{name}_gate_epic_mismatch")
    if result.get("failover_attempt_id") != attempt_id:
        blockers.append(definition["attempt_blocker"])
    if result.get("safe_metadata_only") is not True:
        blockers.append(f"{name}_gate_not_safe_metadata")
    if result.get("target_is_existing_production_supabase") is not True:
        blockers.append(f"{name}_gate_target_not_existing_production_supabase")
    if result.get("create_new_supabase_project") is not False:
        blockers.append(f"{name}_gate_new_supabase_project_not_forbidden")
    if result.get("create_nutsnews_standby_database") is not False:
        blockers.append(f"{name}_gate_standby_database_not_forbidden")
    if result.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append(f"{name}_gate_supabase_writes_not_blocked")

    expires_at = result.get("expires_at_utc") if isinstance(result.get("expires_at_utc"), str) else None
    expires = parse_utc(expires_at)
    if expires is None:
        blockers.append(f"{name}_gate_expiry_malformed")
    elif expires < now:
        blockers.append(definition["expired_blocker"])

    summary: dict[str, Any] = {
        "status": gate_status if isinstance(gate_status, str) else "FAIL",
        "issue": result.get("issue"),
        "failover_attempt_id": result.get("failover_attempt_id"),
        "measured_at_utc": result.get("measured_at_utc"),
        "expires_at_utc": expires_at,
        "source_fingerprint": result.get("source_fingerprint"),
        "target_fingerprint": result.get("target_fingerprint"),
        "manifest_fingerprint": result.get("manifest_fingerprint"),
        "relay_contract_fingerprint": result.get("relay_contract_fingerprint"),
        "blockers": sorted(str(item) for item in gate_blockers if item),
        "safe_metadata_only": result.get("safe_metadata_only") is True,
    }
    if name == "parity":
        summary.update(
            {
                "required_table_count": result.get("required_table_count", 0),
                "passed_table_count": result.get("passed_table_count", 0),
                "failed_table_count": result.get("failed_table_count", 0),
                "tables": summarize_tables(result),
            }
        )
    elif name == "schema":
        summary.update(
            {
                "candidate_application_revision": result.get("candidate_application_revision"),
                "repository_revision": result.get("repository_revision"),
                "required_table_count": result.get("required_table_count", 0),
                "required_sequence_count": result.get("required_sequence_count", 0),
                "passed_identity_count": result.get("passed_identity_count", 0),
                "failed_identity_count": result.get("failed_identity_count", 0),
                "passed_sequence_binding_count": result.get("passed_sequence_binding_count", 0),
                "failed_sequence_binding_count": result.get("failed_sequence_binding_count", 0),
                "schema_status": (result.get("schema") or {}).get("status") if isinstance(result.get("schema"), dict) else None,
                "schema_blockers": (result.get("schema") or {}).get("blockers", [])
                if isinstance(result.get("schema"), dict)
                else [],
            }
        )
    elif name == "sequence":
        summary.update(
            {
                "repository_revision": result.get("repository_revision"),
                "required_sequence_count": result.get("required_sequence_count", 0),
                "passed_sequence_count": result.get("passed_sequence_count", 0),
                "failed_sequence_count": result.get("failed_sequence_count", 0),
            }
        )
    return summary, sorted(set(blockers))


def required_check_results(gates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    parity = gates["parity"]
    schema = gates["schema"]
    sequence = gates["sequence"]
    table_failures = parity.get("failed_table_count") if isinstance(parity.get("failed_table_count"), int) else 0
    schema_failed = bool(schema.get("blockers") or schema.get("schema_blockers"))
    sequence_failures = sequence.get("failed_sequence_count") if isinstance(sequence.get("failed_sequence_count"), int) else 0
    checks = [
        {
            "id": "required-table-row-count-parity",
            "status": "PASS" if parity.get("status") == "PASS" and table_failures == 0 else "FAIL",
            "source_gate": "parity",
            "blockers": ["table_parity_failed"] if table_failures else [],
        },
        {
            "id": "required-table-row-checksum-parity",
            "status": "PASS" if parity.get("status") == "PASS" and table_failures == 0 else "FAIL",
            "source_gate": "parity",
            "blockers": ["table_parity_failed"] if table_failures else [],
        },
        {
            "id": "schema-fingerprint-compatible",
            "status": "PASS" if schema.get("status") == "PASS" and not schema_failed else "FAIL",
            "source_gate": "schema",
            "blockers": ["schema_compatibility_failed"] if schema_failed else [],
        },
        {
            "id": "required-object-list-compatible",
            "status": "PASS" if schema.get("status") == "PASS" and not schema_failed else "FAIL",
            "source_gate": "schema",
            "blockers": ["required_object_list_failed"] if schema_failed else [],
        },
        {
            "id": "sequence-safety",
            "status": "PASS" if sequence.get("status") == "PASS" and sequence_failures == 0 else "FAIL",
            "source_gate": "sequence",
            "blockers": ["sequence_safety_failed"] if sequence_failures else [],
        },
    ]
    return checks


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.reconciliation_attempt_id)
    validate_revision(args.repository_revision)
    now_dt = parse_utc(args.now_utc)
    if now_dt is None:
        raise ValueError("invalid_now_utc")

    raw_results = {
        "parity": load_json(args.parity_gate),
        "schema": load_json(args.schema_gate),
        "sequence": load_json(args.sequence_gate),
    }
    blockers: list[str] = []
    summaries: dict[str, dict[str, Any]] = {}
    source_fingerprints: set[str] = set()
    target_fingerprints: set[str] = set()
    manifest_fingerprints: set[str] = set()
    relay_contract_fingerprints: set[str] = set()

    for name, raw in raw_results.items():
        summary, gate_blockers = gate_summary(name, raw, now_dt, args.reconciliation_attempt_id)
        summaries[name] = summary
        blockers.extend(gate_blockers)
        for field, bucket in (
            ("source_fingerprint", source_fingerprints),
            ("target_fingerprint", target_fingerprints),
            ("manifest_fingerprint", manifest_fingerprints),
            ("relay_contract_fingerprint", relay_contract_fingerprints),
        ):
            value = summary.get(field)
            if isinstance(value, str) and value:
                bucket.add(value)

    if len(source_fingerprints) != 1:
        blockers.append("source_fingerprint_set_mismatch")
    if len(target_fingerprints) != 1:
        blockers.append("target_fingerprint_set_mismatch")
    if len(manifest_fingerprints) != 1:
        blockers.append("manifest_fingerprint_set_mismatch")
    if len(relay_contract_fingerprints) != 1:
        blockers.append("relay_contract_fingerprint_set_mismatch")

    if args.repository_revision:
        schema_revision = summaries["schema"].get("candidate_application_revision")
        sequence_revision = summaries["sequence"].get("repository_revision")
        if schema_revision != args.repository_revision:
            blockers.append("schema_gate_repository_revision_mismatch")
        if sequence_revision not in ("", args.repository_revision):
            blockers.append("sequence_gate_repository_revision_mismatch")

    checks = required_check_results(summaries)
    failed_required = [check["id"] for check in checks if check["status"] != "PASS"]
    if failed_required:
        blockers.append("required_reconciliation_check_failed")

    blockers = sorted(set(blockers))
    return {
        "status": status_from_blockers(blockers),
        "report_id": REPORT_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "gate_epic": GATE_EPIC,
        "reconciliation_attempt_id": args.reconciliation_attempt_id,
        "repository_revision": args.repository_revision,
        "measured_at_utc": args.now_utc,
        "source_label": EXPECTED_SOURCE_LABEL,
        "target_label": EXPECTED_TARGET_LABEL,
        "target_is_existing_production_supabase": True,
        "backend_postgresql_remains_primary": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
        "consumed_gate_issues": [definition["issue"] for definition in GATE_DEFS.values()],
        "required_checks": checks,
        "failed_required_checks": failed_required,
        "gates": summaries,
        "blockers": blockers,
    }


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "report_id": REPORT_ID,
        "issue": ISSUE,
        "epic": EPIC,
        "gate_epic": GATE_EPIC,
        "reconciliation_attempt_id": args.reconciliation_attempt_id,
        "repository_revision": args.repository_revision,
        "measured_at_utc": args.now_utc,
        "source_label": EXPECTED_SOURCE_LABEL,
        "target_label": EXPECTED_TARGET_LABEL,
        "target_is_existing_production_supabase": True,
        "backend_postgresql_remains_primary": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
        "consumed_gate_issues": [definition["issue"] for definition in GATE_DEFS.values()],
        "required_checks": [],
        "failed_required_checks": [],
        "gates": {},
        "blockers": [blocker],
    }


def write_outputs(result: dict[str, Any], output: str, summary: str) -> None:
    text = json.dumps(result, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    if summary:
        summary_lines = [
            "# Supabase Standby Reconciliation",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['reconciliation_attempt_id']}`",
            f"- Repository revision: `{result.get('repository_revision')}`",
            f"- Failed required checks: `{', '.join(result.get('failed_required_checks', [])) or 'none'}`",
            f"- Blockers: `{', '.join(result.get('blockers', [])) or 'none'}`",
            "- Source: backend PostgreSQL primary.",
            "- Target: existing production Supabase standby.",
            "- Report policy: safe metadata only.",
            "- App/worker Supabase writes remain disabled before approved failover.",
        ]
        Path(summary).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parity-gate", required=True)
    parser.add_argument("--schema-gate", required=True)
    parser.add_argument("--sequence-gate", required=True)
    parser.add_argument("--reconciliation-attempt-id", required=True)
    parser.add_argument("--repository-revision", default="")
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--enforce", action="store_true")
    return parser.parse_args(argv)


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
