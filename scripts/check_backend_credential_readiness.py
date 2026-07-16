#!/usr/bin/env python3
"""Check production-backend credential readiness without printing secret values."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "docs" / "backend-credential-inventory.json"


def load_inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def validate_shape(name: str, shape: str) -> str | None:
    value = os.environ.get(name, "")
    if not value.strip():
        return None

    if shape == "https_url":
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            return "must be an https URL"
    elif shape == "database_url":
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
            return "must be a postgres/postgresql URL"
    elif shape == "email":
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()):
            return "must be an email address"
    elif shape == "email_list":
        emails = [item.strip() for item in value.split(",") if item.strip()]
        if not emails or any(not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", item) for item in emails):
            return "must be one or more comma-separated email addresses"
    elif shape == "multiline_private_key":
        if "PRIVATE KEY" not in value:
            return "must look like a private key"
    elif shape == "known_hosts":
        if "65.75.201.18" not in value and "backend.nutsnews.com" not in value:
            return "must include backend host known_hosts data"
    elif shape == "id":
        if not re.match(r"^[A-Za-z0-9_-]{8,}$", value.strip()):
            return "must look like a provider id"

    return None


def group_matches(selected_groups: set[str], group_id: str) -> bool:
    return not selected_groups or group_id in selected_groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", action="append", default=[], help="Limit checks to one or more inventory groups.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    inventory = load_inventory()
    selected_groups = set(args.group)
    known_groups = {group["id"] for group in inventory.get("secret_groups", [])}
    unknown_groups = sorted(selected_groups - known_groups)
    if unknown_groups:
        result = {
            "ok": False,
            "environment": inventory["environment"],
            "unknown_groups": unknown_groups,
            "known_groups": sorted(known_groups),
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Unknown credential groups:")
            for group_id in unknown_groups:
                print(f"- {group_id}")
        return 2

    missing_required: list[str] = []
    missing_conditionals: list[str] = []
    shape_errors: list[str] = []
    checked: list[str] = []

    variables = []
    if not selected_groups:
        variables = inventory.get("non_secret_variables", [])
    for variable in variables:
        name = variable["name"]
        checked.append(name)
        if variable.get("required") and not present(name):
            missing_required.append(name)

    for group in inventory.get("secret_groups", []):
        group_id = group["id"]
        if not group_matches(selected_groups, group_id):
            continue

        for secret in group.get("secrets", []):
            name = secret["name"]
            checked.append(name)
            if secret.get("required") and not present(name):
                missing_required.append(name)
                continue
            error = validate_shape(name, secret.get("shape", "secret_text"))
            if error:
                shape_errors.append(f"{name}: {error}")

        for secret in group.get("conditional_secrets", []):
            name = secret["name"]
            if present(name):
                checked.append(name)
                error = validate_shape(name, secret.get("shape", "secret_text"))
                if error:
                    shape_errors.append(f"{name}: {error}")

        provider = os.environ.get("NUTSNEWS_BACKEND_RESTIC_PROVIDER", inventory.get("non_secret_variables", [{}])[3].get("default", "")).strip()
        if group_id == "restic":
            matching_sets = [item for item in group.get("credential_sets", []) if item["id"] == provider]
            if not matching_sets:
                missing_conditionals.append(f"NUTSNEWS_BACKEND_RESTIC_PROVIDER={provider or '<empty>'} is unsupported")
            for credential_set in matching_sets:
                required_names = credential_set.get("any_of", [])
                checked.extend(name for name in required_names if name not in checked)
                absent = [name for name in required_names if not present(name)]
                if absent:
                    missing_conditionals.append(
                        f"{credential_set['id']} provider requires: {', '.join(absent)}"
                    )

    ok = not missing_required and not missing_conditionals and not shape_errors
    result = {
        "ok": ok,
        "environment": inventory["environment"],
        "checked_names": sorted(set(checked)),
        "missing_required": sorted(set(missing_required)),
        "missing_conditionals": missing_conditionals,
        "shape_errors": shape_errors,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Credential readiness for {result['environment']}: {'ok' if ok else 'not ready'}")
        if missing_required:
            print("Missing required names:")
            for name in sorted(set(missing_required)):
                print(f"- {name}")
        if missing_conditionals:
            print("Missing conditional credential sets:")
            for item in missing_conditionals:
                print(f"- {item}")
        if shape_errors:
            print("Shape errors:")
            for item in shape_errors:
                print(f"- {item}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
