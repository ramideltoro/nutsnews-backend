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
        "supabase",
        "restic",
        "reporting_email",
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
