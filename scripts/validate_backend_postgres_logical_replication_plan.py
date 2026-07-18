#!/usr/bin/env python3
"""Validate the backend PostgreSQL logical replication plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "backend-postgres-logical-replication-plan.json"
PARITY_PATH = ROOT / "docs" / "supabase-backend-postgres-parity.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def main() -> int:
    plan = load_json(PLAN_PATH)
    parity = load_json(PARITY_PATH)
    errors: list[str] = []

    if plan.get("issue") != 109:
        errors.append("issue must be 109")
    if plan.get("safe_metadata_only") is not True:
        errors.append("safe_metadata_only must be true")

    source = plan.get("source", {})
    if source.get("direct_connection_required") is not True:
        errors.append("source direct_connection_required must be true")
    if source.get("connection_pooler_allowed") is not False:
        errors.append("source connection_pooler_allowed must be false")
    if source.get("ipv4_or_ipv6_requirement_must_be_recorded") is not True:
        errors.append("source IPv4/IPv6 requirement gate must be recorded")

    target = plan.get("target", {})
    if target.get("network_path") != "ssh_tunnel_to_loopback_postgresql":
        errors.append("target network path must use approved SSH tunnel")
    if target.get("public_5432_allowed") is not False:
        errors.append("target must forbid public 5432")

    names = plan.get("replication_names", {})
    for key in ("publication", "slot", "subscription"):
        value = names.get(key, "")
        if not value.startswith("nutsnews_backend_migration_"):
            errors.append(f"{key} must use the nutsnews_backend_migration_ prefix")

    required_tables = sorted(
        f"{item['schema']}.{item['name']}"
        for item in parity.get("required_objects", [])
        if item.get("object_type") == "table" and item.get("classification") == "migrate"
    )
    publication_tables = sorted(plan.get("publication_tables", []))
    if publication_tables != required_tables:
        errors.append(f"publication tables do not match parity manifest: expected {required_tables}, got {publication_tables}")

    replica_identity = plan.get("replica_identity", {})
    if replica_identity.get("validation_required") is not True:
        errors.append("replica identity validation is required")
    if not isinstance(replica_identity.get("tables_without_identity"), list):
        errors.append("tables_without_identity must be a list")

    rehearsal = plan.get("staging_rehearsal", {})
    if rehearsal.get("required_before_production") is not True:
        errors.append("staging rehearsal must be required before production")
    if rehearsal.get("production_writes_allowed") is not False:
        errors.append("production writes must not be allowed during replication rehearsal")
    if set(rehearsal.get("change_tests", [])) != {"insert", "update", "delete", "truncate"}:
        errors.append("staging rehearsal must cover insert, update, delete, and truncate")

    slot_checks = set(plan.get("slot_risk_checks", []))
    for required in ("pg_replication_slots.active", "retained_wal_bytes_or_lsn_lag", "backend_disk_free_bytes"):
        if required not in slot_checks:
            errors.append(f"missing slot risk check: {required}")

    if plan.get("refresh_policy", {}).get("manifest_change_required") is not True:
        errors.append("refresh policy must require manifest changes")
    if len(plan.get("rollback", [])) < 3:
        errors.append("rollback must include subscription, slot, and credential cleanup")
    if len(plan.get("live_blockers", [])) < 2:
        errors.append("live blockers must document current non-automatable setup blockers")

    references = set(plan.get("external_references", []))
    for url in (
        "https://supabase.com/docs/guides/database/postgres/setup-replication-external",
        "https://supabase.com/docs/guides/database/replication",
    ):
        if url not in references:
            errors.append(f"missing external reference: {url}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("backend PostgreSQL logical replication plan is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
