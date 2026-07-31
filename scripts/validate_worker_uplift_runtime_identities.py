#!/usr/bin/env python3
"""Validate the reconciled worker-uplift runtime identity inventory."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IDENTITIES_PATH = ROOT / "docs" / "worker-uplift-runtime-identities.json"
READINESS_PATH = ROOT / "docs" / "worker-uplift-runtime-readiness.json"
RUNTIME_EVIDENCE_PATH = ROOT / "docs" / "evidence" / "worker-uplift-runtime-status-2026-07-30.json"
TOPOLOGY_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "worker-uplift-topology.json.j2"
RUNTIME_DEFAULTS_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
RUNTIME_COMPOSE_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "templates" / "worker-uplift-compose.yml.j2"
PROTECTED_APPLY_PATH = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
SECURITY_REVIEW_PATH = ROOT / "docs" / "worker-uplift-security-review.json"

STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
CONSUMER_STAGES = STAGES[1:]
DISPOSITIONS = {"current", "retained", "replaced", "retired"}
EXPECTED_REPLACEMENTS = {
    "RABBITMQ_EVENTS_EXCHANGE": "source-controlled exchange id main",
    "RABBITMQ_RETRY_EXCHANGE": "source-controlled exchange id retry",
    "RABBITMQ_DLX_EXCHANGE": "source-controlled exchange id dlq",
    "NUTSNEWS_WORKER_UPLIFT_POSTGRES_DATABASE": "NUTSNEWS_BACKEND_POSTGRES_PRIMARY_SHADOW_DATABASE",
}
RETAINED_PUBLISHERS = {
    "fetcher_publisher",
    "canonicalizer_publisher",
    "enrichment_publisher",
    "approval_publisher",
    "translation_publisher",
    "persistence_publisher",
}
ACTIVE_RABBIT_IDENTITIES = {
    "scheduler": "scheduler_publisher",
    "fetcher": "fetcher_consumer",
    "canonicalizer": "canonicalizer_consumer",
    "enrichment": "enrichment_consumer",
    "approval": "approval_consumer",
    "translation": "translation_consumer",
    "persistence": "persistence_consumer",
    "publication": "publication_consumer",
}
EXPECTED_API_IDENTITIES = {
    "scheduler": ("shared_backend_read_only", "NUTSNEWS_BACKEND_API_TOKEN", False),
    "fetcher": ("none", None, False),
    "canonicalizer": ("none", None, False),
    "enrichment": ("none", None, False),
    "approval": ("qwen_provider_only", "LOCAL_AI_API_KEY", False),
    "translation": ("qwen_provider_only", "LOCAL_AI_API_KEY", False),
    "persistence": (
        "dedicated_backend_worker_api",
        "NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN",
        True,
    ),
    "publication": (
        "dedicated_backend_worker_api",
        "NUTSNEWS_BACKEND_WORKER_UPLIFT_PUBLICATION_TOKEN",
        True,
    ),
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def load_topology(path: Path = TOPOLOGY_PATH) -> dict[str, Any]:
    rendered = path.read_text(encoding="utf-8").replace(
        "{{ backend_rabbitmq_vhost }}", "nutsnews-worker-uplift"
    )
    return json.loads(rendered)


def load_runtime_defaults(path: Path = RUNTIME_DEFAULTS_PATH) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^  - name: ([a-z]+)$", text, re.MULTILINE))
    services: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end]
        runtime_mode_match = re.search(r"^    runtime_mode: ([a-z]+)$", block, re.MULTILINE)
        queues_match = re.search(
            r"^    queues:\n(?P<body>.*?)(?=^    [a-z_]+:|\Z)",
            block,
            re.MULTILINE | re.DOTALL,
        )
        queue_body = queues_match.group("body") if queues_match else ""

        def parse_queue_list(field: str) -> list[str]:
            empty = re.search(rf"^      {field}: \\[\\]$", queue_body, re.MULTILINE)
            if empty:
                return []
            section = re.search(
                rf"^      {field}:\n(?P<body>(?:        - .+\n)*)",
                queue_body,
                re.MULTILINE,
            )
            if not section:
                return []
            return re.findall(r"^        - (.+)$", section.group("body"), re.MULTILINE)

        main_match = re.search(r"^      main: (.+)$", queue_body, re.MULTILINE)
        services.append(
            {
                "name": match.group(1),
                "runtime_mode": runtime_mode_match.group(1) if runtime_mode_match else None,
                "queues": {
                    "main": main_match.group(1) if main_match else None,
                    "consumes": parse_queue_list("consumes"),
                    "publishes": parse_queue_list("publishes"),
                },
            }
        )
    return {"backend_worker_runtime_services": services}


def duplicate_items(items: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        if item in seen:
            duplicates.add(item)
        seen.add(item)
    return duplicates


def require_dispositions(
    errors: list[str], label: str, items: list[dict[str, Any]]
) -> None:
    for index, item in enumerate(items):
        disposition = item.get("disposition")
        if disposition not in DISPOSITIONS:
            errors.append(
                f"{label}[{index}] must have current/retained/replaced/retired disposition"
            )


def core_topology_identity(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "stage",
        "route_stage",
        "username_variable",
        "password_variable",
        "tags",
        "permissions",
    )
    return {key: item[key] for key in keys if key in item}


def validate_inventory(
    identities: dict[str, Any],
    readiness: dict[str, Any],
    runtime_evidence: dict[str, Any],
    topology: dict[str, Any],
    runtime_defaults: dict[str, Any],
    workflow_text: str,
    compose_text: str,
    security_review: dict[str, Any],
) -> list[str]:
    errors: list[str] = []

    if identities.get("version") != 2:
        errors.append("identity inventory version must be 2")
    if identities.get("tracking_issue") != 160:
        errors.append("tracking_issue must be 160")
    if identities.get("environment") != "production-backend":
        errors.append("environment must be production-backend")
    if identities.get("capture_mode") != "source_and_value_free_deployed_evidence_reconciliation":
        errors.append("capture_mode must describe source/deployed value-free reconciliation")
    if set(identities.get("dispositions", [])) != DISPOSITIONS:
        errors.append("inventory must declare all supported dispositions exactly once")

    invariants = identities.get("safety_invariants", {})
    if invariants.get("inventory_only") is not True:
        errors.append("safety_invariants.inventory_only must be true")
    for key in (
        "credentials_rotated",
        "rabbitmq_permissions_changed",
        "services_restarted",
        "production_writes_enabled",
        "legacy_ingestion_owner_changed",
        "dns_or_failover_changed",
    ):
        if invariants.get(key) is not False:
            errors.append(f"safety_invariants.{key} must be false")

    serialized = json.dumps(identities, sort_keys=True)
    for forbidden in (
        "amqp://",
        "amqps://",
        "postgres://",
        "postgresql://",
        "authorization: bearer ",
        "BEGIN OPENSSH PRIVATE KEY",
    ):
        if forbidden.lower() in serialized.lower():
            errors.append(f"identity inventory contains forbidden credential material: {forbidden}")
    if re.search(r'"(?:secret_value|password_value|token_value|private_key_value)"\s*:', serialized):
        errors.append("identity inventory must not contain credential value fields")

    references = identities.get("credential_reference_dispositions", [])
    require_dispositions(errors, "credential_reference_dispositions", references)
    reference_names = [str(item.get("name", "")) for item in references]
    duplicates = duplicate_items(reference_names)
    if duplicates:
        errors.append(f"credential references must appear exactly once: {sorted(duplicates)}")
    reference_map = {str(item.get("name")): item for item in references}
    for name, replacement in EXPECTED_REPLACEMENTS.items():
        entry = reference_map.get(name, {})
        if entry.get("disposition") != "replaced":
            errors.append(f"{name} must be explicitly replaced")
        if entry.get("replacement") != replacement:
            errors.append(f"{name} replacement mismatch")
    for entry in references:
        if entry.get("kind") not in {"variable", "secret"}:
            errors.append(f"credential reference has invalid kind: {entry.get('name')}")
        if entry.get("kind") == "secret" and not str(entry.get("name", "")).isupper():
            errors.append(f"secret reference must be a source-controlled name: {entry.get('name')}")

    rabbit = identities.get("rabbitmq", {})
    if rabbit.get("vhost") != {
        "reference": "RABBITMQ_VHOST",
        "name": topology.get("vhost"),
        "disposition": "current",
    }:
        errors.append("RabbitMQ vhost inventory must match authoritative topology")
    require_dispositions(errors, "rabbitmq.exchanges", rabbit.get("exchanges", []))
    expected_exchanges = [
        {"id": item["id"], "name": item["name"], "type": item["type"], "disposition": "current"}
        for item in topology.get("exchanges", [])
    ]
    if rabbit.get("exchanges") != expected_exchanges:
        errors.append("RabbitMQ exchange inventory must exactly match authoritative topology")

    topology_identities = rabbit.get("topology_identities", [])
    require_dispositions(errors, "rabbitmq.topology_identities", topology_identities)
    ids = [str(item.get("id", "")) for item in topology_identities]
    if duplicate_items(ids):
        errors.append("every RabbitMQ topology identity must be represented exactly once")
    source_users = {item["id"]: item for item in topology.get("users", [])}
    inventory_users = {item.get("id"): item for item in topology_identities}
    if set(inventory_users) != set(source_users):
        errors.append(
            "RabbitMQ topology identity set mismatch: "
            f"missing={sorted(set(source_users) - set(inventory_users))} "
            f"extra={sorted(set(inventory_users) - set(source_users))}"
        )
    for identity_id, source in source_users.items():
        item = inventory_users.get(identity_id, {})
        if core_topology_identity(item) != core_topology_identity(source):
            errors.append(f"RabbitMQ identity differs from authoritative topology: {identity_id}")
        expected_disposition = "retained" if identity_id in RETAINED_PUBLISHERS | {"break_glass_admin"} else "current"
        if item.get("disposition") != expected_disposition:
            errors.append(f"RabbitMQ identity disposition mismatch: {identity_id}")
        expected_runtime = next(
            (service for service, active_id in ACTIVE_RABBIT_IDENTITIES.items() if active_id == identity_id),
            None,
        )
        if item.get("runtime_service") != expected_runtime:
            errors.append(f"RabbitMQ runtime binding mismatch: {identity_id}")
        for ref_key in ("username_variable", "password_variable"):
            ref = item.get(ref_key)
            if ref not in reference_map:
                errors.append(f"RabbitMQ identity reference missing from disposition map: {ref}")
        permissions = item.get("permissions", {})
        if identity_id != "break_glass_admin" and ".*" in permissions.values():
            errors.append(f"wildcard permission is forbidden for non-break-glass identity: {identity_id}")
        if item.get("runtime_service") and permissions.get("configure") != "^$":
            errors.append(f"runtime service identity must not configure RabbitMQ: {identity_id}")

    routes = topology.get("routes", [])
    inventory_routes = rabbit.get("route_permissions", [])
    require_dispositions(errors, "rabbitmq.route_permissions", inventory_routes)
    if len(inventory_routes) != len(routes):
        errors.append("RabbitMQ route inventory must have seven routes")
    route_by_stage = {item["route_stage"]: item for item in inventory_routes}
    if duplicate_items([str(item.get("route_id", "")) for item in inventory_routes]):
        errors.append("RabbitMQ route ids must be unique")
    for index, source_route in enumerate(routes):
        route_stage = source_route["stage"]
        item = route_by_stage.get(route_stage, {})
        if item.get("queue") != source_route["main_queue"]:
            errors.append(f"main queue mismatch for route: {route_stage}")
        if item.get("routing_key") != source_route["routing_key"]:
            errors.append(f"routing key mismatch for route: {route_stage}")
        expected_consumer = ACTIVE_RABBIT_IDENTITIES[source_route["consumer"]]
        expected_producer = ACTIVE_RABBIT_IDENTITIES[source_route["producer"]]
        if item.get("consumer_identity_id") != expected_consumer:
            errors.append(f"consumer identity mismatch for route: {route_stage}")
        if item.get("producer_identity_id") != expected_producer:
            errors.append(f"active producer identity mismatch for route: {route_stage}")
        if index:
            retained = f"{source_route['producer']}_publisher"
            if item.get("retained_producer_identity_id") != retained:
                errors.append(f"retained producer identity mismatch for route: {route_stage}")
        if item.get("disposition") != "current":
            errors.append(f"route must be current: {route_stage}")

    bindings = rabbit.get("service_bindings", [])
    require_dispositions(errors, "rabbitmq.service_bindings", bindings)
    if [item.get("service") for item in bindings] != list(STAGES):
        errors.append("RabbitMQ service bindings must cover all stages exactly once in pipeline order")
    bindings_by_service = {item.get("service"): item for item in bindings}
    source_routes_by_consumer = {item["consumer"]: item for item in routes}
    source_routes_by_producer = {item["producer"]: item for item in routes}
    for service in STAGES:
        item = bindings_by_service.get(service, {})
        identity_id = ACTIVE_RABBIT_IDENTITIES[service]
        source_identity = source_users.get(identity_id, {})
        if item.get("identity_id") != identity_id:
            errors.append(f"active RabbitMQ identity mismatch for service: {service}")
        expected_pair = [
            source_identity.get("username_variable"),
            source_identity.get("password_variable"),
        ]
        if item.get("credential_pair") != expected_pair:
            errors.append(f"RabbitMQ credential pair mismatch for service: {service}")
        if item.get("disposition") != "current":
            errors.append(f"active service binding must be current: {service}")
        if service == "scheduler":
            expected_consumes: list[str] = []
            expected_main = source_routes_by_producer[service]["main_queue"]
        else:
            expected_main = source_routes_by_consumer[service]["main_queue"]
            expected_consumes = [expected_main]
        expected_publishes = (
            [source_routes_by_producer[service]["main_queue"]]
            if service in source_routes_by_producer
            else []
        )
        if item.get("main_queue") != expected_main:
            errors.append(f"service main queue mismatch: {service}")
        if item.get("consumes") != expected_consumes:
            errors.append(f"service consume binding mismatch: {service}")
        if item.get("publishes") != expected_publishes:
            errors.append(f"service publish binding mismatch: {service}")
        workflow_snippet = (
            f'"{service}-rabbitmq-url": rabbitmq_url('
            f'"{expected_pair[0]}", "{expected_pair[1]}")'
        )
        if workflow_snippet not in workflow_text:
            errors.append(f"protected workflow RabbitMQ injection mismatch: {service}")
    for identity_id in RETAINED_PUBLISHERS:
        source = source_users.get(identity_id, {})
        forbidden_snippet = (
            f'rabbitmq_url("{source.get("username_variable")}", '
            f'"{source.get("password_variable")}")'
        )
        if forbidden_snippet in workflow_text:
            errors.append(f"retained producer is unexpectedly injected into a service: {identity_id}")

    retained_policy = rabbit.get("least_privilege_policy", {})
    if set(retained_policy.get("retained_publishers", [])) != RETAINED_PUBLISHERS:
        errors.append("least-privilege policy must enumerate retained topology-only producers")
    if retained_policy.get("wildcard_identity_allowed_only_for") != "break_glass_admin":
        errors.append("least-privilege policy must reserve wildcard permissions for break-glass")

    services = runtime_defaults.get("backend_worker_runtime_services", [])
    runtime_services = {item.get("name"): item for item in services}
    if set(runtime_services) != set(STAGES):
        errors.append("runtime defaults must contain exactly the eight uplift services")
    for service in STAGES:
        defaults = runtime_services.get(service, {})
        binding = bindings_by_service.get(service, {})
        queues = defaults.get("queues", {})
        if queues.get("main") != binding.get("main_queue"):
            errors.append(f"runtime-default main queue mismatch: {service}")
        if queues.get("consumes", []) != binding.get("consumes"):
            errors.append(f"runtime-default consume list mismatch: {service}")
        if queues.get("publishes", []) != binding.get("publishes"):
            errors.append(f"runtime-default publish list mismatch: {service}")
        if defaults.get("runtime_mode") != "shadow":
            errors.append(f"runtime service must remain shadow: {service}")
    if "user:" in compose_text:
        errors.append("compose unexpectedly overrides image-defined runtime accounts")

    evidence = identities.get("source_evidence", {})
    committed = evidence.get("committed_runtime_status", {})
    approval_free = runtime_evidence.get("approval_free_status", {})
    for field in ("run_id", "artifact_id", "artifact_digest"):
        if committed.get(field) != approval_free.get(field):
            errors.append(f"committed runtime evidence metadata mismatch: {field}")
    if committed.get("report_sha256") != approval_free.get("downloaded_json_sha256"):
        errors.append("committed runtime evidence report hash mismatch")
    if approval_free.get("status") != "pass":
        errors.append("committed deployed runtime status must pass")
    if approval_free.get("mode") != "shadow":
        errors.append("committed deployed runtime status must remain shadow")
    if approval_free.get("production_writes_enabled") is not False:
        errors.append("committed deployed runtime status must keep production writes disabled")
    observed_queues = {
        (item.get("service"), item.get("queue"))
        for item in approval_free.get("queues", [])
        if item.get("consumers", 0) > 0
    }
    expected_observed = {
        (service, bindings_by_service[service].get("main_queue"))
        for service in CONSUMER_STAGES
    }
    if observed_queues != expected_observed:
        errors.append("deployed value-free consumer evidence must match service identity bindings")
    latest = evidence.get("latest_value_free_runtime_status", {})
    if latest.get("status") != "pass" or latest.get("mode") != "shadow":
        errors.append("latest value-free runtime status must pass in shadow mode")
    if latest.get("production_writes_enabled") is not False:
        errors.append("latest value-free runtime status must keep production writes disabled")
    if latest.get("healthy_services") != 8 or latest.get("required_consumer_queues") != 7:
        errors.append("latest value-free runtime status coverage mismatch")
    if latest.get("missing_consumers") or latest.get("unverifiable_consumers"):
        errors.append("latest value-free runtime status has missing or unverifiable consumers")
    blocker = evidence.get("known_runtime_evidence_blocker", {})
    if blocker.get("issue") != "ramideltoro/nutsnews-worker#168":
        errors.append("scheduler evidence defect must remain explicitly tracked by #168")
    if blocker.get("silently_normalized") is not False:
        errors.append("scheduler evidence defect must not be silently normalized")

    postgres = identities.get("postgres", {})
    if postgres.get("database_reference") != "NUTSNEWS_BACKEND_POSTGRES_PRIMARY_SHADOW_DATABASE":
        errors.append("PostgreSQL inventory must use current primary shadow database reference")
    if postgres.get("replaced_database_reference") != "NUTSNEWS_WORKER_UPLIFT_POSTGRES_DATABASE":
        errors.append("PostgreSQL inventory must retain the replaced database reference disposition")
    roles = postgres.get("stage_roles", [])
    require_dispositions(errors, "postgres.stage_roles", roles)
    if [item.get("stage") for item in roles] != list(STAGES):
        errors.append("PostgreSQL roles must cover every service exactly once")
    if len({item.get("username_variable") for item in roles}) != 8:
        errors.append("PostgreSQL stage username references must be distinct")
    if len({item.get("password_secret") for item in roles}) != 8:
        errors.append("PostgreSQL stage password references must be distinct")
    for role in roles:
        stage = role.get("stage")
        if role.get("schema") != f"worker_uplift_{stage}":
            errors.append(f"PostgreSQL schema mismatch: {stage}")
        if role.get("disposition") != "current":
            errors.append(f"PostgreSQL stage role must be current: {stage}")
        for key in ("username_variable", "password_secret"):
            if role.get(key) not in reference_map:
                errors.append(f"PostgreSQL credential reference missing: {role.get(key)}")
        if not all("only" in grant for grant in role.get("grants", [])):
            errors.append(f"PostgreSQL grants must state bounded scope: {stage}")
    pg_policy = postgres.get("least_privilege_evidence", {})
    for key in ("shared_role_allowed", "cross_stage_grants_allowed", "public_schema_write_allowed"):
        if pg_policy.get(key) is not False:
            errors.append(f"PostgreSQL least-privilege evidence must set {key}=false")

    api_entries = identities.get("api_identities", [])
    require_dispositions(errors, "api_identities", api_entries)
    if [item.get("service") for item in api_entries] != list(STAGES):
        errors.append("API identity inventory must cover every service exactly once")
    for item in api_entries:
        service = item.get("service")
        expected = EXPECTED_API_IDENTITIES.get(service)
        actual = (
            item.get("identity_class"),
            item.get("credential_reference"),
            item.get("dedicated"),
        )
        if actual != expected:
            errors.append(f"API identity mismatch: {service}")
        if item.get("production_writes_allowed") is not False:
            errors.append(f"API identity must not authorize production writes: {service}")
        ref = item.get("credential_reference")
        if ref and ref not in reference_map:
            errors.append(f"API credential reference missing from disposition map: {service}")
    if "len({backend_api_token, persistence_token, publication_token}) != 3" not in workflow_text:
        errors.append("protected workflow must require distinct backend API credentials")

    shared = identities.get("shared_validation_identities", [])
    require_dispositions(errors, "shared_validation_identities", shared)
    shared_by_id = {item.get("id"): item for item in shared}
    if shared_by_id.get("shadow_smoke", {}).get("runtime_service_injection") is not False:
        errors.append("shadow smoke identity must not be injected into services")
    reconciliation = shared_by_id.get("reconciliation_confirmation", {})
    if reconciliation.get("services") != list(STAGES):
        errors.append("shared reconciliation identity must explicitly enumerate all services")
    if reconciliation.get("dedicated") is not False:
        errors.append("shared reconciliation identity must not be treated as dedicated")

    accounts = identities.get("host_and_runtime_accounts", {})
    host_automation = accounts.get("host_automation", [])
    require_dispositions(errors, "host_and_runtime_accounts.host_automation", host_automation)
    if len(host_automation) != 1:
        errors.append("host automation account must be represented exactly once")
    elif host_automation[0].get("account_name_recorded") is not False:
        errors.append("policy-sensitive host account name must not be recorded")
    container_accounts = accounts.get("container_accounts", [])
    require_dispositions(errors, "host_and_runtime_accounts.container_accounts", container_accounts)
    if [item.get("service") for item in container_accounts] != list(STAGES):
        errors.append("container account inventory must cover every service exactly once")
    if any(item.get("account_class") != "image-defined non-root runtime user" for item in container_accounts):
        errors.append("every service container account must remain image-defined and non-root")
    deployed_image_stages = [item.get("stage") for item in security_review.get("deployed_images", [])]
    if deployed_image_stages != list(STAGES):
        errors.append("security-review image evidence must cover every runtime account")

    telemetry = identities.get("telemetry_policy", {})
    if telemetry.get("worker_services_receive_grafana_management_credentials") is not False:
        errors.append("worker services must not receive Grafana management credentials")
    if telemetry.get("grafana_resource_management_owner") != "ramideltoro/nutsnews-infra":
        errors.append("Grafana Cloud resource ownership must remain in nutsnews-infra")
    if telemetry.get("disposition") != "current":
        errors.append("telemetry ownership disposition must be current")

    readiness_entries = {item.get("name"): item for item in readiness.get("entries", [])}
    if readiness_entries.get("LOCAL_AI_API_KEY", {}).get("readiness") != "ready":
        errors.append("credential readiness must keep LOCAL_AI_API_KEY ready and retained")
    if identities.get("validation", {}).get("local_validator") != (
        "python3 scripts/validate_worker_uplift_runtime_identities.py"
    ):
        errors.append("validation.local_validator must name this script")

    return errors


def main() -> int:
    errors = validate_inventory(
        load_json(IDENTITIES_PATH),
        load_json(READINESS_PATH),
        load_json(RUNTIME_EVIDENCE_PATH),
        load_topology(),
        load_runtime_defaults(),
        PROTECTED_APPLY_PATH.read_text(encoding="utf-8"),
        RUNTIME_COMPOSE_PATH.read_text(encoding="utf-8"),
        load_json(SECURITY_REVIEW_PATH),
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker uplift runtime identities are reconciled and valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
