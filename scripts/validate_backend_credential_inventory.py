#!/usr/bin/env python3
"""Validate the backend credential inventory schema."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "docs" / "backend-credential-inventory.json"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_inventory() -> dict:
    try:
        return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing credential inventory: {INVENTORY_PATH}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid credential inventory JSON: {exc}") from exc


def iter_secret_entries(inventory: dict):
    for group in inventory.get("secret_groups", []):
        for key in ("secrets", "conditional_secrets"):
            for secret in group.get(key, []):
                yield group, secret


def main() -> int:
    inventory = load_inventory()
    errors: list[str] = []

    if inventory.get("repository") != "ramideltoro/nutsnews-backend":
        errors.append("repository must be ramideltoro/nutsnews-backend")
    if inventory.get("environment") != "production-backend":
        errors.append("environment must be production-backend")

    names: set[str] = set()
    for variable in inventory.get("non_secret_variables", []):
        name = variable.get("name", "")
        if not NAME_RE.match(name):
            errors.append(f"invalid variable name: {name}")
        if name in names:
            errors.append(f"duplicate credential/variable name: {name}")
        names.add(name)
        for field in ("purpose", "required"):
            if field not in variable:
                errors.append(f"variable {name} missing {field}")
        if variable.get("required") and not str(variable.get("default", "")).strip():
            errors.append(f"required variable {name} must have a default")

    group_ids: set[str] = set()
    required_groups = {
        "backend_apply",
        "cloudflare",
        "grafana",
        "rabbitmq",
        "supabase",
        "restic",
        "reporting_email",
        "worker_uplift_ai",
    }
    for group in inventory.get("secret_groups", []):
        group_id = group.get("id", "")
        if not group_id:
            errors.append("secret group missing id")
        if group_id in group_ids:
            errors.append(f"duplicate secret group id: {group_id}")
        group_ids.add(group_id)
        for field in ("title", "required_for"):
            if field not in group:
                errors.append(f"secret group {group_id} missing {field}")

    missing_groups = required_groups - group_ids
    for group_id in sorted(missing_groups):
        errors.append(f"missing required secret group: {group_id}")

    for group, secret in iter_secret_entries(inventory):
        name = secret.get("name", "")
        if not NAME_RE.match(name):
            errors.append(f"invalid secret name in {group.get('id')}: {name}")
        if name in names:
            errors.append(f"duplicate credential/variable name: {name}")
        names.add(name)
        for field in ("purpose", "required", "shape"):
            if field not in secret:
                errors.append(f"secret {name} missing {field}")
        if "value" in secret or "example_value" in secret:
            errors.append(f"secret {name} must not include values")

    for group in inventory.get("secret_groups", []):
        for credential_set in group.get("credential_sets", []):
            if not credential_set.get("id"):
                errors.append(f"credential set in {group.get('id')} missing id")
            if not credential_set.get("any_of"):
                errors.append(f"credential set {credential_set.get('id')} must define any_of")
            for name in credential_set.get("any_of", []) + credential_set.get("optional", []):
                if not NAME_RE.match(name):
                    errors.append(f"invalid conditional credential name: {name}")
                if name not in names:
                    errors.append(f"conditional credential {name} is not defined as a secret")

    grafana_group = next((group for group in inventory.get("secret_groups", []) if group.get("id") == "grafana"), {})
    grafana_secret_names = {
        secret.get("name")
        for key in ("secrets", "conditional_secrets")
        for secret in grafana_group.get(key, [])
    }
    forbidden_grafana_management = {"GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"}
    reintroduced = sorted(forbidden_grafana_management & grafana_secret_names)
    if reintroduced:
        errors.append(
            "backend grafana group must not include resource-management credentials: "
            + ", ".join(reintroduced)
        )
    if "telemetry" not in " ".join(grafana_group.get("required_for", [])).lower():
        errors.append("grafana group must be scoped to backend telemetry production")
    if "ramideltoro/nutsnews-infra" not in grafana_group.get("ownership", ""):
        errors.append("grafana group must document nutsnews-infra resource ownership")

    variable_names = {item.get("name") for item in inventory.get("non_secret_variables", [])}
    if "LOCAL_AI_URL" not in variable_names:
        errors.append("backend credential inventory must include LOCAL_AI_URL")

    ai_group = next(
        (group for group in inventory.get("secret_groups", []) if group.get("id") == "worker_uplift_ai"),
        {},
    )
    ai_secret_names = {
        secret.get("name")
        for key in ("secrets", "conditional_secrets")
        for secret in ai_group.get(key, [])
    }
    if ai_secret_names != {"LOCAL_AI_API_KEY"}:
        errors.append("worker_uplift_ai must contain only the required LOCAL_AI_API_KEY source secret")
    mapping = ai_group.get("runtime_mapping", {})
    if mapping.get("status") != "ready_retained_mapped":
        errors.append("worker_uplift_ai runtime mapping must be ready_retained_mapped")
    if mapping.get("source_environment_secret") != "LOCAL_AI_API_KEY":
        errors.append("worker_uplift_ai source secret must be LOCAL_AI_API_KEY")
    if mapping.get("source_environment_variable") != "LOCAL_AI_URL":
        errors.append("worker_uplift_ai source variable must be LOCAL_AI_URL")
    expected_service_credentials = {
        (
            "approval",
            "approval-qwen-api-key",
            "approval-qwen-base-url",
            "NUTSNEWS_APPROVAL_QWEN_API_KEY",
        ),
        (
            "translation",
            "translation-qwen-api-key",
            "translation-qwen-base-url",
            "NUTSNEWS_TRANSLATION_QWEN_API_KEY",
        ),
    }
    actual_service_credentials = {
        (
            item.get("service"),
            item.get("runtime_secret"),
            item.get("runtime_base_url_secret"),
            item.get("environment_key"),
        )
        for item in mapping.get("service_credentials", [])
    }
    if actual_service_credentials != expected_service_credentials:
        errors.append("worker_uplift_ai must map the source credential to approval and translation Qwen runtime credentials")
    if "Do not retire" not in mapping.get("retirement_decision", ""):
        errors.append("worker_uplift_ai must explicitly retain LOCAL_AI_API_KEY while the runtime mappings consume it")

    actions = inventory.get("manual_provider_actions", [])
    if len(actions) < 5:
        errors.append("manual_provider_actions must document provider/dashboard steps")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend credential inventory is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
