#!/usr/bin/env python3
"""Validate the worker-uplift production-readiness decision and NO-GO guardrails."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECISION_PATH = ROOT / "docs" / "worker-uplift-production-readiness-decision.json"
DEFAULT_BINDING_PATH = (
    ROOT / "docs" / "evidence" / "worker-uplift-cloudflare-bindings-2026-07-30.json"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

STAGES = {
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
}
SOURCE_REPOSITORIES = {
    "ramideltoro/nutsnews-backend",
    "ramideltoro/nutsnews-worker",
    "ramideltoro/nutsnews-worker-contracts",
    "ramideltoro/nutsnews-worker-runtime",
    "ramideltoro/nutsnews-worker-feed-scheduler",
    "ramideltoro/nutsnews-worker-feed-fetcher",
    "ramideltoro/nutsnews-worker-article-canonicalizer",
    "ramideltoro/nutsnews-worker-article-enrichment",
    "ramideltoro/nutsnews-worker-article-approval",
    "ramideltoro/nutsnews-worker-article-translation",
    "ramideltoro/nutsnews-worker-article-persistence",
    "ramideltoro/nutsnews-worker-article-publication",
    "ramideltoro/nutsnews-infra",
    "ramideltoro/nutsnews-docs",
    "ramideltoro/nutsnews",
}
READINESS_ITEMS = {
    "single_writer_and_write_policy",
    "exact_images_packages_contracts",
    "scoped_identities",
    "parity_window",
    "soak_capacity_cost",
    "consumer_failure_recovery",
    "empty_broker_recovery",
    "dependency_outages",
    "backup_and_isolated_restore",
    "current_protected_runtime_status",
    "grafana_alerts_logs_metrics",
    "admin_projection",
    "security_residuals",
    "operations_runbook",
    "cloudflare_failover_invariants",
    "control_implementation_plan",
    "named_readiness_approval",
}
REQUIRED_BLOCKER_ISSUES = {
    "failover_analytics_binding": "ramideltoro/nutsnews-worker#157",
    "current_parity": "ramideltoro/nutsnews-worker#158",
    "current_empty_broker_recovery": "ramideltoro/nutsnews-worker#159",
    "runtime_identity_inventory_drift": "ramideltoro/nutsnews-worker#160",
    "dependency_outage_proof": "ramideltoro/nutsnews-worker#161",
    "backup_and_isolated_restore_proof": "ramideltoro/nutsnews-worker#162",
    "authenticated_admin_deployed_proof": "ramideltoro/nutsnews-worker#163",
    "security_residual_owner_disposition": "ramideltoro/nutsnews-worker#164",
    "control_implementation_plan": "ramideltoro/nutsnews-worker#165",
    "named_readiness_approver": "ramideltoro/nutsnews-worker#125",
}
REQUIRED_BLOCKERS = set(REQUIRED_BLOCKER_ISSUES)
CURRENT_GATE_DEPENDENCIES = {
    f"ramideltoro/nutsnews-worker#{number}"
    for number in (122, 123, 124, 147, 148, 149, 157, 158, 159, 160, 161, 162, 163, 164, 165)
}
ORDERED_CONTROL_AND_EXECUTION_GATES = [
    {
        "issue": "ramideltoro/nutsnews-worker#150",
        "depends_on": ["ramideltoro/nutsnews-worker#125"],
        "purpose": "decouple_ingestion_scheduling_from_dns_failover",
    },
    {
        "issue": "ramideltoro/nutsnews-worker#126",
        "depends_on": ["ramideltoro/nutsnews-worker#150"],
        "purpose": "implement_reversible_cutover_controls",
    },
    {
        "issue": "ramideltoro/nutsnews-worker#166",
        "depends_on": ["ramideltoro/nutsnews-worker#126"],
        "purpose": "final_non_mutating_cutover_execution_readiness",
    },
    {
        "issue": "ramideltoro/nutsnews-worker#127",
        "depends_on": ["ramideltoro/nutsnews-worker#166"],
        "purpose": "execute_protected_cutover",
    },
]
REQUIRED_SECURITY_RESIDUALS = {f"SEC-124-{number:03d}" for number in range(2, 10)}
SERVICE_IDENTITY_IDS = {
    "scheduler_publisher",
    "fetcher_consumer",
    "fetcher_publisher",
    "canonicalizer_consumer",
    "canonicalizer_publisher",
    "enrichment_consumer",
    "enrichment_publisher",
    "approval_consumer",
    "approval_publisher",
    "translation_consumer",
    "translation_publisher",
    "persistence_consumer",
    "persistence_publisher",
    "publication_consumer",
}
POSTGRES_ROLES = {f"worker_uplift_{stage}" for stage in STAGES}
FORBIDDEN_VALUE_KEYS = {
    "value",
    "secret_value",
    "password",
    "token",
    "private_key",
    "account_id",
    "zone_id",
    "namespace_id",
    "connection_string",
}
FORBIDDEN_TEXT = (
    "postgres://",
    "postgresql://",
    "amqp://",
    "amqps://",
    "authorization: bearer ",
    "-----begin private key-----",
)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required evidence: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duplicate_values(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def iter_keys(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from iter_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_keys(value)


def validate_value_free(label: str, document: dict, errors: list[str]) -> None:
    forbidden_keys = sorted(set(iter_keys(document)) & FORBIDDEN_VALUE_KEYS)
    if forbidden_keys:
        errors.append(f"{label} contains forbidden value-bearing keys: {forbidden_keys}")
    text = json.dumps(document).lower()
    for marker in FORBIDDEN_TEXT:
        if marker in text:
            errors.append(f"{label} contains forbidden secret/connection marker: {marker}")


def validate_binding_evidence(proof: dict) -> list[str]:
    errors: list[str] = []
    if proof.get("schema_version") != 1:
        errors.append("binding evidence schema_version must be 1")
    if proof.get("tracking_issue") != "ramideltoro/nutsnews-worker#125":
        errors.append("binding evidence must identify nutsnews-worker#125")
    if proof.get("capture_method") != "cloudflare_workers_settings_api_read_only":
        errors.append("binding evidence must use the read-only Workers settings API")
    if proof.get("api_success") is not True:
        errors.append("binding evidence API query must succeed")
    if proof.get("state_changes_performed") is not False:
        errors.append("binding evidence must record zero state changes")

    value_policy = proof.get("value_policy", {})
    if value_policy.get("binding_names_and_types_only") is not True:
        errors.append("binding evidence must be limited to names and types")
    for field in (
        "binding_values_recorded",
        "account_identifiers_recorded",
        "zone_identifiers_recorded",
        "credentials_recorded",
    ):
        if value_policy.get(field) is not False:
            errors.append(f"value_policy.{field} must be false")

    worker = proof.get("worker", {})
    if worker.get("script_name") != "nutsnews-dns-failover":
        errors.append("binding evidence must identify nutsnews-dns-failover")
    for field in ("latest_deployment_id", "latest_version_id"):
        if not UUID_RE.fullmatch(str(worker.get(field, ""))):
            errors.append(f"worker.{field} must be a UUID")
    if worker.get("deployment_percentage") != 100:
        errors.append("latest Cloudflare Worker version must receive 100 percent")

    schedules = proof.get("deployed_schedules", {})
    if schedules.get("api_success") is not True:
        errors.append("deployed schedule query must succeed")
    if [item.get("cron") for item in schedules.get("schedules", [])] != ["* * * * *"]:
        errors.append("deployed failover schedule must remain once per minute")

    source = proof.get("source_control", {})
    if source.get("repository") != "ramideltoro/nutsnews-infra":
        errors.append("binding source repository must be nutsnews-infra")
    if not SHA_RE.fullmatch(str(source.get("commit", ""))):
        errors.append("binding source commit must be a full Git SHA")
    for field in ("wrangler_sha256", "controller_sha256"):
        if not SHA256_RE.fullmatch(str(source.get(field, ""))):
            errors.append(f"source_control.{field} must be a SHA-256")
    if source.get("failover_analytics_declared") is not False:
        errors.append("source evidence must record absent FAILOVER_ANALYTICS declaration")
    if source.get("analytics_writer_present") is not False:
        errors.append("source evidence must record absent analytics writer")

    bindings = proof.get("bindings", [])
    names = [str(item.get("name", "")) for item in bindings]
    if duplicate_values(names):
        errors.append(f"duplicate deployed binding names: {sorted(duplicate_values(names))}")
    dns_failover = next((item for item in bindings if item.get("name") == "DNS_FAILOVER"), {})
    if dns_failover.get("type") != "durable_object_namespace":
        errors.append("DNS_FAILOVER must remain a Durable Object binding")
    if dns_failover.get("class_name") != "DnsFailoverController":
        errors.append("DNS_FAILOVER must bind DnsFailoverController")
    if "FAILOVER_ANALYTICS" in names:
        errors.append("committed absence evidence cannot claim FAILOVER_ANALYTICS is deployed")

    required = proof.get("required_binding", {})
    if required.get("name") != "FAILOVER_ANALYTICS":
        errors.append("required binding must be FAILOVER_ANALYTICS")
    if required.get("expected_type") != "analytics_engine":
        errors.append("FAILOVER_ANALYTICS expected type must be analytics_engine")
    if required.get("present") is not False:
        errors.append("FAILOVER_ANALYTICS must be recorded absent")
    if required.get("disposition") != "unresolved_blocker":
        errors.append("absent FAILOVER_ANALYTICS must remain an unresolved blocker")
    if required.get("blocker_issue") != "ramideltoro/nutsnews-worker#157":
        errors.append("FAILOVER_ANALYTICS blocker must be nutsnews-worker#157")
    if required.get("named_owner_decision_that_binding_is_unnecessary") is not None:
        errors.append("binding evidence must not fabricate an owner decision")

    validate_value_free("binding evidence", proof, errors)
    return errors


def validate_decision(decision: dict, binding_proof: dict) -> list[str]:
    errors: list[str] = []
    if decision.get("schema_version") != 1:
        errors.append("decision schema_version must be 1")
    if decision.get("tracking_issue") != "ramideltoro/nutsnews-worker#125":
        errors.append("decision must identify nutsnews-worker#125")
    if decision.get("implementation_repository") != "ramideltoro/nutsnews-backend":
        errors.append("implementation repository must be nutsnews-backend")
    if decision.get("review_mode") != "non_mutating_production_readiness_decision":
        errors.append("review mode must remain non-mutating")
    if decision.get("decision_scope") != (
        "guarded_cutover_control_implementation_readiness"
    ):
        errors.append("#125 must gate guarded cutover-control implementation readiness")
    if decision.get("decision") != "no_go":
        errors.append("decision must remain NO-GO while committed blockers are open")
    if decision.get("issue_closure_authorized") is not False:
        errors.append("NO-GO must not authorize issue closure")
    if decision.get("tracking_issue_state_required") != "open":
        errors.append("NO-GO must require #125 to remain open")
    if decision.get("named_approver") is not None:
        errors.append("decision must not fabricate a named approver")
    if decision.get("risk_waivers") != []:
        errors.append("decision must not fabricate risk waivers")
    if decision.get("owner_action_required") is not True:
        errors.append("NO-GO must record required owner action")

    authority = decision.get("decision_authority", {})
    if authority.get("go_authorizes_issue") != "ramideltoro/nutsnews-worker#150":
        errors.append("#125 GO may authorize only the start of #150")
    if authority.get("go_authorizes_action") != (
        "begin_guarded_cutover_control_implementation_only"
    ):
        errors.append("#125 GO authority must be limited to guarded control implementation")
    if authority.get("missing_downstream_control_implementation_blocks_this_gate") is not False:
        errors.append("missing #150/#126 implementation must not block #125")
    expected_non_authority = {
        "cutover_execution",
        "uplift_production_writes",
        "ingestion_ownership_change",
        "legacy_ingestion_change",
        "dns_or_failover_change",
        "production_infrastructure_change",
    }
    if set(authority.get("go_does_not_authorize", [])) != expected_non_authority:
        errors.append("#125 must explicitly deny every cutover and production mutation authority")
    if authority.get("final_cutover_execution_gate") != (
        "ramideltoro/nutsnews-worker#166"
    ):
        errors.append("final cutover-execution readiness gate must be nutsnews-worker#166")
    if authority.get("cutover_execution_issue") != "ramideltoro/nutsnews-worker#127":
        errors.append("cutover execution issue must remain nutsnews-worker#127")

    graph = decision.get("dependency_graph", {})
    if graph.get("current_gate") != "ramideltoro/nutsnews-worker#125":
        errors.append("dependency graph current gate must be nutsnews-worker#125")
    current_dependencies = graph.get("current_gate_dependencies", [])
    if len(current_dependencies) != len(set(current_dependencies)):
        errors.append("dependency graph contains duplicate #125 dependencies")
    if set(current_dependencies) != CURRENT_GATE_DEPENDENCIES:
        errors.append(
            "#125 dependency graph mismatch: "
            f"missing={sorted(CURRENT_GATE_DEPENDENCIES - set(current_dependencies))} "
            f"extra={sorted(set(current_dependencies) - CURRENT_GATE_DEPENDENCIES)}"
        )
    if graph.get("ordered_control_and_execution_gates") != (
        ORDERED_CONTROL_AND_EXECUTION_GATES
    ):
        errors.append("control/execution dependency order must be #125 -> #150 -> #126 -> #166 -> #127")

    base = decision.get("review_base", {})
    if base.get("repository") != "ramideltoro/nutsnews-backend":
        errors.append("review base repository must be nutsnews-backend")
    if not SHA_RE.fullmatch(str(base.get("commit", ""))):
        errors.append("review base commit must be a full Git SHA")

    safety = decision.get("safety_invariants", {})
    required_true = {
        "legacy_worker_is_production_ingestion_owner",
        "uplift_services_are_shadow_only",
    }
    required_false = {
        "production_writes_enabled",
        "production_visibility_enabled",
        "legacy_worker_modified",
        "ingestion_ownership_changed",
        "cloudflare_or_dns_modified",
        "failover_behavior_modified",
        "cutover_performed",
        "production_infrastructure_modified",
        "github_environment_protections_modified",
        "production_data_mutated",
        "secret_values_recorded",
    }
    for field in required_true:
        if safety.get(field) is not True:
            errors.append(f"safety_invariants.{field} must be true")
    for field in required_false:
        if safety.get(field) is not False:
            errors.append(f"safety_invariants.{field} must be false")
    if safety.get("active_single_writer_repository") != "ramideltoro/nutsnews-worker":
        errors.append("legacy nutsnews-worker must remain the active single writer")
    if not SHA_RE.fullmatch(str(safety.get("active_single_writer_commit", ""))):
        errors.append("active single-writer commit must be a full Git SHA")

    source_heads = decision.get("source_heads", [])
    repo_names = [str(item.get("repository", "")) for item in source_heads]
    if duplicate_values(repo_names):
        errors.append(f"duplicate source repositories: {sorted(duplicate_values(repo_names))}")
    if set(repo_names) != SOURCE_REPOSITORIES:
        errors.append(
            "source repository scope mismatch: "
            f"missing={sorted(SOURCE_REPOSITORIES - set(repo_names))} "
            f"extra={sorted(set(repo_names) - SOURCE_REPOSITORIES)}"
        )
    for item in source_heads:
        if not SHA_RE.fullmatch(str(item.get("commit", ""))):
            errors.append(f"{item.get('repository')} source head must be a full Git SHA")

    packages = decision.get("immutable_packages", [])
    package_keys = {(item.get("repository"), item.get("version")) for item in packages}
    expected_packages = {
        ("ramideltoro/nutsnews-worker-contracts", "0.3.1"),
        ("ramideltoro/nutsnews-worker-contracts", "0.4.0"),
        ("ramideltoro/nutsnews-worker-runtime", "0.4.0"),
        ("ramideltoro/nutsnews-worker-runtime", "0.5.0"),
    }
    if package_keys != expected_packages:
        errors.append("immutable package evidence must cover both deployed contract/runtime versions")
    for item in packages:
        label = f"{item.get('repository')}@{item.get('version')}"
        if not SHA_RE.fullmatch(str(item.get("commit", ""))):
            errors.append(f"{label} commit must be a full Git SHA")
        if not isinstance(item.get("publish_run"), int):
            errors.append(f"{label} publish run must be an integer")
        if not isinstance(item.get("attestation_id"), int):
            errors.append(f"{label} attestation id must be an integer")
        if not SHA256_RE.fullmatch(str(item.get("tarball_sha256", ""))):
            errors.append(f"{label} tarball digest must be a SHA-256")

    services = decision.get("deployed_services", [])
    stages = [str(item.get("stage", "")) for item in services]
    if duplicate_values(stages):
        errors.append(f"duplicate deployed stages: {sorted(duplicate_values(stages))}")
    if set(stages) != STAGES:
        errors.append(
            f"deployed services must cover all stages: missing={sorted(STAGES - set(stages))} "
            f"extra={sorted(set(stages) - STAGES)}"
        )
    for item in services:
        stage = item.get("stage", "<unknown>")
        image_digest = str(item.get("image", "")).rsplit("@", maxsplit=1)[-1]
        if not DIGEST_RE.fullmatch(image_digest):
            errors.append(f"{stage} image must be pinned by SHA-256 digest")
        if not SHA_RE.fullmatch(str(item.get("source_commit", ""))):
            errors.append(f"{stage} source commit must be a full Git SHA")
        if item.get("mode") != "shadow":
            errors.append(f"{stage} must remain in shadow mode")
        if item.get("production_writes_enabled") is not False:
            errors.append(f"{stage} production writes must remain false")

    source_hashes = decision.get("source_control_hashes", [])
    paths = [str(item.get("path", "")) for item in source_hashes]
    if duplicate_values(paths):
        errors.append(f"duplicate source-control hash paths: {sorted(duplicate_values(paths))}")
    for item in source_hashes:
        relative_path = str(item.get("path", ""))
        expected_hash = str(item.get("sha256", ""))
        path = ROOT / relative_path
        if not path.is_file():
            errors.append(f"source-control hash target is missing: {relative_path}")
            continue
        if not SHA256_RE.fullmatch(expected_hash):
            errors.append(f"source-control hash is invalid: {relative_path}")
        elif file_sha256(path) != expected_hash:
            errors.append(f"source-control hash is stale: {relative_path}")

    identity = decision.get("identity_and_policy_evidence", {})
    if set(identity.get("rabbitmq_service_identity_ids", [])) != SERVICE_IDENTITY_IDS:
        errors.append("RabbitMQ service identity set must match the current topology")
    if set(identity.get("rabbitmq_non_service_identity_ids", [])) != {
        "break_glass_admin",
        "monitoring_canary",
    }:
        errors.append("RabbitMQ non-service identities must be break_glass_admin and monitoring_canary")
    if set(identity.get("postgres_stage_roles", [])) != POSTGRES_ROLES:
        errors.append("PostgreSQL roles must cover every stage")
    if identity.get("inventory_state") != "stale":
        errors.append("dedicated identity inventory drift must remain explicit")
    write_policy = identity.get("backend_api_write_policy", {})
    for field in (
        "runtime_api_writes_enabled",
        "publication_visibility_enabled",
        "reconciliation_apply_enabled",
        "openai_fallback_enabled",
    ):
        if write_policy.get(field) is not False:
            errors.append(f"backend_api_write_policy.{field} must be false")

    comparison = decision.get("comparison_and_soak_evidence", {})
    if comparison.get("parity", {}).get("status") != "stale":
        errors.append("superseded-image parity evidence must remain marked stale")
    soak = comparison.get("soak", {})
    if soak.get("status") != "pass" or soak.get("observed_hours", 0) < 48:
        errors.append("soak evidence must record a complete passing window")
    if soak.get("image_set_matches_current") is not True:
        errors.append("passing soak must match the current deployed image set")

    recovery = decision.get("runtime_and_recovery_evidence", {})
    fresh_status = recovery.get("fresh_status_dispatch", {})
    if fresh_status.get("status") not in {
        "waiting_for_production_backend_approval",
        "pass",
    }:
        errors.append("current-head status dispatch must be waiting for approval or pass")
    if fresh_status.get("run_id") != 30513933114:
        errors.append("current-head status evidence must identify run 30513933114")
    if fresh_status.get("head_commit") != "b619cf91504eafca21f70c5d68888563f5fca7a9":
        errors.append("current-head status evidence must identify the reviewed main commit")
    if fresh_status.get("action") != "status" or fresh_status.get("dry_run") is not True:
        errors.append("current-head runtime operation must remain read-only status/dry-run")
    if fresh_status.get("approval_bypassed") is not False:
        errors.append("production-backend approval must not be bypassed")
    if fresh_status.get("status") == "pass":
        if not isinstance(fresh_status.get("artifact_id"), int):
            errors.append("passing current-head status must record an artifact id")
        if not DIGEST_RE.fullmatch(str(fresh_status.get("artifact_digest", ""))):
            errors.append("passing current-head status must record an artifact digest")
        if fresh_status.get("mode") != "shadow":
            errors.append("passing current-head status must record shadow mode")
        if fresh_status.get("production_writes_enabled") is not False:
            errors.append("passing current-head status must record production writes false")
        if fresh_status.get("healthy_services") != 8:
            errors.append("passing current-head status must record eight healthy services")
        if fresh_status.get("consumers_per_required_queue") != 1:
            errors.append("passing current-head status must record one consumer per required queue")
    empty_broker = recovery.get("empty_broker_recovery", {})
    if empty_broker.get("status") != "stale":
        errors.append("empty-broker proof must remain stale after topology change")
    if empty_broker.get("drill_topology_sha256") == empty_broker.get(
        "current_topology_sha256"
    ):
        errors.append("empty-broker staleness must be backed by differing topology hashes")
    rollback = recovery.get("rollback_limits", {})
    for field in (
        "production_owner_rollback_workflow_exists",
        "cutover_watermark_exists",
        "rollback_deadline_defined",
        "observation_window_approved",
    ):
        if rollback.get(field) is not False:
            errors.append(f"rollback_limits.{field} must remain false")
    if rollback.get("planning_blocker_issue") != "ramideltoro/nutsnews-worker#165":
        errors.append("planned cutover parameters must be owned by nutsnews-worker#165")
    if rollback.get("downstream_control_implementation_required_before") != (
        "ramideltoro/nutsnews-worker#166"
    ):
        errors.append("downstream controls must be complete before final gate #166")
    if rollback.get("downstream_control_implementation_blocks_issue_125") is not False:
        errors.append("downstream control implementation must not block #125")

    security = decision.get("security_gate", {})
    if security.get("status") != "blocked_pending_named_owner_disposition":
        errors.append("security residuals must block pending named owner disposition")
    if set(security.get("residual_findings_requiring_gate_disposition", [])) != (
        REQUIRED_SECURITY_RESIDUALS
    ):
        errors.append("all #124 residual findings must remain in the gate")
    if security.get("named_owner_decisions") != []:
        errors.append("security gate must not fabricate owner decisions")

    failover = decision.get("cloudflare_failover", {})
    if failover.get("status") != "block":
        errors.append("Cloudflare failover evidence must block readiness")
    if failover.get("failover_analytics_binding_present") is not False:
        errors.append("decision must record FAILOVER_ANALYTICS absent")
    if failover.get("named_owner_decision_that_analytics_is_unnecessary") is not None:
        errors.append("decision must not fabricate an analytics owner decision")
    if failover.get("blocker_issue") != (
        "https://github.com/ramideltoro/nutsnews-worker/issues/157"
    ):
        errors.append("decision must link nutsnews-worker#157")
    binding_relative_path = str(failover.get("binding_evidence_path", ""))
    if binding_relative_path != (
        "docs/evidence/worker-uplift-cloudflare-bindings-2026-07-30.json"
    ):
        errors.append("decision must link the committed binding evidence")
    binding_path = ROOT / binding_relative_path
    if binding_path.is_file():
        if failover.get("binding_evidence_sha256") != file_sha256(binding_path):
            errors.append("decision binding evidence SHA-256 is stale")
    else:
        errors.append("decision binding evidence path is missing")

    readiness = decision.get("readiness_items", [])
    readiness_ids = [str(item.get("id", "")) for item in readiness]
    if duplicate_values(readiness_ids):
        errors.append(f"duplicate readiness items: {sorted(duplicate_values(readiness_ids))}")
    if set(readiness_ids) != READINESS_ITEMS:
        errors.append(
            f"readiness scope mismatch: missing={sorted(READINESS_ITEMS - set(readiness_ids))} "
            f"extra={sorted(set(readiness_ids) - READINESS_ITEMS)}"
        )
    for item in readiness:
        if item.get("status") not in {"pass", "block"}:
            errors.append(f"{item.get('id')} has invalid readiness status")
        if not item.get("evidence"):
            errors.append(f"{item.get('id')} must record evidence")
    if not any(item.get("status") == "block" for item in readiness):
        errors.append("NO-GO must contain at least one blocked readiness item")

    blockers = decision.get("blockers", [])
    blocker_ids = [str(item.get("id", "")) for item in blockers]
    if duplicate_values(blocker_ids):
        errors.append(f"duplicate blockers: {sorted(duplicate_values(blocker_ids))}")
    if set(blocker_ids) != REQUIRED_BLOCKERS:
        errors.append(
            f"blocker scope mismatch: missing={sorted(REQUIRED_BLOCKERS - set(blocker_ids))} "
            f"extra={sorted(set(blocker_ids) - REQUIRED_BLOCKERS)}"
        )
    for item in blockers:
        if item.get("status") != "open":
            errors.append(f"{item.get('id')} must remain open")
        if not item.get("owner_repository") or not item.get("required_resolution"):
            errors.append(f"{item.get('id')} must record owner and resolution")
        expected_issue = REQUIRED_BLOCKER_ISSUES.get(str(item.get("id", "")))
        if item.get("issue") != expected_issue:
            errors.append(
                f"{item.get('id')} must link canonical tracking issue {expected_issue}"
            )

    if not decision.get("reconsideration_requirements"):
        errors.append("NO-GO must record reconsideration requirements")

    validate_value_free("readiness decision", decision, errors)
    errors.extend(validate_binding_evidence(binding_proof))
    return errors


def main_args(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision", type=Path, default=DEFAULT_DECISION_PATH)
    parser.add_argument("--binding-evidence", type=Path, default=DEFAULT_BINDING_PATH)
    args = parser.parse_args(argv)

    errors = validate_decision(load_json(args.decision), load_json(args.binding_evidence))
    if errors:
        print("Worker-uplift production readiness validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Worker-uplift production readiness decision is a valid non-mutating NO-GO.")
    return 0


def main() -> int:
    return main_args(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
