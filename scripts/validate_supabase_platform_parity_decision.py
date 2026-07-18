#!/usr/bin/env python3
"""Validate the Supabase platform parity decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "supabase-platform-parity-decision.json"
FEATURES = ("auth", "storage", "realtime", "edge_functions", "data_api_postgrest", "api_keys")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def require_path(errors: list[str], value: str, field: str) -> None:
    if not value:
        errors.append(f"missing path field: {field}")
        return
    if not (ROOT / value).exists():
        errors.append(f"{field} points to missing file: {value}")


def main() -> int:
    decision = load_json(DECISION_PATH)
    errors: list[str] = []

    if decision.get("issue") != 106:
        errors.append("issue must be 106")
    if decision.get("cutover_allowed_without_app_api_replacement") is not False:
        errors.append("cutover must remain blocked until app/API replacement exists")

    for field in ("source_inventory", "parity_manifest", "runbook"):
        require_path(errors, str(decision.get(field, "")), field)

    features = decision.get("features", {})
    for feature in FEATURES:
        item = features.get(feature)
        if not isinstance(item, dict):
            errors.append(f"missing feature decision: {feature}")
            continue
        for field in ("owner", "decision", "classification", "migration_required", "reason", "cutover_action", "validation"):
            if field not in item:
                errors.append(f"{feature} decision missing {field}")
        if len(str(item.get("reason", "")).strip()) < 30:
            errors.append(f"{feature} needs a specific reason")

    for feature in ("auth", "storage", "realtime", "edge_functions"):
        if features.get(feature, {}).get("migration_required") is not False:
            errors.append(f"{feature} must not require migration for the current decision")
    for feature in ("data_api_postgrest", "api_keys"):
        if features.get(feature, {}).get("migration_required") is not True:
            errors.append(f"{feature} must remain a required replacement")
        if features.get(feature, {}).get("classification") != "replace":
            errors.append(f"{feature} classification must be replace")

    evidence = decision.get("live_metadata_evidence", {})
    if evidence.get("production_storage_bucket_count") != 0:
        errors.append("storage decision requires current production bucket count to be zero")
    if evidence.get("production_edge_function_count") != 0:
        errors.append("edge function decision requires current production function count to be zero")
    if evidence.get("staging_edge_function_count") != 0:
        errors.append("edge function decision requires current staging function count to be zero")
    if evidence.get("safe_metadata_only") is not True:
        errors.append("live metadata evidence must be marked safe_metadata_only")

    if len(decision.get("app_evidence", [])) < 4:
        errors.append("decision must include app and worker evidence")

    companion_repos = {item.get("repository") for item in decision.get("companion_issues", []) if isinstance(item, dict)}
    for repo in ("ramideltoro/nutsnews", "ramideltoro/nutsnews-worker"):
        if repo not in companion_repos:
            errors.append(f"missing companion issue for {repo}")

    blockers = set(decision.get("cutover_blockers", []))
    for required in (
        "data_api_postgrest replacement is not implemented",
        "feature-flagged database provider switch is not implemented",
        "staging app and worker compatibility tests have not passed",
    ):
        if required not in blockers:
            errors.append(f"missing cutover blocker: {required}")

    references = set(decision.get("external_references", []))
    for url in (
        "https://supabase.com/docs/guides/platform/backups",
        "https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore",
        "https://supabase.com/docs/guides/database/replication",
    ):
        if url not in references:
            errors.append(f"missing external reference: {url}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Supabase platform parity decision is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
