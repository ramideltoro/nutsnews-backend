#!/usr/bin/env python3
"""Validate the worker-uplift runtime identity and access map."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IDENTITIES_PATH = ROOT / "docs" / "worker-uplift-runtime-identities.json"
READINESS_PATH = ROOT / "docs" / "worker-uplift-runtime-readiness.json"

EXPECTED_VARIABLES = {
    "RABBITMQ_VHOST",
    "RABBITMQ_EVENTS_EXCHANGE",
    "RABBITMQ_RETRY_EXCHANGE",
    "RABBITMQ_DLX_EXCHANGE",
    "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME",
    "RABBITMQ_MONITORING_USERNAME",
    "RABBITMQ_SCHEDULER_PUBLISHER_USERNAME",
    "RABBITMQ_FETCHER_CONSUMER_USERNAME",
    "RABBITMQ_FETCHER_PUBLISHER_USERNAME",
    "RABBITMQ_CANONICALIZER_CONSUMER_USERNAME",
    "RABBITMQ_CANONICALIZER_PUBLISHER_USERNAME",
    "RABBITMQ_ENRICHMENT_CONSUMER_USERNAME",
    "RABBITMQ_ENRICHMENT_PUBLISHER_USERNAME",
    "RABBITMQ_APPROVAL_CONSUMER_USERNAME",
    "RABBITMQ_APPROVAL_PUBLISHER_USERNAME",
    "RABBITMQ_TRANSLATION_CONSUMER_USERNAME",
    "RABBITMQ_TRANSLATION_PUBLISHER_USERNAME",
    "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME",
    "RABBITMQ_PERSISTENCE_PUBLISHER_USERNAME",
    "RABBITMQ_PUBLICATION_CONSUMER_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_DATABASE",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_SCHEDULER_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_FETCHER_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_CANONICALIZER_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_ENRICHMENT_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_APPROVAL_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_TRANSLATION_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_PERSISTENCE_USERNAME",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_PUBLICATION_USERNAME",
}

EXPECTED_SECRETS = {
    "RABBITMQ_ERLANG_COOKIE",
    "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
    "RABBITMQ_MONITORING_PASSWORD",
    "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD",
    "RABBITMQ_FETCHER_CONSUMER_PASSWORD",
    "RABBITMQ_FETCHER_PUBLISHER_PASSWORD",
    "RABBITMQ_CANONICALIZER_CONSUMER_PASSWORD",
    "RABBITMQ_CANONICALIZER_PUBLISHER_PASSWORD",
    "RABBITMQ_ENRICHMENT_CONSUMER_PASSWORD",
    "RABBITMQ_ENRICHMENT_PUBLISHER_PASSWORD",
    "RABBITMQ_APPROVAL_CONSUMER_PASSWORD",
    "RABBITMQ_APPROVAL_PUBLISHER_PASSWORD",
    "RABBITMQ_TRANSLATION_CONSUMER_PASSWORD",
    "RABBITMQ_TRANSLATION_PUBLISHER_PASSWORD",
    "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD",
    "RABBITMQ_PERSISTENCE_PUBLISHER_PASSWORD",
    "RABBITMQ_PUBLICATION_CONSUMER_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_SCHEDULER_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_FETCHER_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_CANONICALIZER_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_ENRICHMENT_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_APPROVAL_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_TRANSLATION_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_PERSISTENCE_PASSWORD",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_PUBLICATION_PASSWORD",
    "NUTSNEWS_SHADOW_SMOKE_TOKEN",
    "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN",
}

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


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def duplicate_items(items: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        if item in seen:
            duplicates.add(item)
        seen.add(item)
    return duplicates


def iter_sensitive_value_keys(node: object, path: str = "$"):
    if isinstance(node, dict):
        for key, value in node.items():
            next_path = f"{path}.{key}"
            if key in {"value", "example_value", "secret_value", "password", "token"}:
                yield next_path
            yield from iter_sensitive_value_keys(value, next_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_sensitive_value_keys(value, f"{path}[{index}]")


def validate_permissions(label: str, permissions: dict, errors: list[str]) -> None:
    for key in ("configure", "write", "read"):
        if key not in permissions:
            errors.append(f"{label} missing RabbitMQ permission: {key}")
    if permissions.get("configure") != "^$":
        errors.append(f"{label} service identity must not have configure permission")
    if ".*" in permissions.values():
        errors.append(f"{label} service identity must not use wildcard admin-style permissions")


def main() -> int:
    identities = load_json(IDENTITIES_PATH)
    readiness = load_json(READINESS_PATH)
    errors: list[str] = []

    if identities.get("tracking_issue") != 67:
        errors.append("tracking_issue must be 67")
    if identities.get("environment") != "production-backend":
        errors.append("environment must be production-backend")
    if identities.get("capture_mode") != "generated_values_stored_value_free_evidence_committed":
        errors.append("capture_mode must record generated value-free evidence")

    for path in iter_sensitive_value_keys(identities):
        errors.append(f"identity artifact must not include generated values: {path}")

    docs = set(identities.get("official_docs_retrieved", []))
    for url in (
        "https://www.rabbitmq.com/docs/access-control",
        "https://www.rabbitmq.com/docs/clustering",
        "https://www.rabbitmq.com/docs/management",
    ):
        if url not in docs:
            errors.append(f"missing RabbitMQ doc source: {url}")

    generation = identities.get("generation_summary", {})
    for field in ("values_printed_or_committed", "legacy_worker_changed", "cloudflare_changed", "rabbitmq_broker_changed", "postgres_roles_created"):
        if generation.get(field) is not False:
            errors.append(f"generation_summary.{field} must be false")

    evidence = identities.get("environment_names_present", {})
    variables = evidence.get("variables", [])
    secrets = evidence.get("secrets", [])
    variable_set = set(variables)
    secret_set = set(secrets)
    if duplicate_items(variables):
        errors.append(f"duplicate variables: {sorted(duplicate_items(variables))}")
    if duplicate_items(secrets):
        errors.append(f"duplicate secrets: {sorted(duplicate_items(secrets))}")
    if variable_set != EXPECTED_VARIABLES:
        errors.append(f"variable set mismatch: missing={sorted(EXPECTED_VARIABLES - variable_set)} extra={sorted(variable_set - EXPECTED_VARIABLES)}")
    if secret_set != EXPECTED_SECRETS:
        errors.append(f"secret set mismatch: missing={sorted(EXPECTED_SECRETS - secret_set)} extra={sorted(secret_set - EXPECTED_SECRETS)}")

    rabbit = identities.get("rabbitmq", {})
    if rabbit.get("vhost_value") != "nutsnews-worker-uplift":
        errors.append("RabbitMQ vhost must be nutsnews-worker-uplift")
    if rabbit.get("vhost_variable") != "RABBITMQ_VHOST":
        errors.append("RabbitMQ vhost variable must be RABBITMQ_VHOST")

    management = {item.get("id"): item for item in rabbit.get("management_identities", [])}
    break_glass = management.get("break_glass_admin", {})
    if break_glass.get("service_runtime_allowed") is not False:
        errors.append("break-glass admin must not be allowed in service runtime")
    if "administrator" not in break_glass.get("tags", []):
        errors.append("break-glass admin must carry administrator tag")
    monitoring = management.get("monitoring", {})
    if monitoring.get("service_runtime_allowed") is not False:
        errors.append("monitoring identity must not be allowed in service runtime")
    if "monitoring" not in monitoring.get("tags", []):
        errors.append("monitoring identity must carry monitoring tag")
    if monitoring.get("permissions") != {"configure": "^$", "write": "^$", "read": "^$"}:
        errors.append("monitoring identity must have empty object permissions")

    routes = rabbit.get("route_permissions", [])
    if len(routes) != 7:
        errors.append("RabbitMQ route permission count must be 7")
    seen_route_ids: set[str] = set()
    route_stages: set[str] = set()
    route_usernames: set[str] = set()
    route_passwords: set[str] = set()
    for route in routes:
        route_id = route.get("route_id", "")
        if route_id in seen_route_ids:
            errors.append(f"duplicate route id: {route_id}")
        seen_route_ids.add(route_id)
        if not str(route.get("queue", "")).startswith("worker.uplift."):
            errors.append(f"queue must be under worker.uplift namespace: {route_id}")
        for side in ("producer", "consumer"):
            identity = route.get(side, {})
            stage = identity.get("stage")
            if stage not in STAGES:
                errors.append(f"{route_id} {side} has unknown stage: {stage}")
            route_stages.add(stage)
            username = identity.get("username_variable", "")
            password = identity.get("password_secret", "")
            route_usernames.add(username)
            route_passwords.add(password)
            if username not in variable_set:
                errors.append(f"{route_id} {side} username variable missing from evidence: {username}")
            if password not in secret_set:
                errors.append(f"{route_id} {side} password secret missing from evidence: {password}")
            validate_permissions(f"{route_id} {side}", identity.get("permissions", {}), errors)
        producer_perms = route.get("producer", {}).get("permissions", {})
        consumer_perms = route.get("consumer", {}).get("permissions", {})
        if producer_perms.get("read") != "^$":
            errors.append(f"{route_id} producer must not read queues")
        if producer_perms.get("write") != "^worker\\.uplift\\.events$":
            errors.append(f"{route_id} producer must write only to the events exchange")
        if consumer_perms.get("read") in {"^$", ".*"}:
            errors.append(f"{route_id} consumer must read exactly its route queue")
        if consumer_perms.get("write") != "^worker\\.uplift\\.(retry|dlx)$":
            errors.append(f"{route_id} consumer write must be limited to retry/DLX exchanges")
    if STAGES - route_stages:
        errors.append(f"route permissions do not cover stages: {sorted(STAGES - route_stages)}")
    if len(route_usernames) != 14:
        errors.append("RabbitMQ service username variables must be distinct per producer/consumer identity")
    if len(route_passwords) != 14:
        errors.append("RabbitMQ service password secrets must be distinct per producer/consumer identity")

    postgres = identities.get("postgres", {})
    if postgres.get("database_variable") != "NUTSNEWS_WORKER_UPLIFT_POSTGRES_DATABASE":
        errors.append("PostgreSQL database variable mismatch")
    stage_roles = postgres.get("stage_roles", [])
    if len(stage_roles) != 8:
        errors.append("PostgreSQL stage role count must be 8")
    pg_stages = {item.get("stage") for item in stage_roles}
    if pg_stages != STAGES:
        errors.append(f"PostgreSQL stage roles must cover every stage: missing={sorted(STAGES - pg_stages)} extra={sorted(pg_stages - STAGES)}")
    pg_users = {item.get("username_variable") for item in stage_roles}
    pg_passwords = {item.get("password_secret") for item in stage_roles}
    if len(pg_users) != 8 or not pg_users.issubset(variable_set):
        errors.append("PostgreSQL stage username variables must be distinct and present")
    if len(pg_passwords) != 8 or not pg_passwords.issubset(secret_set):
        errors.append("PostgreSQL stage password secrets must be distinct and present")
    for item in stage_roles:
        if not str(item.get("schema", "")).startswith("worker_uplift_"):
            errors.append(f"stage schema must be worker_uplift_*: {item.get('stage')}")
        grants = " ".join(item.get("grants", []))
        if "only" not in grants:
            errors.append(f"stage grants must state own-boundary only: {item.get('stage')}")

    validation_secrets = {item.get("secret") for item in identities.get("validation_identities", [])}
    for name in ("NUTSNEWS_SHADOW_SMOKE_TOKEN", "NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN"):
        if name not in validation_secrets:
            errors.append(f"validation identity missing: {name}")
    for item in identities.get("validation_identities", []):
        if item.get("id") == "shadow_smoke" and item.get("runtime_service_injection") is not False:
            errors.append("shadow smoke identity must not be injected into runtime services")
        if item.get("id") == "reconciliation_confirmation" and item.get("runtime_service_injection") is not True:
            errors.append("reconciliation identity must be injected into runtime services for bearer authorization")

    telemetry = identities.get("telemetry_policy", {})
    if "GRAFANA_SERVICE_ACCOUNT_TOKEN" not in telemetry.get("grafana_management_credentials_not_for_worker_services", []):
        errors.append("Grafana management credentials must be excluded from worker services")
    if telemetry.get("grafana_resource_management_owner") != "ramideltoro/nutsnews-infra":
        errors.append("Grafana resource management owner must be nutsnews-infra")

    ai_policy = identities.get("ai_provider_policy", {})
    if ai_policy.get("local_ai", {}).get("status") != "blocked_missing_source_value":
        errors.append("LOCAL_AI_API_KEY must remain blocked until a source value is provided or rotated")
    if ai_policy.get("openai_fallback", {}).get("default") != "disabled":
        errors.append("OpenAI fallback must default to disabled")
    if ai_policy.get("openai_fallback", {}).get("new_runtime_secret_generated") is not False:
        errors.append("No new OpenAI worker-uplift runtime secret should be generated")

    readiness_entries = {item.get("name"): item for item in readiness.get("entries", [])}
    if readiness_entries.get("NUTSNEWS_SHADOW_SMOKE_TOKEN", {}).get("readiness") != "ready":
        errors.append("runtime readiness must mark NUTSNEWS_SHADOW_SMOKE_TOKEN ready after #67")
    if readiness_entries.get("LOCAL_AI_API_KEY", {}).get("readiness") != "blocked_missing_source_value":
        errors.append("runtime readiness must keep LOCAL_AI_API_KEY blocked")

    validation = identities.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_worker_uplift_runtime_identities.py":
        errors.append("validation.local_validator must name this script")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker uplift runtime identities are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
