#!/usr/bin/env python3
"""Validate the worker-uplift runtime readiness map."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
READINESS_PATH = ROOT / "docs" / "worker-uplift-runtime-readiness.json"
CLOUDFLARE_INVENTORY_PATH = ROOT / "docs" / "worker-uplift-cloudflare-inventory.json"
BACKEND_INVENTORY_PATH = ROOT / "docs" / "backend-credential-inventory.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def iter_sensitive_value_keys(node: object, path: str = "$"):
    if isinstance(node, dict):
        for key, value in node.items():
            next_path = f"{path}.{key}"
            if key in {"value", "example_value", "secret_value", "token_value"}:
                yield next_path
            yield from iter_sensitive_value_keys(value, next_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_sensitive_value_keys(value, f"{path}[{index}]")


def backend_inventory_names(inventory: dict) -> tuple[set[str], dict[str, dict]]:
    variables = {item.get("name"): item for item in inventory.get("non_secret_variables", [])}
    names = set(variables)
    secrets_by_group: dict[str, dict] = {}
    for group in inventory.get("secret_groups", []):
        for key in ("secrets", "conditional_secrets"):
            for secret in group.get(key, []):
                name = secret.get("name")
                if name:
                    names.add(name)
                    secrets_by_group[name] = {"group": group.get("id"), **secret}
    return names, secrets_by_group


def main() -> int:
    readiness = load_json(READINESS_PATH)
    cloudflare = load_json(CLOUDFLARE_INVENTORY_PATH)
    backend = load_json(BACKEND_INVENTORY_PATH)
    errors: list[str] = []

    if readiness.get("tracking_issue") != 66:
        errors.append("tracking_issue must be 66")
    if readiness.get("environment") != "production-backend":
        errors.append("environment must be production-backend")
    if readiness.get("capture_mode") != "value_free_name_presence":
        errors.append("capture_mode must be value_free_name_presence")

    for path in iter_sensitive_value_keys(readiness):
        errors.append(f"readiness artifact must not include secret values: {path}")

    required_deps = {
        "docs/worker-uplift-cloudflare-inventory.json",
        "docs/backend-credential-inventory.json",
        "docs/worker-uplift-api-admin-compatibility-contract.json",
    }
    deps = set(readiness.get("depends_on", []))
    for dep in sorted(required_deps - deps):
        errors.append(f"missing dependency: {dep}")

    cloudflare_names = {item.get("name") for item in cloudflare.get("secret_decisions", [])}
    entry_by_source = {
        item.get("source_inventory_name"): item
        for item in readiness.get("entries", [])
        if item.get("source_inventory_name")
    }
    for name in sorted(cloudflare_names - set(entry_by_source)):
        errors.append(f"Cloudflare inventory name is not accounted for: {name}")

    backend_names, backend_secrets = backend_inventory_names(backend)
    if "NUTSNEWS_BACKEND_API_URL" not in backend_names:
        errors.append("backend credential inventory must include NUTSNEWS_BACKEND_API_URL")
    worker_token = backend_secrets.get("NUTSNEWS_BACKEND_API_TOKEN", {})
    if worker_token.get("group") != "worker_api":
        errors.append("NUTSNEWS_BACKEND_API_TOKEN must be in the worker_api group")
    if worker_token.get("required") is not True:
        errors.append("NUTSNEWS_BACKEND_API_TOKEN must be required")
    local_ai_secret = backend_secrets.get("LOCAL_AI_API_KEY", {})
    if local_ai_secret.get("group") != "worker_uplift_ai":
        errors.append("LOCAL_AI_API_KEY must be in the worker_uplift_ai backend credential group")
    if local_ai_secret.get("required") is not True:
        errors.append("LOCAL_AI_API_KEY must be required while approval and translation consume it")

    evidence = readiness.get("github_environment_evidence", {})
    variables_present = set(evidence.get("variables_present", []))
    secrets_present = set(evidence.get("secrets_present", []))
    for name in ("NUTSNEWS_BACKEND_API_URL", "NUTSNEWS_BACKEND_WORKER_API_ENABLED"):
        if name not in variables_present:
            errors.append(f"production-backend variable evidence missing: {name}")
    for name in (
        "LOCAL_AI_API_KEY",
        "NUTSNEWS_BACKEND_API_TOKEN",
        "NUTSNEWS_BACKEND_POSTGRES_WORKER_API_PASSWORD",
        "NUTSNEWS_SHADOW_SMOKE_TOKEN",
    ):
        if name not in secrets_present:
            errors.append(f"production-backend secret evidence missing: {name}")

    entries = {item.get("name"): item for item in readiness.get("entries", [])}
    if len(entries) != len(readiness.get("entries", [])):
        errors.append("runtime readiness entry names must be unique")
    for name in readiness.get("readiness_summary", {}).get("ready_now", []):
        item = entries.get(name, {})
        if item.get("readiness") != "ready":
            errors.append(f"ready_now entry is not ready: {name}")
        if "production-backend" not in item.get("location", ""):
            errors.append(f"ready_now entry must live in production-backend: {name}")

    local_ai = entries.get("LOCAL_AI_API_KEY", {})
    if local_ai.get("readiness") != "ready":
        errors.append("LOCAL_AI_API_KEY must be ready after value-free production-backend presence verification")
    if local_ai.get("retirement_status") != "retained_active_source_not_replaced":
        errors.append("LOCAL_AI_API_KEY must be explicitly retained because protected apply still consumes it")
    expected_ai_mappings = {
        ("approval", "approval-qwen-api-key", "NUTSNEWS_APPROVAL_QWEN_API_KEY"),
        ("translation", "translation-qwen-api-key", "NUTSNEWS_TRANSLATION_QWEN_API_KEY"),
    }
    actual_ai_mappings = {
        (
            item.get("service"),
            item.get("protected_apply_secret"),
            item.get("environment_key"),
        )
        for item in local_ai.get("runtime_mappings", [])
    }
    if actual_ai_mappings != expected_ai_mappings:
        errors.append("LOCAL_AI_API_KEY must map to both service-specific Qwen runtime credentials")
    if readiness.get("readiness_summary", {}).get("required_before_service_bootstrap"):
        errors.append("required_before_service_bootstrap must be empty after all required credentials are reconciled")

    shadow = entries.get("NUTSNEWS_SHADOW_SMOKE_TOKEN", {})
    if shadow.get("readiness") != "ready":
        errors.append("NUTSNEWS_SHADOW_SMOKE_TOKEN must be ready after #67 generation")
    if "production-backend" not in shadow.get("location", ""):
        errors.append("NUTSNEWS_SHADOW_SMOKE_TOKEN must live in production-backend")

    retained = set(readiness.get("readiness_summary", {}).get("not_injected_by_design", []))
    for name in retained:
        item = entries.get(name, {})
        if item.get("readiness") != "retained_not_injected":
            errors.append(f"not_injected_by_design entry must be retained_not_injected: {name}")

    summary = readiness.get("readiness_summary", {})
    summary_names = [
        name
        for key in (
            "ready_now",
            "required_before_service_bootstrap",
            "required_before_shadow_validation",
            "not_injected_by_design",
        )
        for name in summary.get(key, [])
    ]
    if len(summary_names) != len(set(summary_names)):
        errors.append("runtime readiness summary entries must appear in exactly one disposition")
    if set(summary_names) != set(entries):
        errors.append("every runtime readiness entry must have a summary disposition")
    allowed_readiness = {"ready", "retained_not_injected", "retired"}
    for name, item in entries.items():
        if item.get("readiness") not in allowed_readiness:
            errors.append(f"runtime readiness entry is not mapped, ready, retained, or retired: {name}")

    validation = readiness.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_worker_uplift_runtime_readiness.py":
        errors.append("validation.local_validator must name this script")
    if validation.get("blocked_items_are_intentional") != []:
        errors.append("runtime readiness must not retain blocked inventory entries")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker uplift runtime readiness is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
