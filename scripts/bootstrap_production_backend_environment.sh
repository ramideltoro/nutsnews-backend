#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/bootstrap_production_backend_environment.sh --dry-run
  scripts/bootstrap_production_backend_environment.sh --apply

Creates or updates the ramideltoro/nutsnews-backend production-backend GitHub
Environment, sets non-secret variables, and sets Environment secrets from
local environment variables or files.

Secret sources, in priority order:
  1. environment variable with the exact secret name
  2. file named $NUTSNEWS_BACKEND_SECRET_DIR/<SECRET_NAME>

Defaults:
  NUTSNEWS_BACKEND_SECRET_DIR=.secrets/production-backend

This script never prints secret values.
USAGE
}

MODE="${1:-}"
if [[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]]; then
  usage >&2
  exit 2
fi

REPO="${NUTSNEWS_BACKEND_REPO:-ramideltoro/nutsnews-backend}"
ENVIRONMENT="${NUTSNEWS_BACKEND_GITHUB_ENVIRONMENT:-production-backend}"
SECRET_DIR="${NUTSNEWS_BACKEND_SECRET_DIR:-.secrets/production-backend}"
INVENTORY_PATH="${NUTSNEWS_BACKEND_CREDENTIAL_INVENTORY:-docs/backend-credential-inventory.json}"
REVIEWER_LOGIN="${NUTSNEWS_BACKEND_ENV_REVIEWER:-ramideltoro}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command gh
require_command python3

reviewer_id="$(gh api "users/${REVIEWER_LOGIN}" --jq '.id')"

echo "Target repository: ${REPO}"
echo "Target environment: ${ENVIRONMENT}"
echo "Reviewer: ${REVIEWER_LOGIN}"
echo "Mode: ${MODE#--}"

if [[ "$MODE" == "--apply" ]]; then
  gh api --method PUT "repos/${REPO}/environments/${ENVIRONMENT}" --input - >/dev/null <<JSON
{
  "wait_timer": 0,
  "can_admins_bypass": false,
  "prevent_self_review": false,
  "reviewers": [
    { "type": "User", "id": ${reviewer_id} }
  ],
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
JSON

  if ! gh api "repos/${REPO}/environments/${ENVIRONMENT}/deployment-branch-policies" \
      --jq '.branch_policies[].name' | grep -Fxq main; then
    gh api --method POST "repos/${REPO}/environments/${ENVIRONMENT}/deployment-branch-policies" \
      --input - >/dev/null <<'JSON'
{
  "name": "main"
}
JSON
  fi
else
  echo "Would create/update protected GitHub Environment and main branch deployment policy."
fi

while IFS= read -r pair; do
  [[ -z "$pair" ]] && continue
  name="${pair%%=*}"
  default_value="${pair#*=}"
  value="${!name:-$default_value}"
  if [[ "$MODE" == "--apply" ]]; then
    printf '%s' "$value" | gh variable set "$name" --repo "$REPO" --env "$ENVIRONMENT" >/dev/null
    echo "Set environment variable: $name"
  else
    echo "Would set environment variable: $name"
  fi
done < <(python3 - "$INVENTORY_PATH" <<'PY'
import json
import sys
from pathlib import Path

inventory = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for item in inventory.get("non_secret_variables", []):
    print(f"{item['name']}={item.get('default', '')}")
PY
)

missing_required=()
missing_optional=()
while IFS=$'\t' read -r name required; do
  [[ -z "$name" ]] && continue
  value=""
  if [[ -n "${!name:-}" ]]; then
    value="${!name}"
  elif [[ -f "${SECRET_DIR}/${name}" ]]; then
    value="$(<"${SECRET_DIR}/${name}")"
  fi

  if [[ -z "$value" ]]; then
    if [[ "$required" == "true" ]]; then
      missing_required+=("$name")
    else
      missing_optional+=("$name")
    fi
    continue
  fi

  if [[ "$MODE" == "--apply" ]]; then
    printf '%s' "$value" | gh secret set "$name" --repo "$REPO" --env "$ENVIRONMENT" >/dev/null
    echo "Set environment secret: $name"
  else
    echo "Would set environment secret: $name"
  fi
done < <(python3 - "$INVENTORY_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

inventory = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
seen = set()
variable_defaults = {
    item["name"]: str(item.get("default", ""))
    for item in inventory.get("non_secret_variables", [])
}
restic_provider = (
    os.environ.get("NUTSNEWS_BACKEND_RESTIC_PROVIDER")
    or variable_defaults.get("NUTSNEWS_BACKEND_RESTIC_PROVIDER", "")
).strip()
required_conditionals = set()
for group in inventory.get("secret_groups", []):
    if group.get("id") != "restic":
        continue
    for credential_set in group.get("credential_sets", []):
        if credential_set.get("id") == restic_provider:
            required_conditionals.update(credential_set.get("any_of", []))

for group in inventory.get("secret_groups", []):
    for key in ("secrets", "conditional_secrets"):
        for item in group.get(key, []):
            name = item["name"]
            if name not in seen:
                seen.add(name)
                required = bool(item.get("required")) or name in required_conditionals
                print(f"{name}\t{str(required).lower()}")
PY
)

if ((${#missing_required[@]} > 0)); then
  echo "Required secrets without local values:"
  printf -- '- %s\n' "${missing_required[@]}"
  if [[ "$MODE" == "--apply" ]]; then
    echo "Environment was updated, but credential readiness is incomplete." >&2
    exit_code=1
  else
    exit_code=0
  fi
else
  exit_code=0
fi
if ((${#missing_optional[@]} > 0)); then
  echo "Optional or inactive-provider secrets without local values:"
  printf -- '- %s\n' "${missing_optional[@]}"
fi

echo "Done."
exit "$exit_code"
