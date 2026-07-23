#!/usr/bin/env python3
"""Validate the worker-uplift API/admin compatibility contract."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "worker-uplift-api-admin-compatibility-contract.json"
API_SOURCE_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "nutsnews_worker_db_api.py"


OPERATION_SET_CONSTANTS = {
    "worker_read": "READ_OPERATIONS",
    "worker_write": "WRITE_OPERATIONS",
    "app_admin_public_read": "APP_READ_OPERATIONS",
    "app_write": "APP_WRITE_OPERATIONS",
}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def load_source_operation_sets() -> dict[str, set[str]]:
    try:
        tree = ast.parse(API_SOURCE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing API source: {API_SOURCE_PATH}") from None

    found: dict[str, set[str]] = {}
    wanted = set(OPERATION_SET_CONSTANTS.values())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                value = ast.literal_eval(node.value)
                if not isinstance(value, set) or not all(isinstance(item, str) for item in value):
                    raise SystemExit(f"{target.id} must be a set of strings")
                found[target.id] = set(value)

    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"missing operation constants in source: {', '.join(sorted(missing))}")
    return found


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    source_sets = load_source_operation_sets()
    errors: list[str] = []

    if contract.get("contract_id") != "worker-uplift-api-admin-compatibility-contract":
        errors.append("contract_id must be worker-uplift-api-admin-compatibility-contract")
    if contract.get("tracking_issue") != 140:
        errors.append("tracking_issue must be 140")
    if contract.get("status") != "read_only_contract_baseline":
        errors.append("status must be read_only_contract_baseline")

    db_state = contract.get("production_database_state", {})
    if db_state.get("current_primary") != "backend_postgres_primary":
        errors.append("current_primary must be backend_postgres_primary")
    if "rollback" not in db_state.get("supabase_state", ""):
        errors.append("Supabase state must be rollback/retirement, not the uplift target")
    if "SUPABASE_SERVICE_ROLE_KEY" not in db_state.get("service_role_policy", ""):
        errors.append("service_role_policy must explicitly reject SUPABASE_SERVICE_ROLE_KEY reuse")

    deps = set(contract.get("depends_on", []))
    for dep in (
        "docs/backend-api-compatibility-contract.json",
        "docs/worker-uplift-cloudflare-inventory.json",
        "docs/worker-uplift-legacy-parity-baseline.md",
        "ansible/roles/backend_baseline/files/nutsnews_worker_db_api.py",
    ):
        if dep not in deps:
            errors.append(f"missing dependency: {dep}")

    routes = {item.get("scope"): item for item in contract.get("routes", [])}
    worker_route = routes.get("worker", {})
    app_route = routes.get("app_admin_public", {})
    if worker_route.get("route") != "https://backend.nutsnews.com/api/worker/db/*":
        errors.append("worker route must document /api/worker/db/*")
    if app_route.get("route") != "https://backend.nutsnews.com/api/app/db/*":
        errors.append("app/admin/public route must document /api/app/db/*")
    for scope, route in routes.items():
        if "NUTSNEWS_BACKEND_API_TOKEN" not in route.get("auth", ""):
            errors.append(f"{scope} route must use NUTSNEWS_BACKEND_API_TOKEN")
        if route.get("max_body_bytes") != 2000000:
            errors.append(f"{scope} route must document 2000000 byte body limit")
        if sorted(route.get("provider_modes", [])) != ["backend_postgres_primary", "backend_postgres_shadow"]:
            errors.append(f"{scope} route must allow only backend_postgres_shadow/backend_postgres_primary")

    operation_sets = contract.get("operation_sets", {})
    for key, source_constant in OPERATION_SET_CONSTANTS.items():
        item = operation_sets.get(key, {})
        if item.get("source_constant") != source_constant:
            errors.append(f"{key} source_constant must be {source_constant}")
        documented = set(item.get("names", []))
        actual = source_sets[source_constant]
        if documented != actual:
            errors.append(
                f"{key} operation set mismatch: missing={sorted(actual - documented)} extra={sorted(documented - actual)}"
            )

    all_operations = set()
    for item in operation_sets.values():
        all_operations.update(item.get("names", []))
    for required in (
        "load-admin-production-readiness",
        "load-admin-article-reviews",
        "load-admin-ai-usage",
        "load-admin-local-ai",
        "load-admin-translation-quality",
        "load-admin-guardrails",
        "load-admin-worker-shards",
        "load-admin-rss-feed-health",
        "save-accepted-articles-batch",
        "publish-articles-batch",
        "refresh-public-feed-snapshot",
    ):
        if required not in all_operations:
            errors.append(f"required retained operation missing: {required}")

    stage_names = {item.get("stage") for item in contract.get("stage_boundaries", [])}
    for stage in ("scheduler", "fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"):
        if stage not in stage_names:
            errors.append(f"missing stage boundary: {stage}")
    for stage in contract.get("stage_boundaries", []):
        if not stage.get("stage_private_state"):
            errors.append(f"stage missing private state: {stage.get('stage')}")
        if stage.get("final_domain_write_owner") != "backend Worker DB API":
            errors.append(f"stage final writes must go through backend Worker DB API: {stage.get('stage')}")
        for operation in stage.get("backend_api_operations_initially_allowed", []):
            if operation not in all_operations:
                errors.append(f"stage references unknown operation {operation}: {stage.get('stage')}")

    shadow = contract.get("write_and_shadow_behavior", {}).get("shadow_mode", {})
    primary_guarded = contract.get("write_and_shadow_behavior", {}).get("primary_mode_guarded", {})
    if shadow.get("write_status") != 409:
        errors.append("shadow write status must be 409")
    if primary_guarded.get("writes_enabled_false_status") != 403:
        errors.append("guarded primary write status must be 403")

    translation = contract.get("translation_policy", {})
    if translation.get("required_languages") != ["fr", "ja", "de-CH", "de", "el"]:
        errors.append("translation required languages must match legacy baseline")
    if "translation_pending" not in translation.get("accepted_article_status_before_translation", ""):
        errors.append("accepted articles must remain translation_pending before translation")
    if "missingTranslations" not in translation.get("missing_translation_response", []):
        errors.append("translation gate response must include missingTranslations")

    snapshot = contract.get("snapshot_behavior", {})
    if snapshot.get("refresh_operation") != "refresh-public-feed-snapshot":
        errors.append("snapshot refresh operation must be refresh-public-feed-snapshot")
    if snapshot.get("backend_function") != "public.refresh_public_feed_snapshot()":
        errors.append("snapshot backend function must be public.refresh_public_feed_snapshot()")

    idempotency = contract.get("idempotency_and_correlation", {})
    correlation = set(idempotency.get("required_uplift_correlation_fields", []))
    for field in ("pipeline_run_id", "stage_execution_id", "source_message_id", "original_url", "operation_version"):
        if field not in correlation:
            errors.append(f"missing required correlation field: {field}")
    versioning = idempotency.get("versioning", {})
    if versioning.get("api_contract_version") != 1:
        errors.append("api contract version must be 1")

    statuses = {item.get("status") for item in contract.get("failure_semantics", [])}
    for status in (400, 401, 403, 404, 409, 413, 503, 500):
        if status not in statuses:
            errors.append(f"missing failure status: {status}")

    consumers = contract.get("admin_and_public_consumers", {})
    for section in ("public", "admin", "server_write"):
        for operation in consumers.get(section, []):
            if operation not in all_operations:
                errors.append(f"{section} consumer references unknown operation: {operation}")

    legacy = contract.get("legacy_field_policy", {})
    for field in ("original_url", "language_code", "ai_provider", "worker_id", "feed_url"):
        if field not in legacy.get("required_during_coexistence", []):
            errors.append(f"legacy coexistence field missing: {field}")
    if "Cloudflare shard index" not in legacy.get("retire_only_after_consumer_audit", []):
        errors.append("legacy Cloudflare shard fields must require consumer audit before retirement")

    validation = contract.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_worker_uplift_api_admin_contract.py":
        errors.append("validation.local_validator must name this script")
    if validation.get("no_legacy_worker_mutation") is not True:
        errors.append("contract must assert no legacy worker mutation")
    if validation.get("no_cloudflare_mutation") is not True:
        errors.append("contract must assert no Cloudflare mutation")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker uplift API/admin compatibility contract is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
