#!/usr/bin/env python3
"""Validate Supabase standby split-brain fence gate guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-split-brain-fence-gate.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_split_brain_fence_gate.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_split_brain_fence_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-split-brain-fence-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_SPLIT_BRAIN_FENCE_GATE.md"
README_PATH = ROOT / "README.md"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read(path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object in {path}")
    return data


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def require_token(token: str, text: str, label: str, errors: list[str]) -> None:
    require(token in text, f"{label} missing token: {token}", errors)


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    script = read(SCRIPT_PATH)
    tests = read(TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    errors: list[str] = []

    require(contract.get("schema_version") == 1, "contract schema_version must be 1", errors)
    require(
        contract.get("gate_id") == "backend-supabase-standby-split-brain-fence",
        "contract gate_id is incorrect",
        errors,
    )
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews#527", "contract must point to #527", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)

    source = contract.get("source", {})
    target = contract.get("target", {})
    safety = contract.get("safety", {})
    lease = contract.get("lease", {})
    require(source.get("label") == "backend_postgres_primary", "source must be backend PostgreSQL primary", errors)
    require(
        source.get("must_be_fenced_before_target_write_eligibility") is True,
        "backend PostgreSQL must be fenced before target write eligibility",
        errors,
    )
    require(target.get("label") == "existing_production_supabase_standby", "target must be existing production Supabase", errors)
    require(target.get("existing_production_supabase_project") is True, "target must confirm existing production Supabase", errors)
    require(target.get("create_new_supabase_project") is False, "new Supabase project creation must be forbidden", errors)
    require(target.get("create_nutsnews_standby_database") is False, "nutsnews-standby DB creation must be forbidden", errors)
    require(
        safety.get("backend_postgresql_remains_primary_until_approved_failover") is True,
        "backend primary policy flag missing",
        errors,
    )
    require(safety.get("target_is_existing_production_supabase") is True, "existing Supabase safety flag missing", errors)
    require(safety.get("app_worker_writes_to_supabase_before_failover") is False, "app/worker Supabase writes must stay blocked", errors)
    require(safety.get("supabase_write_enablement_is_failover_only") is True, "Supabase writes must be failover-only", errors)
    require(safety.get("safe_metadata_only") is True, "safe metadata flag missing", errors)
    require(safety.get("sql_text_exposed") is False, "SQL text exposure must be false", errors)
    require(safety.get("row_data_exposed") is False, "row data exposure must be false", errors)
    require(safety.get("credentials_exposed") is False, "credential exposure must be false", errors)
    require(lease.get("exactly_one_provider_write_eligible") is True, "lease must require exactly one writer", errors)
    require(lease.get("stale_processes_must_reject_previous_epochs") is True, "stale epoch rejection is required", errors)
    require(lease.get("idempotent_retry_required") is True, "idempotent retry is required", errors)

    backend_controls = {item.get("id") for item in contract.get("required_backend_fence_controls", []) if isinstance(item, dict)}
    for control in (
        "writer_pause_gate_passed",
        "backend_worker_database_api_writes_disabled",
        "worker_uplift_production_writes_disabled",
        "backend_postgres_application_write_routes_disabled",
        "backend_postgres_database_roles_revoked_or_blocked",
        "stale_backend_writer_epoch_rejected",
        "provider_epoch_mismatch_rejects_writes",
    ):
        require(control in backend_controls, f"backend fence controls missing {control}", errors)
    target_controls = {item.get("id") for item in contract.get("required_target_controls", []) if isinstance(item, dict)}
    for control in (
        "supabase_write_eligibility_after_backend_fence",
        "supabase_write_credentials_not_exposed_to_app_workers_before_failover",
        "supabase_write_enabled_only_for_current_epoch",
    ):
        require(control in target_controls, f"target controls missing {control}", errors)

    failure_policy = contract.get("failure_policy", {})
    for policy in (
        "ambiguous_ownership",
        "simultaneous_write_eligibility",
        "stale_epoch",
        "stale_process",
        "partial_fencing",
        "retry_not_idempotent",
        "verification_unavailable",
        "writer_pause_missing_or_stale",
        "mismatched_attempt",
        "malformed_evidence",
        "target_mismatch",
    ):
        require(failure_policy.get(policy) == "FAIL", f"failure policy must fail closed for {policy}", errors)

    result_contract = contract.get("result_contract", {})
    require(result_contract.get("safe_metadata_only") is True, "result contract must be safe metadata only", errors)
    require(result_contract.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    for field in (
        "failover_attempt_id",
        "fence_epoch",
        "source_fingerprint",
        "target_fingerprint",
        "write_eligible_provider_count",
        "eligible_provider",
        "measured_at_utc",
        "expires_at_utc",
        "blockers",
    ):
        require(field in result_contract.get("required_fields", []), f"result contract missing {field}", errors)

    for token in (
        "DEFAULT_CONTRACT = ROOT / \"docs\" / \"backend-supabase-standby-split-brain-fence-gate.json\"",
        "GATE_NAME = \"supabase_standby_split_brain_fence\"",
        "ISSUE = \"ramideltoro/nutsnews#527\"",
        "EPIC = \"ramideltoro/nutsnews#521\"",
        "EXPECTED_SOURCE_LABEL = \"backend_postgres_primary\"",
        "EXPECTED_TARGET_LABEL = \"existing_production_supabase_standby\"",
        "writer_pause_evidence_stale",
        "simultaneous_write_eligibility",
        "stale_backend_writer_not_rejected",
        "provider_epoch_mismatch_not_rejected",
        "fence_retry_not_safe",
        "fence_verification_unavailable",
        "supabase_write_enabled_before_backend_fence",
        "--enforce",
    ):
        require_token(token, script, "split-brain fence evaluator", errors)

    for forbidden in (
        "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PGPASSWORD",
        "supabase projects create",
        "nutsnews-standby",
    ):
        require(forbidden not in script, f"evaluator must not contain credential/provisioning marker: {forbidden}", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "fence_epoch:",
        "writer_pause_gate_run_id:",
        "confirmation:",
        "backend_fence_confirmation:",
        "stale_writer_confirmation:",
        "supabase_eligibility_confirmation:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "python3 scripts/validate_backend_supabase_standby_split_brain_fence_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_split_brain_fence_gate",
        "gh run download",
        "backend-supabase-standby-writer-pause-gate",
        "backend_supabase_standby_split_brain_fence_gate.py",
        "backend-supabase-standby-split-brain-fence-gate",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "split-brain fence workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)

    for token in (
        "test_complete_fence_and_single_target_write_eligibility_passes",
        "test_enabling_supabase_before_backend_revocation_fails",
        "test_stale_process_fixture_fails",
        "test_stale_epoch_fixture_fails",
        "test_partial_backend_fencing_fails",
        "test_retry_not_idempotent_fails",
        "test_verification_unavailable_fails",
        "test_writer_pause_missing_or_stale_fails",
        "test_target_mismatch_fails",
        "test_malformed_evidence_fails_closed",
        "test_enforce_mode_returns_non_zero_on_fail",
        "test_artifact_is_safe_metadata_only",
    ):
        require_token(token, tests, "split-brain fence tests", errors)

    for token in (
        "Issue #527",
        "PASS",
        "FAIL",
        "writer-pause gate",
        "fence epoch",
        "safe metadata only",
        "existing production Supabase",
        "no new Supabase project",
        "no `nutsnews-standby` database",
        "backend PostgreSQL remains the normal primary until approved failover",
        "backend-supabase-standby-split-brain-fence-gate.yml",
    ):
        require_token(token, runbook, "split-brain fence runbook", errors)

    require_token("runbooks/SUPABASE_STANDBY_SPLIT_BRAIN_FENCE_GATE.md", readme, "README", errors)
    require_token("python3 scripts/validate_backend_supabase_standby_split_brain_fence_gate.py", checks_workflow, "backend checks workflow", errors)
    require_token("python3 -m unittest tests.test_backend_supabase_standby_split_brain_fence_gate", checks_workflow, "backend checks workflow", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby split-brain fence gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
