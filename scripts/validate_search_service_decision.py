#!/usr/bin/env python3
"""Validate the backend search-service decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "backend-search-service-decision.json"
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    decision = load_json(DECISION_PATH)
    baseline = load_json(BASELINE_PATH)
    errors: list[str] = []

    if decision.get("decision") != "keep_postgres_full_text_search_now":
        errors.append("decision must remain keep_postgres_full_text_search_now until a measured need changes it")

    if decision.get("install_search_service") is not False:
        errors.append("install_search_service must be false for the current decision")

    public_ports = {int(entry["port"]) for entry in baseline.get("public_tcp_ports", [])}
    if public_ports != {22}:
        errors.append(f"service baseline must expose only SSH while search service is not installed; got {sorted(public_ports)}")

    not_deployed = set(baseline.get("not_deployed", []))
    if "search service" not in not_deployed:
        errors.append("service baseline no longer marks search service as not_deployed; update this decision")

    comparison = decision.get("comparison", {})
    for option in ("postgres_full_text", "meilisearch", "typesense", "opensearch_elasticsearch"):
        if option not in comparison:
            errors.append(f"missing search comparison option: {option}")
            continue
        if "resource_estimate" not in comparison[option]:
            errors.append(f"missing resource estimate for search option: {option}")
        if "backup_strategy" not in comparison[option]:
            errors.append(f"missing backup strategy for search option: {option}")
        if "rollback" not in comparison[option]:
            errors.append(f"missing rollback for search option: {option}")

    if comparison.get("postgres_full_text", {}).get("status") != "recommended_now":
        errors.append("Postgres full-text search must be recommended for the current phase")

    product_need = decision.get("product_need", {})
    for need in ("public_article_search", "admin_article_search", "moderation_search", "facets_typo_tolerance_semantic_search"):
        if need not in product_need:
            errors.append(f"missing product need entry: {need}")

    future_gate = decision.get("future_install_gate", {})
    for required in (
        "trigger",
        "network",
        "resource_limits",
        "rebuild_strategy",
        "health_check",
        "observability",
        "app_tracking",
    ):
        if required not in future_gate:
            errors.append(f"missing future install gate: {required}")

    if "no public search TCP port" not in future_gate.get("network", ""):
        errors.append("future network gate must explicitly forbid public search TCP exposure")

    if future_gate.get("protected_apply_required") is not True:
        errors.append("future install gate must require protected apply")

    references = set(decision.get("external_references", []))
    for required_url in (
        "https://www.postgresql.org/docs/current/textsearch-indexes.html",
        "https://meilisearch.com/docs/resources/help/faq",
        "https://typesense.org/docs/guide/system-requirements.html",
    ):
        if required_url not in references:
            errors.append(f"missing external reference: {required_url}")

    if len(decision.get("app_evidence", [])) < 3:
        errors.append("decision must include app evidence for search-service reasoning")

    if decision.get("validation", {}).get("live_verification") != "read-only until a later approved protected apply":
        errors.append("live verification must remain read-only until approved protected apply")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("search service decision is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
