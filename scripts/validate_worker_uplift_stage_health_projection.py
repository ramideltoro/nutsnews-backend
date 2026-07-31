#!/usr/bin/env python3
"""Validate the standing authorization and fixed projection workflow."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/worker-uplift-stage-health-projection-authorization.json"
WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-stage-health-projection.yml"
IMPLEMENTATION = ROOT / "scripts/backend_worker_uplift_stage_health_projection.py"
RUNBOOK = ROOT / "runbooks/WORKER_UPLIFT_STAGE_HEALTH_PROJECTION.md"
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
MODEL_TEMPLATE = ROOT / "ansible/roles/backend_baseline/templates/worker-uplift-shadow-data-model.sql.j2"
PROTECTED_APPLY = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
TARGET = "worker_uplift_final.stage_health_projections"
ROLE = "nutsnews_worker_uplift_projection"
CONFIRMATION = "refresh-worker-uplift-stage-health-projections"
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
ALLOWED_COLUMNS = [
    "stage_name",
    "active_ingestion_owner",
    "stage_status",
    "stale_status",
    "last_attempt_at",
    "last_success_at",
    "last_failure_at",
    "consecutive_failure_count",
    "throughput_per_minute",
    "latency_p50_ms",
    "latency_p95_ms",
    "retry_count",
    "dlq_count",
    "queue_age_seconds",
    "active_consumers",
    "deployment_version",
    "telemetry_version",
    "projection_version",
    "sanitized_error_code",
    "sanitized_error_message",
    "diagnostic_metadata",
    "redact_after",
    "updated_at",
]
FORBIDDEN_OPERATIONS = {
    "delete",
    "truncate",
    "arbitrary_sql",
    "article_write",
    "domain_write",
    "queue_publish",
    "queue_consume",
    "queue_purge",
    "infrastructure_mutation",
    "schema_change",
    "cutover",
    "ingestion_owner_change",
    "legacy_worker_change",
    "dns_change",
    "failover_change",
}


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
    require(contract.get("tracking_issue") == "ramideltoro/nutsnews-worker#169", "tracking issue must be #169", errors)
    authorization = contract.get("authorization", {})
    require(authorization.get("kind") == "standing_bounded_authorization", "authorization kind mismatch", errors)
    require(authorization.get("per_release_owner_approval_required") is False, "per-release approval must be false", errors)
    require(authorization.get("first_run_owner_approval_required") is False, "first-run approval must be false", errors)
    require(authorization.get("environment") == "production-backend", "protected environment mismatch", errors)
    require(authorization.get("workflow") == str(WORKFLOW.relative_to(ROOT)), "workflow path mismatch", errors)
    require(authorization.get("typed_confirmation") == CONFIRMATION, "typed confirmation mismatch", errors)
    require(authorization.get("survives_candidate_revisions") is True, "candidate revision policy must be standing", errors)
    require(authorization.get("fails_closed_on_contract_change") is True, "contract changes must fail closed", errors)

    mutation = contract.get("mutation", {})
    require(mutation.get("database") == "nutsnews_primary_shadow", "database target mismatch", errors)
    require(mutation.get("target") == TARGET, "sole mutation target mismatch", errors)
    require(mutation.get("operation") == "insert_on_conflict_stage_name_update", "upsert operation mismatch", errors)
    require(mutation.get("expected_row_count") == 8, "exact row count must be eight", errors)
    require(mutation.get("idempotent") is True, "upsert must be idempotent", errors)
    require(mutation.get("stale_overwrite_allowed") is False, "stale overwrite must be denied", errors)
    require(mutation.get("statement_timeout_seconds") == 30, "statement timeout must be 30 seconds", errors)
    require(mutation.get("allowed_columns") == ALLOWED_COLUMNS, "allowed columns must remain exact", errors)

    stages = contract.get("stages")
    require(isinstance(stages, list), "stages must be a list", errors)
    if isinstance(stages, list):
        require([item.get("name") for item in stages if isinstance(item, dict)] == STAGES, "stage set/order mismatch", errors)
        require(
            [item.get("consumer_required") for item in stages if isinstance(item, dict)] == [False] + [True] * 7,
            "consumer requirements mismatch",
            errors,
        )

    identity = contract.get("database_identity", {})
    require(identity.get("role") == ROLE, "database role mismatch", errors)
    require(
        identity.get("password_source") == "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "password source mismatch",
        errors,
    )
    require(identity.get("new_secret_material_required") is False, "contract must not require copied secret material", errors)
    require(identity.get("allowed_table_privileges") == ["SELECT", "INSERT", "UPDATE"], "allowed privileges mismatch", errors)
    require(
        identity.get("forbidden_table_privileges") == ["DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"],
        "forbidden privileges mismatch",
        errors,
    )
    require(identity.get("other_table_mutation_privileges_allowed") is False, "other table writes must be denied", errors)
    require(identity.get("schema_create_allowed") is False, "schema create must be denied", errors)
    require(identity.get("role_create_allowed") is False, "role create must be denied", errors)
    require(identity.get("database_create_allowed") is False, "database create must be denied", errors)

    dry_run = contract.get("dry_run", {})
    require(dry_run.get("approval_free") is True, "dry run must be approval-free", errors)
    require(dry_run.get("value_free") is True, "dry run must be value-free", errors)
    require(dry_run.get("exact_candidate_required") is True, "dry run must bind the exact candidate", errors)
    require(dry_run.get("maximum_evidence_age_seconds") == 900, "dry-run freshness window mismatch", errors)

    preconditions = contract.get("required_preconditions", {})
    require(preconditions.get("runtime_mode") == "shadow", "runtime mode precondition mismatch", errors)
    require(preconditions.get("production_writes_enabled") is False, "production writes precondition mismatch", errors)
    require(preconditions.get("active_ingestion_owner") == "legacy_shards", "legacy owner precondition mismatch", errors)
    require(preconditions.get("required_consumer_minimum") == 1, "consumer minimum mismatch", errors)
    require(preconditions.get("missing_consumers") == [], "missing consumer precondition must be empty", errors)
    require(preconditions.get("unverifiable_consumers") == [], "unverifiable consumer precondition must be empty", errors)

    require(set(contract.get("forbidden_operations", [])) == FORBIDDEN_OPERATIONS, "forbidden operation set mismatch", errors)
    post = contract.get("post_apply_proof", {})
    expected_post = {
        "runtime_mode_unchanged": True,
        "production_writes_enabled_false": True,
        "active_ingestion_owner_unchanged": True,
        "consumer_counts_unchanged": True,
        "queue_counts_unchanged": True,
        "runtime_manifest_digest_unchanged": True,
        "runtime_compose_digest_unchanged": True,
        "target_schema_fingerprint_unchanged": True,
        "target_rows_match_candidate": True,
        "dns_or_failover_credentials_available": False,
        "rabbitmq_mutation_credentials_available": False,
        "article_or_domain_mutation_privileges_available": False,
        "infrastructure_mutation_credentials_available": False,
    }
    require(post == expected_post, "post-apply proof contract mismatch", errors)
    audit = contract.get("audit", {})
    require(audit.get("immutable_artifact_required") is True, "immutable artifact must be required", errors)
    require(audit.get("candidate_sha256_required") is True, "candidate digest must be required", errors)
    require(audit.get("workflow_run_id_required") is True, "workflow run id must be required", errors)
    require(audit.get("workflow_commit_required") is True, "workflow commit must be required", errors)
    require(audit.get("redacted_results_only") is True, "audit results must be redacted", errors)
    return errors


def validate_workflow(text: str) -> list[str]:
    errors: list[str] = []
    required_fragments = (
        "name: Backend Worker-Uplift Stage Health Projection",
        "workflow_dispatch:",
        "permissions:\n  contents: read",
        "cancel-in-progress: false",
        "dry_run:",
        "apply:",
        "environment: production-backend",
        CONFIRMATION,
        "timeout-minutes: 15",
        "backend_worker_uplift_stage_health_projection.py",
        "validate_worker_uplift_stage_health_projection.py",
        "actions/upload-artifact@",
        "actions/download-artifact@",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "--db-password-env NUTSNEWS_STAGE_HEALTH_PROJECTION_PASSWORD",
        "--mode dry-run",
        "--mode apply",
        "--mode verify",
        "sudo -n /usr/local/sbin/nutsnews-worker-runtime",
        "--queue-kind",
        "--dry-run",
    )
    for fragment in required_fragments:
        require(fragment in text, f"workflow missing required fragment: {fragment}", errors)
    require(text.count("environment: production-backend") == 1, "only the apply job may use production-backend", errors)
    require("pull_request_target:" not in text, "workflow must not use pull_request_target", errors)
    require("schedule:" not in text, "projection refresh must not be scheduled", errors)
    require("permissions: write-all" not in text, "workflow permissions must remain read-only", errors)
    for forbidden in (
        "CLOUDFLARE_",
        "RABBITMQ_DEFAULT_PASS",
        "RABBITMQ_.*PASSWORD",
        "terraform ",
        "ansible-playbook",
        "docker compose up",
        "queue-purge",
        "dlq-replay",
        "curl -X POST",
        "curl -X PUT",
        "curl -X PATCH",
        "curl -X DELETE",
    ):
        if re.search(forbidden, text, re.IGNORECASE):
            errors.append(f"workflow contains forbidden mutation capability: {forbidden}")
    dry_run_section = text.split("  dry_run:", 1)[1].split("  apply:", 1)[0] if "  dry_run:" in text and "  apply:" in text else ""
    require("environment:" not in dry_run_section, "dry-run job must remain approval-free", errors)
    apply_section = text.split("  apply:", 1)[1] if "  apply:" in text else ""
    require("if: inputs.mode == 'apply'" in apply_section, "protected job must run only for apply mode", errors)
    require("inputs.confirmation" in apply_section, "protected job must recheck typed confirmation", errors)
    return errors


def validate_implementation(text: str) -> list[str]:
    errors: list[str] = []
    for fragment in (
        f'TARGET = "{TARGET}"',
        f'PROJECTION_ROLE = "{ROLE}"',
        f'APPLY_CONFIRMATION = "{CONFIRMATION}"',
        "on conflict (stage_name) do update set",
        "where {TARGET}.updated_at <= excluded.updated_at",
        "statement_timeout = '30s'",
        "candidate must contain exactly eight rows",
        "stale candidate cannot overwrite newer projection evidence",
        "1 / case when count(*) = 8 then 1 else 0 end",
        "apply candidate is outside the authorized freshness window",
        "schema_create_grants",
        "other_mutation_grants",
        "article_or_domain_mutation_privileges_available",
        "dns_or_failover_credentials_available",
    ):
        require(fragment in text, f"implementation missing guard: {fragment}", errors)
    mutation_sql = text.split("def apply_sql", 1)[1].split("def normalized_db_rows", 1)[0] if "def apply_sql" in text else text
    for pattern in (r"\bdelete\s+from\b", r"\btruncate\b", r"\balter\s+table\b", r"\bdrop\s+", r"\bcreate\s+(table|schema|role|database)\b"):
        if re.search(pattern, mutation_sql, re.IGNORECASE):
            errors.append(f"apply SQL contains forbidden statement pattern: {pattern}")
    require(mutation_sql.count("insert into {TARGET}") == 1, "apply SQL must contain one fixed target insert", errors)
    require("--sql" not in text and "args.sql" not in text, "implementation must not accept arbitrary SQL", errors)
    return errors


def validate_ansible(defaults: str, tasks: str, template: str, protected_apply: str) -> list[str]:
    errors: list[str] = []
    require(f"backend_worker_uplift_projection_user: {ROLE}" in defaults, "projection role default is missing", errors)
    require('backend_worker_uplift_projection_password: ""' in defaults, "projection role password default is missing", errors)
    require("Ensure worker-uplift projection role exists" in tasks, "projection role task is missing", errors)
    require("backend_worker_uplift_projection_user_result" in tasks, "projection role check-mode guard is missing", errors)
    require("backend_worker_uplift_projection_user" in template, "projection role is absent from data model template", errors)
    require("GRANT SELECT, INSERT, UPDATE ON TABLE %I.stage_health_projections" in template, "projection target grant is missing", errors)
    require("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I" in template, "projection role broad revoke is missing", errors)
    require("stage_health_projections_id_seq" in template, "projection sequence grant is missing", errors)
    require(
        'extra_vars["backend_worker_uplift_projection_password"] = os.environ["NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD"]'
        in protected_apply,
        "protected apply must source the existing protected password without copying values",
        errors,
    )
    return errors


def load_projection_module():
    spec = importlib.util.spec_from_file_location("worker_uplift_stage_health_projection", IMPLEMENTATION)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load projection implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_artifact(path: Path, contract: dict[str, Any]) -> list[str]:
    try:
        module = load_projection_module()
        artifact = load_json(path)
        module.validate_candidate_artifact(artifact, contract)
        module.ensure_value_free(artifact, "candidate artifact")
    except Exception as exc:  # validator must turn all artifact failures into one fail-closed result
        return [f"candidate artifact invalid: {exc}"]
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
        if not RUNBOOK.exists():
            errors.append("projection runbook is missing")
        if args.artifact:
            errors.extend(validate_artifact(args.artifact, contract))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift stage health projection authorization: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
