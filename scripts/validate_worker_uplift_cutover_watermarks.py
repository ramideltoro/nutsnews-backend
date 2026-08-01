#!/usr/bin/env python3
"""Validate the exact standing authorization for cutover watermarks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/worker-uplift-cutover-watermark-authorization.json"
WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-cutover-watermarks.yml"
IMPLEMENTATION = ROOT / "scripts/backend_worker_uplift_cutover_watermarks.py"
RUNBOOK = ROOT / "runbooks/WORKER_UPLIFT_CUTOVER_WATERMARKS.md"
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
MODEL_TEMPLATE = ROOT / "ansible/roles/backend_baseline/templates/worker-uplift-shadow-data-model.sql.j2"
PROTECTED_APPLY = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
IDENTITIES = ROOT / "docs/worker-uplift-runtime-identities.json"
IDENTITY_VALIDATOR = ROOT / "scripts/validate_worker_uplift_runtime_identities.py"
STAGES = [
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
]
ROLE = "nutsnews_worker_uplift_watermark"
WATERMARK_NAME = "cutover-boundary-v1"
CONFIRMATION = "refresh-worker-uplift-cutover-watermarks"
COMMENT_URL = "https://github.com/ramideltoro/nutsnews-worker/issues/174#issuecomment-5150823795"
COMMENT_SHA256 = "6b5b50b4a62a08582616195d419d9660bae11c8d24bbcf0441fb61a99e5b093b"
FORBIDDEN_OPERATIONS = {
    "delete",
    "truncate",
    "arbitrary_sql",
    "outbox_replay",
    "article_write",
    "domain_write",
    "queue_publish",
    "queue_consume",
    "queue_purge",
    "consumer_change",
    "scheduler_change",
    "schema_change",
    "infrastructure_mutation",
    "production_write_enablement",
    "cutover",
    "ingestion_owner_change",
    "legacy_worker_change",
    "dns_change",
    "failover_change",
    "risk_acceptance",
    "final_readiness_go",
    "cutover_execution",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} root must be an object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require(contract.get("schema_version") == 1, "schema_version must be 1", errors)
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews-worker#174", "tracking issue must be #174", errors)
    require(contract.get("implementation_repository") == "ramideltoro/nutsnews-backend", "implementation repository mismatch", errors)
    require(contract.get("blocks") == "ramideltoro/nutsnews-worker#166", "#174 must block #166", errors)
    authorization = contract.get("authorization", {})
    expected_authorization = {
        "kind": "standing_bounded_authorization",
        "owner_login": "ramideltoro",
        "owner_comment_url": COMMENT_URL,
        "owner_comment_body_sha256": COMMENT_SHA256,
        "per_release_owner_approval_required": False,
        "first_run_owner_approval_required": False,
        "routine_environment_wait_owner_response_required": False,
        "exact_environment_wait_api_approval_allowed": True,
        "environment": "production-backend",
        "workflow": str(WORKFLOW.relative_to(ROOT)),
        "typed_confirmation": CONFIRMATION,
        "survives_candidate_revisions": True,
        "fails_closed_on_scope_or_invariant_change": True,
        "scope_sha256": sha256_json(contract.get("scope_fingerprint")),
    }
    require(authorization == expected_authorization, "standing authorization is not exact", errors)
    expected_scope = {
        "database": "nutsnews_primary_shadow",
        "role": ROLE,
        "watermark_name": WATERMARK_NAME,
        "operation": "eight_fixed_insert_on_conflict_upserts",
        "target_row_count": 8,
        "workflow": str(WORKFLOW.relative_to(ROOT)),
        "typed_confirmation": CONFIRMATION,
        "runtime_mode": "shadow",
        "production_writes_enabled": False,
        "active_ingestion_owner": "legacy_shards",
    }
    require(contract.get("scope_fingerprint") == expected_scope, "authorization scope fingerprint changed", errors)
    targets = contract.get("targets")
    expected_targets = [
        {
            "stage": stage,
            "schema": f"worker_uplift_{stage}",
            "table": "reconciliation_watermarks",
            "watermark_name": WATERMARK_NAME,
        }
        for stage in STAGES
    ]
    require(targets == expected_targets, "watermark target set/order changed", errors)
    mutation = contract.get("mutation", {})
    require(mutation.get("operation") == "insert_on_conflict_watermark_name_update", "watermark SQL operation mismatch", errors)
    require(mutation.get("expected_row_count") == 8, "watermark row count must be eight", errors)
    require(mutation.get("idempotent") is True, "watermark upsert must be idempotent", errors)
    require(mutation.get("stale_overwrite_allowed") is False, "stale overwrite must be denied", errors)
    require(mutation.get("statement_timeout_seconds") == 30, "statement timeout must remain 30 seconds", errors)
    require(
        mutation.get("allowed_columns") == [
            "watermark_name",
            "cursor_value",
            "confirmed_message_id",
            "confirmed_at",
            "lag_count",
            "diagnostic_metadata",
            "redact_after",
        ],
        "watermark columns changed",
        errors,
    )
    for field in ("arbitrary_sql_input_allowed", "other_row_keys_allowed", "other_tables_allowed"):
        require(mutation.get(field) is False, f"{field} must remain false", errors)
    identity = contract.get("database_identity", {})
    require(identity.get("role") == ROLE, "watermark database role mismatch", errors)
    require(identity.get("password_source") == "NUTSNEWS_WORKER_UPLIFT_WATERMARK_PASSWORD", "watermark credential source mismatch", errors)
    require(identity.get("new_secret_material_required") is True, "watermark role must use dedicated protected credential material", errors)
    require(identity.get("allowed_target_privileges") == ["SELECT", "INSERT", "UPDATE"], "watermark target privileges changed", errors)
    require(identity.get("allowed_source_privileges") == ["SELECT"], "watermark source privilege changed", errors)
    require(identity.get("allowed_read_tables_per_stage") == ["inbox", "outbox", "reconciliation_watermarks"], "watermark read sources changed", errors)
    require(identity.get("forbidden_table_privileges") == ["DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"], "watermark forbidden privileges changed", errors)
    for field in (
        "other_table_mutation_privileges_allowed",
        "schema_create_allowed",
        "role_create_allowed",
        "database_create_allowed",
        "role_memberships_allowed",
        "role_inheritance_allowed",
        "row_level_security_bypass_allowed",
        "runtime_service_injection",
    ):
        require(identity.get(field) is False, f"{field} must remain false", errors)
    evidence = contract.get("evidence", {})
    require(evidence.get("value_free") is True, "watermark evidence must remain value-free", errors)
    require(evidence.get("exact_candidate_required_before_apply") is True, "exact candidate must precede apply", errors)
    require(evidence.get("maximum_age_seconds") == 900, "watermark freshness window changed", errors)
    require(evidence.get("queue_stability_sample_minimum_seconds") == 5, "queue stability minimum changed", errors)
    require(evidence.get("queue_stability_sample_maximum_seconds") == 60, "queue stability maximum changed", errors)
    for field in ("candidate_sha256_required", "workflow_run_id_required", "workflow_commit_required", "immutable_artifact_required", "redacted_results_only"):
        require(evidence.get(field) is True, f"{field} must remain true", errors)
    preconditions = contract.get("required_preconditions", {})
    expected_preconditions = {
        "runtime_mode": "shadow",
        "production_writes_enabled": False,
        "active_ingestion_owner": "legacy_shards",
        "scheduler_consumer_required": False,
        "required_consumer_count_per_main_queue": 1,
        "main_and_retry_messages": 0,
        "rabbitmq_dlq_growth": 0,
        "active_inbox_count": 0,
        "unconfirmed_outbox_count": 0,
        "retrying_outbox_count": 0,
        "dead_lettered_outbox_count": 0,
        "watermark_lag_count": 0,
        "unexpected_watermark_rows": 0,
        "terminal_failed_or_parked_inbox_allowed_when_aggregated": True,
        "terminal_failure_reason_values_allowed_in_artifacts": False,
    }
    require(preconditions == expected_preconditions, "watermark preconditions changed", errors)
    post = contract.get("post_apply_proof", {})
    require(post.get("exact_declared_watermark_rows") == 8, "post-apply row proof must be eight", errors)
    for field, value in post.items():
        if field != "exact_declared_watermark_rows":
            require(value is True if not field.endswith("_available") else value is False, f"post-apply proof {field} changed", errors)
    require(set(contract.get("forbidden_operations", [])) == FORBIDDEN_OPERATIONS, "forbidden operation set changed", errors)
    return errors


def validate_workflow(text: str) -> list[str]:
    errors: list[str] = []
    for fragment in (
        "name: Backend Worker-Uplift Cutover Watermarks",
        "workflow_dispatch:",
        "permissions:\n  contents: read",
        "cancel-in-progress: false",
        "environment: production-backend",
        "timeout-minutes: 20",
        CONFIRMATION,
        "validate_worker_uplift_cutover_watermarks.py",
        "backend_worker_uplift_cutover_watermarks.py",
        "backend_worker_uplift_stage_health_projection.py",
        "--mode dry-run",
        "--mode apply",
        "--mode verify",
        "NUTSNEWS_WORKER_UPLIFT_WATERMARK_PASSWORD",
        "--db-password-env NUTSNEWS_CUTOVER_WATERMARK_PASSWORD",
        "sudo -n /usr/local/sbin/nutsnews-worker-runtime status",
        "sudo -n /usr/local/sbin/nutsnews-worker-runtime queue-inspect",
        "actions/upload-artifact@",
        "watermark-candidate.sha256",
        "watermark-evidence.sha256",
    ):
        require(fragment in text, f"workflow missing required fragment: {fragment}", errors)
    require(text.count("environment: production-backend") == 1, "watermark workflow must use exactly one protected job", errors)
    require("pull_request_target:" not in text, "watermark workflow must not use pull_request_target", errors)
    require("schedule:" not in text, "watermark workflow must not be scheduled", errors)
    require("permissions: write-all" not in text, "watermark workflow permissions must remain read-only", errors)
    require("inputs.confirmation" in text, "watermark workflow must recheck typed confirmation", errors)
    for forbidden in (
        "CLOUDFLARE_",
        "terraform ",
        "ansible-playbook",
        "dlq-replay",
        "queue-purge",
        "curl -X POST",
        "curl -X PUT",
        "curl -X PATCH",
        "curl -X DELETE",
    ):
        if re.search(forbidden, text, re.IGNORECASE):
            errors.append(f"watermark workflow contains forbidden capability: {forbidden}")
    return errors


def load_implementation_module():
    spec = importlib.util.spec_from_file_location("worker_uplift_cutover_watermarks", IMPLEMENTATION)
    if spec is None or spec.loader is None:
        raise RuntimeError("watermark implementation cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_implementation(text: str) -> list[str]:
    errors: list[str] = []
    for fragment in (
        f'WATERMARK_ROLE = "{ROLE}"',
        f'WATERMARK_NAME = "{WATERMARK_NAME}"',
        f'APPLY_CONFIRMATION = "{CONFIRMATION}"',
        "stale evidence cannot overwrite a newer watermark",
        "exact authorized consumer count",
        "other_mutation_grants",
        "role_memberships",
        "row_level_security_bypass",
        "statement_timeout = '30s'",
        "on conflict (watermark_name) do update set",
        "exact_row_guard",
        "article_or_domain_mutation_privileges_available",
        "dns_or_failover_credentials_available",
    ):
        require(fragment in text, f"implementation missing guard: {fragment}", errors)
    require("--sql" not in text and "args.sql" not in text, "watermark implementation must not accept arbitrary SQL", errors)
    module = load_implementation_module()
    rows = [
        {
            "stage": stage,
            "schema": f"worker_uplift_{stage}",
            "watermark_name": WATERMARK_NAME,
            "cursor_value": "0",
            "confirmed_message_id": None,
            "confirmed_at": "2026-08-01T00:00:00Z",
            "lag_count": 0,
            "diagnostic_metadata": {"capturedAtUtc": "2026-08-01T00:00:00Z"},
            "redact_after": "2026-10-30T00:00:00Z",
        }
        for stage in STAGES
    ]
    sql = module.apply_sql(rows)
    require(sql.lower().count("insert into worker_uplift_") == 8, "apply SQL must contain exactly eight fixed inserts", errors)
    for stage in STAGES:
        require(f"insert into worker_uplift_{stage}.reconciliation_watermarks" in sql, f"apply SQL target missing: {stage}", errors)
    for pattern in (r"\bdelete\s+from\b", r"\btruncate\b", r"\balter\s+table\b", r"\bdrop\s+", r"\bcreate\s+(table|schema|role|database)\b"):
        if re.search(pattern, sql, re.IGNORECASE):
            errors.append(f"watermark apply SQL contains forbidden statement: {pattern}")
    return errors


def validate_ansible(defaults: str, tasks: str, template: str, protected: str) -> list[str]:
    errors: list[str] = []
    require(f"backend_worker_uplift_watermark_user: {ROLE}" in defaults, "watermark role default is missing", errors)
    require('backend_worker_uplift_watermark_password: ""' in defaults, "watermark role password default is missing", errors)
    require("Ensure worker-uplift watermark role exists" in tasks, "watermark role task is missing", errors)
    require("NOINHERIT,NOBYPASSRLS" in tasks, "watermark role must deny inherited and RLS-bypass privileges", errors)
    require("backend_worker_uplift_watermark_user_result" in tasks, "watermark role check-mode guard is missing", errors)
    require("watermark_role constant text" in template, "watermark role is missing from the model template", errors)
    require("GRANT SELECT ON TABLE %I.inbox, %I.outbox, %I.reconciliation_watermarks" in template, "watermark read grants are missing", errors)
    require("GRANT INSERT, UPDATE ON TABLE %I.reconciliation_watermarks" in template, "watermark target grants are missing", errors)
    require("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I" in template, "watermark broad revocation is missing", errors)
    require("reconciliation_watermarks_id_seq" in template, "watermark sequence grant is missing", errors)
    require(
        'extra_vars["backend_worker_uplift_watermark_password"] = os.environ["NUTSNEWS_WORKER_UPLIFT_WATERMARK_PASSWORD"]'
        in protected,
        "protected baseline must inject the watermark role credential",
        errors,
    )
    return errors


def validate_inventory(identities: dict[str, Any], validator_text: str) -> list[str]:
    errors: list[str] = []
    watermark = identities.get("postgres", {}).get("watermark_writer", {})
    require(watermark.get("role") == ROLE, "watermark identity inventory role mismatch", errors)
    require(watermark.get("fixed_row_key") == WATERMARK_NAME, "watermark identity row key mismatch", errors)
    require(watermark.get("targets") == [f"worker_uplift_{stage}.reconciliation_watermarks" for stage in STAGES], "watermark identity targets mismatch", errors)
    require(watermark.get("other_table_mutation_privileges_allowed") is False, "watermark identity must deny other mutations", errors)
    require("expected_watermark_writer" in validator_text, "runtime identity validator does not enforce watermark identity", errors)
    return errors


def validate_artifact(path: Path, contract: dict[str, Any]) -> list[str]:
    try:
        module = load_implementation_module()
        artifact = load_json(path)
        module.validate_candidate_artifact(artifact, contract)
        module.ensure_value_free(artifact, "watermark candidate artifact")
    except Exception as exc:
        return [f"watermark candidate artifact invalid: {exc}"]
    return []


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument("--artifact", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    errors: list[str] = []
    try:
        contract = load_json(args.contract)
        errors.extend(validate_contract(contract))
        errors.extend(validate_workflow(WORKFLOW.read_text(encoding="utf-8")))
        errors.extend(validate_implementation(IMPLEMENTATION.read_text(encoding="utf-8")))
        errors.extend(
            validate_ansible(
                DEFAULTS.read_text(encoding="utf-8"),
                POSTGRES_TASKS.read_text(encoding="utf-8"),
                MODEL_TEMPLATE.read_text(encoding="utf-8"),
                PROTECTED_APPLY.read_text(encoding="utf-8"),
            )
        )
        errors.extend(
            validate_inventory(
                load_json(IDENTITIES),
                IDENTITY_VALIDATOR.read_text(encoding="utf-8"),
            )
        )
        if not RUNBOOK.exists():
            errors.append("watermark runbook is missing")
        if args.artifact:
            errors.extend(validate_artifact(args.artifact, contract))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift cutover watermark authorization: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
