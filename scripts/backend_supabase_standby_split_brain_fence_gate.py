#!/usr/bin/env python3
"""Evaluate split-brain fencing evidence for Supabase standby failover."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-standby-split-brain-fence-gate.json"
GATE_NAME = "supabase_standby_split_brain_fence"
ISSUE = "ramideltoro/nutsnews#527"
EPIC = "ramideltoro/nutsnews#521"
EXPECTED_SOURCE_LABEL = "backend_postgres_primary"
EXPECTED_TARGET_LABEL = "existing_production_supabase_standby"
EXPECTED_BACKEND_PROVIDER = "backend_postgres_primary"
EXPECTED_TARGET_PROVIDER = "existing_production_supabase_standby"
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
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


def safe_fingerprint(kind: str, label: str, contract_id: str, version: Any) -> str:
    payload = f"{kind}|{label}|{contract_id}|{version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:24]


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


def bool_field(data: dict[str, Any], key: str, blockers: list[str], blocker: str, *, expected: bool = True) -> None:
    if data.get(key) is not expected:
        blockers.append(blocker)


def provider_map(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = evidence.get("providers", [])
    if not isinstance(raw, list):
        return {}
    providers: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            providers[item["id"]] = item
    return providers


def base_result(args: argparse.Namespace, measured_at: str | None = None) -> dict[str, Any]:
    measured = measured_at or utc_now()
    return {
        "status": "FAIL",
        "gate": GATE_NAME,
        "issue": ISSUE,
        "epic": EPIC,
        "failover_attempt_id": args.failover_attempt_id,
        "fence_epoch": args.fence_epoch,
        "repository_revision": args.repository_revision,
        "measured_at_utc": measured,
        "expires_at_utc": iso_add(measured, args.result_ttl_seconds),
        "max_evidence_age_seconds": args.max_evidence_age_seconds,
        "source_fingerprint": None,
        "target_fingerprint": None,
        "source_binding_fingerprint": None,
        "target_binding_fingerprint": None,
        "contract_fingerprint": None,
        "writer_pause_gate_fingerprint": None,
        "fence_evidence_fingerprint": None,
        "writer_pause_measured_at_utc": None,
        "fence_evidence_measured_at_utc": None,
        "writer_pause_age_seconds": None,
        "fence_evidence_age_seconds": None,
        "write_eligible_provider_count": 0,
        "eligible_provider": None,
        "providers": [],
        "fence_controls": [],
        "blockers": [],
        "backend_postgresql_fenced": False,
        "target_write_eligible_after_backend_fence": False,
        "backend_postgresql_remains_primary_until_approved_failover": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }


def safe_provider_result(provider_id: str, provider: dict[str, Any] | None) -> dict[str, Any]:
    blockers: list[str] = []
    if provider is None:
        blockers.append("provider_evidence_missing")
        write_eligible = None
        fenced = None
        epoch = None
    else:
        write_eligible = provider.get("write_eligible")
        fenced = provider.get("fenced")
        epoch = provider.get("epoch")
        if not isinstance(write_eligible, bool):
            blockers.append("provider_write_eligibility_missing")
        if not isinstance(epoch, str):
            blockers.append("provider_epoch_missing")
    return {
        "id": provider_id,
        "write_eligible": write_eligible if isinstance(write_eligible, bool) else None,
        "fenced": fenced if isinstance(fenced, bool) else None,
        "epoch": epoch if isinstance(epoch, str) else None,
        "blockers": blockers,
        "safe_status_only": True,
    }


def evaluate_contract(contract: dict[str, Any], blockers: list[str]) -> tuple[str, str, str, Any]:
    contract_id = str(contract.get("gate_id") or "unknown")
    version = contract.get("schema_version", "unknown")
    source_label = str(contract.get("source", {}).get("label") or "")
    target_label = str(contract.get("target", {}).get("label") or "")
    if contract_id != "backend-supabase-standby-split-brain-fence":
        blockers.append("contract_id_mismatch")
    if source_label != EXPECTED_SOURCE_LABEL:
        blockers.append("source_policy_mismatch")
    if target_label != EXPECTED_TARGET_LABEL:
        blockers.append("target_policy_mismatch")
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    lease = contract.get("lease", {})
    if not isinstance(target, dict) or target.get("existing_production_supabase_project") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if not isinstance(target, dict) or target.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if not isinstance(target, dict) or target.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if not isinstance(safety, dict):
        blockers.append("safety_policy_missing")
    else:
        if safety.get("backend_postgresql_remains_primary_until_approved_failover") is not True:
            blockers.append("backend_primary_policy_mismatch")
        if safety.get("target_is_existing_production_supabase") is not True:
            blockers.append("target_existing_production_supabase_not_confirmed")
        if safety.get("app_worker_writes_to_supabase_before_failover") is not False:
            blockers.append("app_worker_supabase_writes_not_blocked")
        if safety.get("safe_metadata_only") is not True:
            blockers.append("contract_not_safe_metadata")
    if not isinstance(lease, dict) or lease.get("exactly_one_provider_write_eligible") is not True:
        blockers.append("lease_exactly_one_provider_missing")
    if not isinstance(lease, dict) or lease.get("stale_processes_must_reject_previous_epochs") is not True:
        blockers.append("stale_process_policy_missing")
    return contract_id, source_label, target_label, version


def evaluate_writer_pause(args: argparse.Namespace, pause: dict[str, Any], blockers: list[str]) -> None:
    result_time = str(pause.get("measured_at_utc") or pause.get("second_write_position_at_utc") or "")
    if pause.get("status") != "PASS":
        blockers.append("writer_pause_gate_failed")
    if pause.get("failover_attempt_id") != args.failover_attempt_id:
        blockers.append("writer_pause_attempt_mismatch")
    if pause.get("backend_postgresql_remains_primary") is not True:
        blockers.append("writer_pause_backend_primary_policy_mismatch")
    if pause.get("target_is_existing_production_supabase") is not True:
        blockers.append("writer_pause_target_policy_mismatch")
    if pause.get("create_new_supabase_project") is not False:
        blockers.append("writer_pause_new_supabase_project_not_forbidden")
    if pause.get("create_nutsnews_standby_database") is not False:
        blockers.append("writer_pause_nutsnews_standby_database_not_forbidden")
    if pause.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("writer_pause_app_worker_supabase_writes_not_blocked")
    if pause.get("safe_metadata_only") is not True:
        blockers.append("writer_pause_not_safe_metadata")
    if pause.get("paused_writer_count") != pause.get("required_writer_count") or pause.get("failed_writer_count") != 0:
        blockers.append("writer_pause_incomplete")
    expires = parse_utc(str(pause.get("expires_at_utc") or ""))
    now = parse_utc(args.now_utc)
    if expires is None or now is None:
        blockers.append("writer_pause_time_malformed")
    elif now > expires:
        blockers.append("writer_pause_evidence_stale")
    age = seconds_between(args.now_utc, result_time)
    if age is None:
        blockers.append("writer_pause_time_malformed")
    elif age < -60:
        blockers.append("writer_pause_evidence_from_future")
    elif age > args.max_evidence_age_seconds:
        blockers.append("writer_pause_evidence_stale")


def fence_control_results(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    backend = evidence.get("backend_fence", {})
    target = evidence.get("supabase_fence", {})
    if not isinstance(backend, dict):
        backend = {}
    if not isinstance(target, dict):
        target = {}
    controls = [
        ("backend_worker_database_api_writes_disabled", backend.get("backend_worker_database_api_writes_enabled") is False),
        ("worker_uplift_production_writes_disabled", backend.get("worker_uplift_production_writes_enabled") is False),
        ("backend_postgres_application_write_routes_disabled", backend.get("application_write_routes_disabled") is True),
        ("backend_postgres_database_roles_revoked_or_blocked", backend.get("database_write_roles_revoked_or_blocked") is True),
        ("stale_backend_writer_epoch_rejected", backend.get("stale_writer_epoch_rejected") is True),
        ("provider_epoch_mismatch_rejects_writes", backend.get("provider_epoch_mismatch_rejects_writes") is True),
        ("backend_fence_verification_reachable", backend.get("verification_reachable") is True),
        ("idempotent_retry_safe", backend.get("idempotent_retry_safe") is True),
        ("supabase_write_eligibility_after_backend_fence", target.get("write_eligibility_requires_backend_fence") is True),
        (
            "supabase_write_credentials_not_exposed_to_app_workers_before_failover",
            target.get("write_credentials_not_exposed_to_app_workers_before_failover") is True,
        ),
        ("supabase_write_enabled_only_for_current_epoch", target.get("write_enabled_only_for_current_epoch") is True),
        ("supabase_fence_verification_reachable", target.get("verification_reachable") is True),
    ]
    return [
        {
            "id": control_id,
            "status": "PASS" if passed else "FAIL",
            "blockers": [] if passed else [f"{control_id}_failed"],
            "safe_status_only": True,
        }
        for control_id, passed in controls
    ]


def evaluate_fence_evidence(args: argparse.Namespace, evidence: dict[str, Any], blockers: list[str]) -> list[dict[str, Any]]:
    if evidence.get("status") != "pass":
        blockers.append("fence_evidence_failed")
    if evidence.get("safe_metadata_only") is not True:
        blockers.append("fence_evidence_not_safe_metadata")
    if evidence.get("failover_attempt_id") != args.failover_attempt_id:
        blockers.append("fence_attempt_mismatch")
    if evidence.get("fence_epoch") != args.fence_epoch:
        blockers.append("fence_epoch_mismatch")
    if evidence.get("source_label") != EXPECTED_SOURCE_LABEL:
        blockers.append("source_policy_mismatch")
    if evidence.get("target_label") != EXPECTED_TARGET_LABEL:
        blockers.append("target_policy_mismatch")
    if evidence.get("target_is_existing_production_supabase") is not True:
        blockers.append("target_existing_production_supabase_not_confirmed")
    if evidence.get("create_new_supabase_project") is not False:
        blockers.append("new_supabase_project_not_forbidden")
    if evidence.get("create_nutsnews_standby_database") is not False:
        blockers.append("nutsnews_standby_database_not_forbidden")
    if evidence.get("app_worker_writes_to_supabase_before_failover") is not False:
        blockers.append("app_worker_supabase_writes_not_blocked")

    lease = evidence.get("lease", {})
    if not isinstance(lease, dict):
        blockers.append("lease_evidence_missing")
        lease = {}
    if lease.get("epoch") != args.fence_epoch:
        blockers.append("lease_epoch_mismatch")
    if lease.get("previous_holder") != EXPECTED_BACKEND_PROVIDER:
        blockers.append("lease_previous_holder_mismatch")
    if lease.get("holder") != EXPECTED_TARGET_PROVIDER:
        blockers.append("lease_holder_mismatch")

    age = seconds_between(args.now_utc, str(evidence.get("checked_at_utc") or ""))
    if age is None:
        blockers.append("fence_evidence_time_malformed")
    elif age < -60:
        blockers.append("fence_evidence_from_future")
    elif age > args.max_evidence_age_seconds:
        blockers.append("fence_evidence_stale")

    controls = fence_control_results(evidence)
    failed_controls = [control for control in controls if control["status"] != "PASS"]
    if failed_controls:
        blockers.append("backend_postgres_fence_incomplete")
    failed_ids = {control["id"] for control in failed_controls}
    if "stale_backend_writer_epoch_rejected" in failed_ids:
        blockers.append("stale_backend_writer_not_rejected")
    if "provider_epoch_mismatch_rejects_writes" in failed_ids:
        blockers.append("provider_epoch_mismatch_not_rejected")
    if "idempotent_retry_safe" in failed_ids:
        blockers.append("fence_retry_not_safe")
    if "backend_fence_verification_reachable" in failed_ids or "supabase_fence_verification_reachable" in failed_ids:
        blockers.append("fence_verification_unavailable")
    return controls


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    validate_attempt_id(args.failover_attempt_id)
    validate_epoch(args.fence_epoch)
    contract = load_json(Path(args.contract), missing="contract_missing", malformed="contract_malformed")
    pause = load_json(Path(args.writer_pause_gate), missing="writer_pause_gate_missing", malformed="writer_pause_gate_malformed")
    evidence = load_json(Path(args.fence_evidence), missing="fence_evidence_missing", malformed="fence_evidence_malformed")

    measured_at = str(evidence.get("checked_at_utc") or args.now_utc)
    result = base_result(args, measured_at)
    blockers: list[str] = result["blockers"]

    contract_id, source_label, target_label, version = evaluate_contract(contract, blockers)
    result["contract_fingerprint"] = digest_object(contract)
    result["source_fingerprint"] = safe_fingerprint("source", source_label, contract_id, version)
    result["target_fingerprint"] = safe_fingerprint("target", target_label, contract_id, version)
    result["source_binding_fingerprint"] = standby_binding_fingerprint("source", source_label)
    result["target_binding_fingerprint"] = standby_binding_fingerprint("target", target_label)
    result["writer_pause_gate_fingerprint"] = digest_object(pause)
    result["fence_evidence_fingerprint"] = digest_object(evidence)
    result["writer_pause_measured_at_utc"] = pause.get("measured_at_utc") or pause.get("second_write_position_at_utc")
    result["fence_evidence_measured_at_utc"] = evidence.get("checked_at_utc")
    result["writer_pause_age_seconds"] = seconds_between(args.now_utc, str(result["writer_pause_measured_at_utc"] or ""))
    result["fence_evidence_age_seconds"] = seconds_between(args.now_utc, str(evidence.get("checked_at_utc") or ""))

    evaluate_writer_pause(args, pause, blockers)
    result["fence_controls"] = evaluate_fence_evidence(args, evidence, blockers)

    providers = provider_map(evidence)
    backend = providers.get(EXPECTED_BACKEND_PROVIDER)
    target = providers.get(EXPECTED_TARGET_PROVIDER)
    provider_results = [
        safe_provider_result(EXPECTED_BACKEND_PROVIDER, backend),
        safe_provider_result(EXPECTED_TARGET_PROVIDER, target),
    ]
    result["providers"] = provider_results
    for item in provider_results:
        blockers.extend(item["blockers"])
        if item["epoch"] not in {None, args.fence_epoch}:
            blockers.append("provider_epoch_mismatch")

    backend_write_eligible = backend.get("write_eligible") if isinstance(backend, dict) else None
    target_write_eligible = target.get("write_eligible") if isinstance(target, dict) else None
    backend_fenced = backend.get("fenced") if isinstance(backend, dict) else None
    eligible = [
        item["id"]
        for item in provider_results
        if item.get("write_eligible") is True
    ]
    result["write_eligible_provider_count"] = len(eligible)
    result["eligible_provider"] = eligible[0] if len(eligible) == 1 else None
    result["backend_postgresql_fenced"] = backend_fenced is True and backend_write_eligible is False
    result["target_write_eligible_after_backend_fence"] = target_write_eligible is True and result["backend_postgresql_fenced"]

    if backend_write_eligible is True and target_write_eligible is True:
        blockers.append("simultaneous_write_eligibility")
    if backend_write_eligible is not False:
        blockers.append("backend_postgres_still_write_eligible")
    if backend_fenced is not True:
        blockers.append("backend_postgres_not_fenced")
    if target_write_eligible is not True:
        blockers.append("target_not_write_eligible")
    if len(eligible) != 1:
        blockers.append("ambiguous_write_ownership")
    if target_write_eligible is True and backend_write_eligible is not False:
        blockers.append("supabase_write_enabled_before_backend_fence")
    if result["eligible_provider"] not in {None, EXPECTED_TARGET_PROVIDER}:
        blockers.append("eligible_provider_mismatch")

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
            "# Supabase Standby Split-Brain Fence Gate",
            "",
            f"- Status: `{result['status']}`",
            f"- Attempt: `{result['failover_attempt_id']}`",
            f"- Fence epoch: `{result['fence_epoch']}`",
            f"- Eligible provider: `{result.get('eligible_provider')}`",
            f"- Write-eligible provider count: `{result.get('write_eligible_provider_count')}`",
            f"- Blockers: `{', '.join(result['blockers']) if result['blockers'] else 'none'}`",
            "",
            "Safe metadata only; no credentials, SQL text, database URLs, row data, or host/project metadata are emitted.",
        ]
        Path(summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(text)


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--writer-pause-gate", required=True)
    parser.add_argument("--fence-evidence", required=True)
    parser.add_argument("--repository-revision", required=True)
    parser.add_argument("--failover-attempt-id", required=True)
    parser.add_argument("--fence-epoch", required=True)
    parser.add_argument("--now-utc", default=utc_now())
    parser.add_argument("--max-evidence-age-seconds", type=int, default=MAX_EVIDENCE_AGE_SECONDS)
    parser.add_argument("--result-ttl-seconds", type=int, default=RESULT_TTL_SECONDS)
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = evaluate(args)
    except ValueError as exc:
        result = fail_result(args, str(exc))
    write_outputs(result, args.output, args.summary)
    return 1 if args.enforce and result["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
