#!/usr/bin/env python3
"""Validate the worker-uplift architecture ADR."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADR_PATH = ROOT / "docs" / "worker-uplift-architecture-adr.json"
IDENTITIES_PATH = ROOT / "docs" / "worker-uplift-runtime-identities.json"
API_CONTRACT_PATH = ROOT / "docs" / "worker-uplift-api-admin-compatibility-contract.json"

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

ROUTES = {
    "scheduler_to_fetcher",
    "fetcher_to_canonicalizer",
    "canonicalizer_to_enrichment",
    "enrichment_to_approval",
    "approval_to_translation",
    "translation_to_persistence",
    "persistence_to_publication",
}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    adr = load_json(ADR_PATH)
    identities = load_json(IDENTITIES_PATH)
    api_contract = load_json(API_CONTRACT_PATH)
    errors: list[str] = []

    if adr.get("adr_id") != "worker-uplift-architecture":
        errors.append("adr_id must be worker-uplift-architecture")
    if adr.get("tracking_issue") != 69:
        errors.append("tracking_issue must be 69")
    if adr.get("status") != "approved_for_implementation_not_cutover":
        errors.append("status must be approved_for_implementation_not_cutover")
    if "backend Worker DB API" not in adr.get("decision", ""):
        errors.append("decision must keep final writes behind the backend Worker DB API")

    non_decisions = "\n".join(adr.get("non_decisions", []))
    for required in ("does not enable production writes", "does not change legacy Cloudflare Worker", "does not move Grafana"):
        if required not in non_decisions:
            errors.append(f"missing non-decision guardrail: {required}")

    deps = set(adr.get("depends_on", []))
    for dep in (
        "docs/worker-uplift-legacy-parity-baseline.md",
        "docs/worker-uplift-api-admin-compatibility-contract.json",
        "docs/worker-uplift-runtime-identities.json",
        "docs/backend-production-cutover-plan.json",
    ):
        if dep not in deps:
            errors.append(f"missing dependency: {dep}")

    docs = set(adr.get("official_docs_retrieved", []))
    for url in (
        "https://www.rabbitmq.com/docs/access-control",
        "https://www.rabbitmq.com/docs/clustering",
        "https://www.rabbitmq.com/docs/management",
    ):
        if url not in docs:
            errors.append(f"missing RabbitMQ doc source: {url}")

    pipeline = adr.get("pipeline", {})
    if pipeline.get("ordered_stages") != STAGES:
        errors.append("pipeline ordered_stages must be scheduler -> fetcher -> canonicalizer -> enrichment -> approval -> translation -> persistence -> publication")
    stage_repos = pipeline.get("stage_repos", {})
    reasons = pipeline.get("reason_to_change", {})
    for stage in STAGES:
        if stage not in stage_repos:
            errors.append(f"missing stage repo: {stage}")
        if stage not in reasons:
            errors.append(f"missing reason to change: {stage}")

    message = adr.get("message_contract", {})
    if message.get("delivery") != "at_least_once":
        errors.append("delivery must be at_least_once")
    if "manual_ack" not in message.get("acknowledgement", ""):
        errors.append("manual acknowledgement must be required")
    if message.get("publisher_confirms") is not True:
        errors.append("publisher confirms must be required")
    if "bounded" not in message.get("retry", ""):
        errors.append("retry must be bounded")
    if message.get("ordering") != "no_global_ordering_assumption":
        errors.append("global ordering must be rejected")
    if message.get("message_payload") != "bounded_id_only_message":
        errors.append("messages must be bounded ID-only payloads")
    required_fields = set(message.get("message_required_fields", []))
    for field in ("message_id", "pipeline_run_id", "schema_version", "operation_version", "entity_id", "attempt"):
        if field not in required_fields:
            errors.append(f"missing message required field: {field}")
    forbidden = set(message.get("message_forbidden_fields", []))
    for field in ("article_body", "full_prompt", "secret", "credential", "bearer_token"):
        if field not in forbidden:
            errors.append(f"missing message forbidden field: {field}")
    if "DLQ" not in message.get("version_checks", ""):
        errors.append("version checks must reject stale versions to DLQ")

    state = adr.get("state_and_recovery_model", {})
    if "backend PostgreSQL stage schemas" not in state.get("authoritative_state", ""):
        errors.append("PostgreSQL stage schemas must be authoritative")
    if "not the disaster-recovery source" not in state.get("transport_role", ""):
        errors.append("RabbitMQ must not be the disaster-recovery source")
    recovery = "\n".join(state.get("broker_loss_recovery", []))
    for required in ("Rebuild RabbitMQ topology", "Replay pending stage outbox", "reconciliation watermarks"):
        if required not in recovery:
            errors.append(f"broker-loss recovery missing: {required}")
    if "message_id" not in state.get("duplicate_replay_policy", ""):
        errors.append("duplicate replay policy must include message_id")
    if "confirmed publish watermark" not in state.get("watermark_policy", ""):
        errors.append("watermark policy must include confirmed publish watermark")

    identity_routes = {route.get("route_id") for route in identities.get("rabbitmq", {}).get("route_permissions", [])}
    api_operations = set()
    for item in api_contract.get("operation_sets", {}).values():
        api_operations.update(item.get("names", []))
    stage_boundaries = adr.get("stage_boundaries", [])
    if len(stage_boundaries) != len(STAGES):
        errors.append("ADR must define exactly eight stage boundaries")
    for index, boundary in enumerate(stage_boundaries):
        stage = boundary.get("stage")
        if stage != STAGES[index]:
            errors.append(f"stage boundary order mismatch at index {index}: {stage}")
        if boundary.get("schema") != f"worker_uplift_{stage}":
            errors.append(f"stage schema mismatch: {stage}")
        for route in boundary.get("rabbitmq_produces", []) + boundary.get("rabbitmq_consumes", []):
            if route not in ROUTES or route not in identity_routes:
                errors.append(f"stage references unknown route: {stage} {route}")
        for operation in boundary.get("backend_api_operations", []):
            if operation not in api_operations:
                errors.append(f"stage references unknown backend API operation: {stage} {operation}")
        if stage not in {"persistence", "publication"} and boundary.get("final_domain_write_allowed") is not False:
            errors.append(f"only persistence/publication may request final domain writes: {stage}")
        if stage in {"persistence", "publication"} and boundary.get("final_domain_write_allowed") != "only_through_backend_worker_db_api":
            errors.append(f"{stage} final writes must go only through backend Worker DB API")

    owners = {item.get("owner_repo"): item.get("area", "") for item in adr.get("ownership", [])}
    for repo in (
        "ramideltoro/nutsnews-backend",
        "ramideltoro/nutsnews-infra",
        "ramideltoro/nutsnews",
        "ramideltoro/nutsnews-docs",
        "ramideltoro/nutsnews-worker",
    ):
        if repo not in owners:
            errors.append(f"missing owner repo: {repo}")
    if "RabbitMQ" not in owners.get("ramideltoro/nutsnews-backend", ""):
        errors.append("nutsnews-backend must own RabbitMQ")
    if "Grafana Cloud" not in owners.get("ramideltoro/nutsnews-infra", ""):
        errors.append("nutsnews-infra must own Grafana Cloud resources")

    deployment = adr.get("deployment_model", {})
    if deployment.get("rabbitmq_initial_topology") != "single RabbitMQ node":
        errors.append("initial RabbitMQ topology must be single RabbitMQ node")
    if deployment.get("single_node_ha_statement") != "A single RabbitMQ node is not high availability.":
        errors.append("ADR must state that single RabbitMQ node is not HA")
    if "managed RabbitMQ" not in deployment.get("managed_or_multinode_trigger", ""):
        errors.append("ADR must define managed/multi-node trigger")
    rejected = set(deployment.get("rejected_scope", []))
    for item in ("Kubernetes", "service mesh", "direct final domain writes from stage services"):
        if item not in rejected:
            errors.append(f"missing rejected scope: {item}")

    cutover = adr.get("shadow_cutover_and_rollback", {})
    if "Legacy Cloudflare Worker ingestion remains active" not in cutover.get("legacy_worker_status", ""):
        errors.append("legacy Worker must remain active until cutover approval")
    if "independent and active" not in cutover.get("dns_failover_status", ""):
        errors.append("DNS failover must remain independent and active")
    gates = "\n".join(cutover.get("production_write_gate", []))
    for required in ("NUTSNEWS_BACKEND_WORKER_API_WRITES_ENABLED", "cannot bypass", "translation gate"):
        if required not in gates:
            errors.append(f"production write gate missing: {required}")
    proof = "\n".join(cutover.get("proof_requirements", []))
    for required in ("fixture parity", "watermark", "backup and restore", "rollback rehearsal"):
        if required not in proof:
            errors.append(f"proof requirements missing: {required}")
    rollback = "\n".join(cutover.get("rollback", []))
    for required in ("legacy Worker", "drain consumers", "forward recovery"):
        if required not in rollback:
            errors.append(f"rollback policy missing: {required}")

    failure = adr.get("failure_behavior", {})
    for key in ("duplicate_message", "replay", "broker_loss", "poison_message", "translation_policy", "stale_version", "backend_api_failure"):
        if key not in failure:
            errors.append(f"missing failure behavior: {key}")
    if "translation_pending" not in failure.get("translation_policy", ""):
        errors.append("translation policy must preserve translation_pending")

    validation = adr.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_worker_uplift_architecture_adr.py":
        errors.append("validation.local_validator must name this script")
    if validation.get("runtime_identity_validator") != "python3 scripts/validate_worker_uplift_runtime_identities.py":
        errors.append("validation.runtime_identity_validator must name identity validator")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker uplift architecture ADR is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
