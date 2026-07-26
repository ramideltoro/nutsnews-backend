#!/usr/bin/env python3
"""Validate Supabase standby schema compatibility gate guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-supabase-standby-schema-gate.json"
SYNC_RELAY_CONTRACT_PATH = ROOT / "docs" / "backend-supabase-sync-relay.json"
SCRIPT_PATH = ROOT / "scripts" / "backend_supabase_standby_schema_gate.py"
TEST_PATH = ROOT / "tests" / "test_backend_supabase_standby_schema_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-supabase-standby-schema-gate.yml"
CHECKS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
RUNBOOK_PATH = ROOT / "runbooks" / "SUPABASE_STANDBY_SCHEMA_GATE.md"
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
    sync_relay_contract = load_json(SYNC_RELAY_CONTRACT_PATH)
    script = read(SCRIPT_PATH)
    tests = read(TEST_PATH)
    workflow = read(WORKFLOW_PATH)
    checks_workflow = read(CHECKS_WORKFLOW_PATH)
    runbook = read(RUNBOOK_PATH)
    readme = read(README_PATH)
    errors: list[str] = []

    require(contract.get("gate_id") == "supabase_standby_schema_compatibility", "gate_id is incorrect", errors)
    require(contract.get("version") == 1, "contract version must be 1", errors)
    require(contract.get("issue") == "ramideltoro/nutsnews#524", "contract must point to #524", errors)
    require(contract.get("epic") == "ramideltoro/nutsnews#521", "contract must point to #521", errors)
    require("ramideltoro/nutsnews#497" in contract.get("parent_prerequisites", []), "contract must depend on #497", errors)
    require("ramideltoro/nutsnews#499" in contract.get("parent_prerequisites", []), "contract must depend on #499", errors)
    require(
        sync_relay_contract.get("manifest_schema_fingerprint")
        == sync_relay_contract.get("source_manifest", {}).get("schema_fingerprint"),
        "sync relay contract must expose the #497 manifest schema fingerprint at top level",
        errors,
    )

    thresholds = contract.get("thresholds", {})
    require(thresholds.get("max_telemetry_age_seconds") == 300, "telemetry threshold must be 300 seconds", errors)
    require(thresholds.get("result_ttl_seconds") == 300, "result ttl must be 300 seconds", errors)
    workflow_contract = contract.get("workflow", {})
    require(workflow_contract.get("environment") == "production-backend", "workflow must use production-backend", errors)
    require(workflow_contract.get("default_enforce") is False, "default enforce must be false", errors)
    require(workflow_contract.get("artifact") == "backend-supabase-standby-schema-gate", "artifact name is incorrect", errors)

    required_fields = set(contract.get("result_schema", {}).get("required_fields", []))
    for field in (
        "status",
        "gate",
        "failover_attempt_id",
        "candidate_application_revision",
        "repository_revision",
        "manifest_fingerprint",
        "candidate_manifest",
        "relay_contract_fingerprint",
        "source_fingerprint",
        "target_fingerprint",
        "measured_at_utc",
        "expires_at_utc",
        "relay_checked_at_utc",
        "relay_schema_age_seconds",
        "schema",
        "required_sequence_count",
        "identity_checks",
        "sequence_bindings",
        "blockers",
        "safe_metadata_only",
    ):
        require(field in required_fields, f"result schema missing {field}", errors)

    for blocker in (
        "candidate_manifest_fingerprint_mismatch",
        "candidate_manifest_structural_objects_malformed",
        "candidate_manifest_table_set_mismatch",
        "candidate_manifest_sequence_set_mismatch",
        "required_function_validation_unavailable",
        "required_view_validation_unavailable",
        "schema_compatibility_failed",
        "identity_compatibility_failed",
        "sequence_binding_failed",
        "telemetry_stale",
        "relay_schema_stale",
        "target_fingerprint_mismatch",
        "candidate_application_revision_mismatch",
    ):
        require(blocker in contract.get("fail_closed_blockers", []), f"contract missing fail-closed blocker: {blocker}", errors)

    for blocker in ("schema_fingerprint_mismatch", "migration_contract_fingerprint_mismatch"):
        require(blocker in contract.get("schema_blockers", []), f"contract missing schema blocker: {blocker}", errors)
    for blocker in ("manifest_identity_check_missing", "live_identity_check_missing", "target_primary_key_mismatch"):
        require(blocker in contract.get("identity_blockers", []), f"contract missing identity blocker: {blocker}", errors)
    for blocker in ("sequence_binding_check_missing", "sequence_metadata_unavailable", "sequence_binding_mismatch"):
        require(blocker in contract.get("sequence_binding_blockers", []), f"contract missing sequence blocker: {blocker}", errors)

    for token in (
        "GATE_NAME = \"supabase_standby_schema_compatibility\"",
        "ISSUE = \"ramideltoro/nutsnews#524\"",
        "EPIC = \"ramideltoro/nutsnews#521\"",
        "MAX_TELEMETRY_AGE_SECONDS = 300",
        "RESULT_TTL_SECONDS = 300",
        "REVISION_RE = re.compile",
        "candidate_manifest_summary",
        "candidate_manifest_fingerprint_mismatch",
        "schema_fingerprint_mismatch",
        "migration_contract_fingerprint_mismatch",
        "identity_compatibility_failed",
        "sequence_binding_failed",
        "required_function_validation_unavailable",
        "required_view_validation_unavailable",
        "position_safety_status",
        "--candidate-standby-manifest",
        "--expected-application-revision",
        "--enforce",
    ):
        require_token(token, script, "schema gate script", errors)

    for forbidden in ("postgres://", "postgresql://", "PGPASSWORD", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "supabase.co"):
        require(forbidden not in script, f"schema gate script must not contain credential/host marker: {forbidden}", errors)

    for token in (
        "test_exact_compatible_schema_passes",
        "test_candidate_manifest_fingerprint_mismatch_fails_wrong_revision_evidence",
        "test_candidate_manifest_table_set_mismatch_fails",
        "test_candidate_manifest_function_or_view_requires_validator",
        "test_schema_fingerprint_mismatch_fails",
        "test_migration_contract_mismatch_fails",
        "test_schema_diff_is_bounded_safe_metadata",
        "test_missing_identity_check_fails",
        "test_primary_key_or_replica_identity_mismatch_fails",
        "test_missing_sequence_binding_fails",
        "test_sequence_position_only_failure_does_not_block_schema_gate",
        "test_stale_telemetry_fails_closed",
        "test_malformed_health_report_fails_closed",
        "test_malformed_candidate_manifest_fails_closed",
        "test_mismatched_target_fails_without_printing_target_label",
        "test_unavailable_relay_status_fails_closed",
        "test_expected_candidate_revision_mismatch_fails",
        "test_enforce_returns_nonzero_on_failure",
        "test_artifact_and_summary_are_safe_metadata_only",
    ):
        require_token(token, tests, "schema gate tests", errors)

    for token in (
        "workflow_dispatch:",
        "failover_attempt_id:",
        "candidate_application_revision:",
        "confirmation:",
        "evaluate-standby-schema-gate",
        "enforce:",
        "environment: production-backend",
        "runs-on: ubuntu-latest",
        "CONFIRMATION: ${{ inputs.confirmation }}",
        "CANDIDATE_APPLICATION_REVISION: ${{ inputs.candidate_application_revision }}",
        "FAILOVER_ATTEMPT_ID: ${{ inputs.failover_attempt_id }}",
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "repository: ramideltoro/nutsnews",
        "ref: ${{ env.CANDIDATE_APPLICATION_REVISION }}",
        "path: app-candidate",
        "python3 scripts/backend_health_report.py",
        "--ssh-key \"$HOME/.ssh/nutsnews_backend_schema_gate\"",
        "> \"$RUNNER_TEMP/backend-health-report.stdout\"",
        "python3 scripts/backend_supabase_standby_schema_gate.py",
        "--candidate-standby-manifest app-candidate/supabase/standby_manifest.json",
        "backend-supabase-standby-schema-gate",
        "if-no-files-found: error",
    ):
        require_token(token, workflow, "schema gate workflow", errors)

    for unsafe_pattern in (
        '[[ "${{ inputs.confirmation }}"',
        '[[ "${{ inputs.failover_attempt_id }}"',
        '[[ "${{ inputs.candidate_application_revision }}"',
        '--failover-attempt-id "${{ inputs.failover_attempt_id }}"',
        '--candidate-application-revision "${{ inputs.candidate_application_revision }}"',
    ):
        require(unsafe_pattern not in workflow, f"workflow contains unsafe direct input interpolation: {unsafe_pattern}", errors)
    for forbidden in ("NUTSNEWS_BACKEND_HOST", "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", "PGPASSWORD", "postgresql://", "postgres://"):
        require(forbidden not in workflow, f"schema gate workflow must not contain credential marker: {forbidden}", errors)

    for token in (
        "python3 scripts/validate_backend_supabase_standby_schema_gate.py",
        "python3 -m unittest tests.test_backend_supabase_standby_schema_gate",
    ):
        require_token(token, checks_workflow, "backend checks workflow", errors)

    for token in (
        "Issue #524",
        "PASS",
        "FAIL",
        "schema compatibility",
        "candidate application revision",
        "safe metadata only",
        "wrong-candidate-revision",
        "sequence position-only failure remains scoped to #525",
    ):
        require_token(token, runbook, "schema gate runbook", errors)
    require("SUPABASE_STANDBY_SCHEMA_GATE.md" in readme, "README must link schema gate runbook", errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Supabase standby schema compatibility gate guardrails validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
