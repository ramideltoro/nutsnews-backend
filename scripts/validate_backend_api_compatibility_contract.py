#!/usr/bin/env python3
"""Validate the backend API compatibility contract for DB primary migration."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "backend-api-compatibility-contract.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    contract = load_json(CONTRACT_PATH)
    errors: list[str] = []

    if contract.get("contract_id") != "backend-api-compatibility-contract":
        errors.append("contract_id must be backend-api-compatibility-contract")
    if contract.get("issue") != 111:
        errors.append("issue must be 111")
    if contract.get("decision") != "app_owned_backend_api":
        errors.append("decision must be app_owned_backend_api")
    if contract.get("production_cutover_blocker") is not True:
        errors.append("production_cutover_blocker must be true")

    chosen = contract.get("chosen_pattern", {})
    if chosen.get("reject_bare_postgres_for_clients") is not True:
        errors.append("bare PostgreSQL must be rejected for clients")
    if chosen.get("reject_supabase_js_against_bare_postgres") is not True:
        errors.append("supabase-js against bare PostgreSQL must be rejected")

    modes = {item.get("mode"): item for item in contract.get("provider_modes", [])}
    for mode in ("supabase_primary", "backend_postgres_shadow", "backend_postgres_primary"):
        if mode not in modes:
            errors.append(f"missing provider mode: {mode}")
    if modes.get("supabase_primary", {}).get("writer") != "supabase":
        errors.append("supabase_primary writer must be supabase")
    if modes.get("backend_postgres_shadow", {}).get("writer") != "supabase":
        errors.append("backend_postgres_shadow must keep Supabase as writer")
    if modes.get("backend_postgres_shadow", {}).get("serves_production_responses") is not False:
        errors.append("backend_postgres_shadow must not serve production responses")
    if modes.get("backend_postgres_primary", {}).get("requires_cutover_approval") is not True:
        errors.append("backend_postgres_primary must require cutover approval")

    capabilities = {item.get("id"): item for item in contract.get("required_capabilities", [])}
    for capability in (
        "public_feed",
        "article_detail",
        "public_search",
        "admin_readiness",
        "quota_and_engagement_writes",
        "worker_batches",
    ):
        item = capabilities.get(capability)
        if not item:
            errors.append(f"missing required capability: {capability}")
            continue
        if not item.get("backend_contract"):
            errors.append(f"missing backend contract for capability: {capability}")
        if not str(item.get("validated_by", "")).startswith("backend_postgres_smoke_tests"):
            errors.append(f"capability must map to backend smoke tests: {capability}")

    auth = contract.get("authorization_replacement", {})
    if "No database credential" not in auth.get("browser", ""):
        errors.append("browser authorization replacement must forbid database credentials")
    if "least-privilege" not in auth.get("worker", ""):
        errors.append("worker authorization replacement must require least-privilege credentials")
    if "Supabase RLS" not in auth.get("rls_replacement", ""):
        errors.append("RLS replacement must explicitly mention Supabase RLS")

    env = {item.get("name"): item for item in contract.get("required_environment", [])}
    mode_env = env.get("NUTSNEWS_DATABASE_PROVIDER_MODE", {})
    if mode_env.get("safe_default") != "supabase_primary":
        errors.append("NUTSNEWS_DATABASE_PROVIDER_MODE safe_default must be supabase_primary")
    if "backend_postgres_primary" not in mode_env.get("allowed_values", []):
        errors.append("provider mode env must include backend_postgres_primary")
    if "NUTSNEWS_BACKEND_API_TOKEN" not in env:
        errors.append("missing NUTSNEWS_BACKEND_API_TOKEN environment contract")

    companion_issues = set(contract.get("companion_issues", []))
    for url in (
        "https://github.com/ramideltoro/nutsnews/issues/255",
        "https://github.com/ramideltoro/nutsnews-worker/issues/27",
    ):
        if url not in companion_issues:
            errors.append(f"missing companion issue: {url}")

    blockers = "\n".join(contract.get("cutover_blockers", []))
    for required in ("non-production", "backend_postgres_shadow", "Smoke tests", "Rollback"):
        if required not in blockers:
            errors.append(f"missing cutover blocker wording: {required}")

    validation = contract.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_backend_api_compatibility_contract.py":
        errors.append("local_validator must name this validator")
    if "blocked until app and worker companion issues are implemented" not in validation.get("live_status", ""):
        errors.append("live_status must record app/worker blocker")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend API compatibility contract is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
