#!/usr/bin/env python3
"""Combine Supabase standby gates into a single fail-closed promotion decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-promotion-decision.json"
GATE_NAME = "supabase_standby_promotion_decision"
ISSUE = "ramideltoro/nutsnews#528"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REVISION_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
MAX_EVIDENCE_AGE_SECONDS = 300
DECISION_TTL_SECONDS = 300

REQUIRED_GATES = {
    "lag": {
        "argument": "lag_gate",
        "gate": "supabase_standby_lag",
        "issue": "ramideltoro/nutsnews#522",
        "repository_revision": "optional",
        "candidate_application_revision": "absent",
        "fence_epoch": "absent",
    },
    "parity": {
        "argument": "parity_gate",
        "gate": "supabase_standby_required_table_parity",
        "issue": "ramideltoro/nutsnews#523",
        "repository_revision": "optional",
        "candidate_application_revision": "absent",
        "fence_epoch": "absent",
    },
    "schema": {
        "argument": "schema_gate",
        "gate": "supabase_standby_schema_compatibility",
        "issue": "ramideltoro/nutsnews#524",
        "repository_revision": "required",
        "candidate_application_revision": "required",
        "fence_epoch": "absent",
    },
    "sequence": {
        "argument": "sequence_gate",
        "gate": "supabase_standby_sequence_safety",
        "issue": "ramideltoro/nutsnews#525",
        "repository_revision": "required",
        "candidate_application_revision": "absent",
        "fence_epoch": "absent",
    },
    "writer_pause": {
        "argument": "writer_pause_gate",
        "gate": "supabase_standby_writer_pause_quiescence",
        "issue": "ramideltoro/nutsnews#526",
        "repository_revision": "required",
        "candidate_application_revision": "absent",
        "fence_epoch": "absent",
    },
    "split_brain_fence": {
        "argument": "split_brain_fence_gate",
        "gate": "supabase_standby_split_brain_fence",
        "issue": "ramideltoro/nutsnews#527",
        "repository_revision": "required",
        "candidate_application_revision": "absent",
        "fence_epoch": "required",
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


def iso_add(value: str, seconds: int) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return utc_now()
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_min(values: list[str]) -> str | None:
    parsed = [parse_utc(value) for value in values if value]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return min(parsed).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_between(later: str | None, earlier: str | None) -> int | None:
    later_dt = parse_utc(later)
    earlier_dt = parse_utc(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return int((later_dt - earlier_dt).total_seconds())


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def digest_object(value: Any) -> str:
    return "sha256:" + canonical_sha256(value)[:24]


def standby_binding_fingerprint(kind: str, label: str) -> str:
    payload = f"supabase-standby-binding-v1|{kind}|{label}".encode("utf-8")
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


def validate_epoch(value: str) -> None:
    if not EPOCH_RE.fullmatch(value):
        raise ValueError("invalid_fence_epoch")


def validate_revision(value: str, blocker: str) -> None:
    if not REVISION_RE.fullmatch(value):
        raise ValueError(blocker)


def bool_policy(data: dict[str, Any], key: str, expected: bool, blocker: str, blockers: list[str]) -> None:
    if data.get(key) is not expected:
        blockers.append(blocker)


def ledger_consumed(path: str, decision_id: str) -> bool:
    if not path:
        return False
    try:
        ledger = load_json(Path(path), missing="ledger_missing", malformed="ledger_malformed")
    except ValueError:
        return False
    consumed = ledger.get("consumed_decision_ids")
    if isinstance(consumed, list) and decision_id in {str(item) for item in consumed}:
        return True
    decisions = ledger.get("decisions")
    if isinstance(decisions, list):
        for item in decisions:
            if isinstance(item, dict) and item.get("decision_id") == decision_id and item.get("consumed_at_utc"):
                return True
    return False


def base_result(args: argparse.Namespace, measured_at: str | None = None) -> dict[str, Any]:
    measured = measured_at or utc_now()
    source_binding = standby_binding_fingerprint("source", EXPECTED_SOURCE_LABEL)
    target_binding = standby_binding_fingerprint("target", EXPECTED_TARGET_LABEL)
    return {
        "status": "NO-GO",
        "decision": "NO-GO",
        "gate": GATE_NAME,
        "issue": ISSUE,
        "epic": EPIC,
        "failover_attempt_id": args.failover_attempt_id,
        "candidate_application_revision": args.candidate_application_revision,
        "repository_revision": args.repository_revision,
        "fence_epoch": args.fence_epoch,
        "decision_id": None,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.decision_ttl_seconds),
        "evidence_expires_at_utc": None,
        "max_evidence_age_seconds": args.max_evidence_age_seconds,
        "decision_ttl_seconds": args.decision_ttl_seconds,
        "source_binding_fingerprint": source_binding,
        "target_binding_fingerprint": target_binding,
        "source_fingerprint_set": [],
        "target_fingerprint_set": [],
        "required_gate_count": len(REQUIRED_GATES),
        "passed_gate_count": 0,
        "failed_gate_count": 0,
        "gate_results": [],
        "blockers": [],
        "single_use": True,
        "consumed": False,
        "consumption_required_by": "ramideltoro/nutsnews#502",
        "backend_postgresql_remains_primary_until_approved_failover": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "provider_switch_performed": False,
        "safe_metadata_only": True,
    }


def gate_time(data: dict[str, Any]) -> str | None:
    for key in ("measured_at_utc", "second_write_position_at_utc", "fence_evidence_measured_at_utc", "checked_at_utc"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def evaluate_gate(
    key: str,
    spec: dict[str, str],
    args: argparse.Namespace,
    seen_gates: set[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    blockers: list[str] = []
    path = Path(str(getattr(args, spec["argument"])))
    summary: dict[str, Any] = {
        "id": key,
        "issue": spec["issue"],
        "gate": None,
        "status": "MISSING",
        "decision_status": "FAIL",
        "artifact_fingerprint": None,
        "measured_at_utc": None,
        "expires_at_utc": None,
        "evidence_age_seconds": None,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "source_binding_fingerprint": None,
        "target_binding_fingerprint": None,
        "gate_blockers": [],
        "blockers": blockers,
        "safe_metadata_only": True,
    }
    try:
        data = load_json(path, missing=f"{key}_gate_missing", malformed=f"{key}_gate_malformed")
    except ValueError as exc:
        blockers.append(str(exc))
        return summary, None

    summary["artifact_fingerprint"] = digest_object(data)
    gate_name = data.get("gate")
    summary["gate"] = gate_name if isinstance(gate_name, str) else None
    if gate_name != spec["gate"]:
        blockers.append(f"{key}_gate_mismatch")
    if isinstance(gate_name, str):
        if gate_name in seen_gates:
            blockers.append("duplicate_gate_evidence")
        seen_gates.add(gate_name)

    status = data.get("status")
    summary["status"] = status if isinstance(status, str) else "UNKNOWN"
    if status != "PASS":
        blockers.append(f"{key}_gate_not_pass")

    gate_blockers = data.get("blockers")
    if isinstance(gate_blockers, list):
        summary["gate_blockers"] = sorted(str(item) for item in gate_blockers if item)
    elif gate_blockers is not None:
        blockers.append(f"{key}_blockers_malformed")

    if data.get("safe_metadata_only") is not True:
        blockers.append(f"{key}_not_safe_metadata")
        summary["safe_metadata_only"] = False

    if data.get("failover_attempt_id") != args.failover_attempt_id:
        blockers.append(f"{key}_attempt_mismatch")

    measured = gate_time(data)
    expires = data.get("expires_at_utc")
    summary["measured_at_utc"] = measured
    summary["expires_at_utc"] = expires if isinstance(expires, str) else None
    age = seconds_between(args.now_utc, measured)
    summary["evidence_age_seconds"] = age
    if age is None:
        blockers.append(f"{key}_measurement_time_malformed")
    elif age < -60:
        blockers.append(f"{key}_evidence_from_future")
    elif age > args.max_evidence_age_seconds:
        blockers.append(f"{key}_evidence_stale")

    expires_dt = parse_utc(expires if isinstance(expires, str) else None)
    now_dt = parse_utc(args.now_utc)
    if expires_dt is None or now_dt is None:
        blockers.append(f"{key}_expiry_time_malformed")
    elif now_dt > expires_dt:
        blockers.append(f"{key}_evidence_expired")

    for field in ("source_fingerprint", "target_fingerprint", "source_binding_fingerprint", "target_binding_fingerprint"):
        value = data.get(field)
        summary[field] = value if isinstance(value, str) and value.startswith("sha256:") else None
        if summary[field] is None:
            blockers.append(f"{key}_{field}_missing")

    expected_source_binding = standby_binding_fingerprint("source", EXPECTED_SOURCE_LABEL)
    expected_target_binding = standby_binding_fingerprint("target", EXPECTED_TARGET_LABEL)
    if summary["source_binding_fingerprint"] not in {None, expected_source_binding}:
        blockers.append(f"{key}_source_binding_mismatch")
    if summary["target_binding_fingerprint"] not in {None, expected_target_binding}:
        blockers.append(f"{key}_target_binding_mismatch")

    if spec["repository_revision"] == "required" and data.get("repository_revision") != args.repository_revision:
        blockers.append(f"{key}_repository_revision_mismatch")
    if spec["repository_revision"] == "optional" and data.get("repository_revision") not in {None, "", args.repository_revision}:
        blockers.append(f"{key}_repository_revision_mismatch")
    if spec["candidate_application_revision"] == "required" and data.get("candidate_application_revision") != args.candidate_application_revision:
        blockers.append(f"{key}_candidate_application_revision_mismatch")
    if spec["fence_epoch"] == "required" and data.get("fence_epoch") != args.fence_epoch:
        blockers.append(f"{key}_fence_epoch_mismatch")

    bool_policy(data, "target_is_existing_production_supabase", True, f"{key}_target_policy_mismatch", blockers)
    bool_policy(data, "create_new_supabase_project", False, f"{key}_new_supabase_project_not_forbidden", blockers)
    bool_policy(data, "create_nutsnews_standby_database", False, f"{key}_nutsnews_standby_database_not_forbidden", blockers)
    bool_policy(data, "app_worker_writes_to_supabase_before_failover", False, f"{key}_app_worker_supabase_writes_not_blocked", blockers)

    backend_primary = data.get("backend_postgresql_remains_primary")
    backend_primary_until_failover = data.get("backend_postgresql_remains_primary_until_approved_failover")
    if backend_primary is not True and backend_primary_until_failover is not True:
        blockers.append(f"{key}_backend_primary_policy_mismatch")

    if key == "split_brain_fence":
        if data.get("write_eligible_provider_count") != 1:
            blockers.append("split_brain_fence_write_eligible_provider_count_mismatch")
        if data.get("eligible_provider") != EXPECTED_TARGET_LABEL:
            blockers.append("split_brain_fence_eligible_provider_mismatch")
        if data.get("backend_postgresql_fenced") is not True:
            blockers.append("split_brain_fence_backend_not_fenced")
        if data.get("target_write_eligible_after_backend_fence") is not True:
            blockers.append("split_brain_fence_target_not_write_eligible_after_backend_fence")

    summary["decision_status"] = "PASS" if not blockers else "FAIL"
    summary["blockers"] = sorted(set(blockers))
    return summary, data


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.failover_attempt_id)
    validate_epoch(args.fence_epoch)
    validate_revision(args.candidate_application_revision, "invalid_candidate_application_revision")
    validate_revision(args.repository_revision, "invalid_repository_revision")

    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    result = base_result(args, args.now_utc)
    blockers: list[str] = result["blockers"]

    if contract.get("gate_id") != "backend-supabase-standby-promotion-decision":
        blockers.append("contract_id_mismatch")
    if contract.get("tracking_issue") != ISSUE:
        blockers.append("contract_issue_mismatch")
    if contract.get("epic") != EPIC:
        blockers.append("contract_epic_mismatch")

    source = contract.get("source", {})
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    decision_policy = contract.get("decision", {})
    if not isinstance(source, dict) or source.get("label") != EXPECTED_SOURCE_LABEL:
        blockers.append("source_policy_mismatch")
    if not isinstance(target, dict) or target.get("label") != EXPECTED_TARGET_LABEL:
        blockers.append("target_policy_mismatch")
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(safety, dict) or safety.get("backend_postgresql_remains_primary_until_approved_failover") is not True:
        blockers.append("backend_primary_policy_mismatch")
    if not isinstance(safety, dict) or safety.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked")
    if not isinstance(safety, dict) or safety.get("safe_metadata_only") is not True:
        blockers.append("contract_not_safe_metadata")
    if not isinstance(decision_policy, dict) or decision_policy.get("single_use") is not True:
        blockers.append("single_use_policy_missing")
    if not isinstance(decision_policy, dict) or decision_policy.get("ttl_seconds") != DECISION_TTL_SECONDS:
        blockers.append("decision_ttl_policy_mismatch")

    seen_gates: set[str] = set()
    loaded_gates: dict[str, dict[str, Any]] = {}
    gate_results: list[dict[str, Any]] = []
    for key, spec in REQUIRED_GATES.items():
        summary, data = evaluate_gate(key, spec, args, seen_gates)
        gate_results.append(summary)
        blockers.extend(summary["blockers"])
        if data is not None:
            loaded_gates[key] = data

    result["gate_results"] = gate_results
    result["passed_gate_count"] = sum(1 for item in gate_results if item["decision_status"] == "PASS")
    result["failed_gate_count"] = len(gate_results) - result["passed_gate_count"]
    if result["passed_gate_count"] != len(REQUIRED_GATES):
        blockers.append("not_all_gates_passed")

    source_fingerprints = sorted(
        {
            str(item["source_fingerprint"])
            for item in gate_results
            if isinstance(item.get("source_fingerprint"), str)
        }
    )
    target_fingerprints = sorted(
        {
            str(item["target_fingerprint"])
            for item in gate_results
            if isinstance(item.get("target_fingerprint"), str)
        }
    )
    result["source_fingerprint_set"] = source_fingerprints
    result["target_fingerprint_set"] = target_fingerprints

    evidence_expiry = iso_min([str(item.get("expires_at_utc") or "") for item in gate_results])
    result["evidence_expires_at_utc"] = evidence_expiry
    decision_expiry = iso_min([iso_add(args.now_utc, args.decision_ttl_seconds), evidence_expiry or ""])
    if decision_expiry:
        result["expires_at_utc"] = decision_expiry

    gate_fingerprints = {
        item["id"]: item["artifact_fingerprint"]
        for item in gate_results
        if isinstance(item.get("artifact_fingerprint"), str)
    }
    result["decision_id"] = digest_object(
        {
            "gate": GATE_NAME,
            "attempt": args.failover_attempt_id,
            "candidate_application_revision": args.candidate_application_revision,
            "repository_revision": args.repository_revision,
            "fence_epoch": args.fence_epoch,
            "source_binding_fingerprint": result["source_binding_fingerprint"],
            "target_binding_fingerprint": result["target_binding_fingerprint"],
            "gate_fingerprints": gate_fingerprints,
        }
    )

    if ledger_consumed(args.consumption_ledger, str(result["decision_id"])):
        blockers.append("decision_already_consumed")
        result["consumed"] = True

    result["blockers"] = sorted(set(blockers))
    if not result["blockers"]:
        result["status"] = "GO"
        result["decision"] = "GO"
    return result


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    result = base_result(args, args.now_utc)
    result["blockers"] = [blocker]
    result["decision_id"] = digest_object(
        {
            "gate": GATE_NAME,
            "attempt": args.failover_attempt_id,
            "candidate_application_revision": args.candidate_application_revision,
            "repository_revision": args.repository_revision,
            "fence_epoch": args.fence_epoch,
            "blocker": blocker,
        }
    )
    return result


def write_outputs(result: dict[str, Any], output: str, summary: str) -> None:
    text = json.dumps(result, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    if summary:
        lines = [
            "# Supabase Standby Promotion Decision",
            "",
            f"- Decision: `{result['decision']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Candidate revision: `{result['candidate_application_revision']}`",
            f"- Repository revision: `{result['repository_revision']}`",
            f"- Fence epoch: `{result['fence_epoch']}`",
            f"- Decision id: `{result.get('decision_id')}`",
            f"- Required gates: `{result['required_gate_count']}`",
            f"- Passed gates: `{result['passed_gate_count']}`",
            f"- Failed gates: `{result['failed_gate_count']}`",
            f"- Measured at: `{result['measured_at_utc']}`",
            f"- Expires at: `{result['expires_at_utc']}`",
            f"- Blockers: `{', '.join(result['blockers']) if result['blockers'] else 'none'}`",
            "",
            "Safe metadata only; provider switching is not performed by this decision.",
        ]
        Path(summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(text)


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--lag-gate", required=True)
    parser.add_argument("--parity-gate", required=True)
    parser.add_argument("--schema-gate", required=True)
    parser.add_argument("--sequence-gate", required=True)
    parser.add_argument("--writer-pause-gate", required=True)
    parser.add_argument("--split-brain-fence-gate", required=True)
    parser.add_argument("--failover-attempt-id", required=True)
    parser.add_argument("--candidate-application-revision", required=True)
    parser.add_argument("--repository-revision", required=True)
    parser.add_argument("--fence-epoch", required=True)
    parser.add_argument("--consumption-ledger", default="")
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--max-evidence-age-seconds", type=int, default=MAX_EVIDENCE_AGE_SECONDS)
    parser.add_argument("--decision-ttl-seconds", type=int, default=DECISION_TTL_SECONDS)
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    if args.max_evidence_age_seconds < 1 or args.max_evidence_age_seconds > 900:
        raise SystemExit("max_evidence_age_seconds_out_of_bounds")
    if args.decision_ttl_seconds != DECISION_TTL_SECONDS:
        raise SystemExit("decision_ttl_seconds_is_fixed_at_300")

    try:
        result = evaluate(args)
    except ValueError as exc:
        result = fail_result(args, str(exc))
    write_outputs(result, args.output, args.summary)
    return 1 if args.enforce and result["decision"] != "GO" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
