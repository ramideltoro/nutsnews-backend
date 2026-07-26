#!/usr/bin/env python3
"""Run the private backend-to-existing-Supabase standby sync relay.

The relay is intended to run on the backend host as a systemd oneshot/timer. It
reads backend PostgreSQL over loopback, writes outbound to the existing
production Supabase standby, and emits safe metadata only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from scripts import backend_supabase_standby_reconcile as reconcile
except ModuleNotFoundError:
    import backend_supabase_standby_reconcile as reconcile  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-sync-relay.json"
DEFAULT_SOURCE_ENV = "NUTSNEWS_BACKEND_PRIMARY_DB_URL"
DEFAULT_TARGET_ENV = "NUTSNEWS_PRODUCTION_SUPABASE_DB_URL"
DEFAULT_BATCH_SIZE = 500


class RelayError(Exception):
    """Safe failure marker; messages must not contain secrets or row data."""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_contract_path(path: Path) -> str:
    try:
        if path.is_absolute() and path.is_relative_to(ROOT):
            return str(path.relative_to(ROOT))
    except ValueError:
        pass
    return str(path)


def relation_identity_sql(relation: reconcile.Relation) -> str:
    return f"""
    with selected_relation as (
      select c.oid, c.relkind, c.relreplident
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = {reconcile.sql_literal(relation.schema)}
        and c.relname = {reconcile.sql_literal(relation.name)}
        and c.relkind in ('r', 'p')
    ),
    primary_key as (
      select coalesce(jsonb_agg(a.attname order by key_columns.ordinality), '[]'::jsonb) as columns
      from selected_relation relation
      join pg_index idx on idx.indrelid = relation.oid and idx.indisprimary
      join unnest(idx.indkey) with ordinality as key_columns(attnum, ordinality) on true
      join pg_attribute a on a.attrelid = relation.oid and a.attnum = key_columns.attnum
    )
    select jsonb_build_object(
      'relation_kind', relation.relkind,
      'replica_identity', relation.relreplident,
      'primary_key', coalesce((select columns from primary_key), '[]'::jsonb)
    )::text
    from selected_relation relation
    """


def parse_identity(identity_text: str | None) -> dict[str, Any] | None:
    if not identity_text:
        return None
    try:
        identity = json.loads(identity_text)
    except json.JSONDecodeError as exc:
        raise RelayError("invalid_table_identity_metadata") from exc
    if not isinstance(identity, dict):
        raise RelayError("invalid_table_identity_metadata")
    primary_key = identity.get("primary_key")
    if not isinstance(primary_key, list) or not all(isinstance(column, str) for column in primary_key):
        raise RelayError("invalid_table_identity_metadata")
    return identity


def table_primary_key(table: dict[str, Any]) -> list[str]:
    raw_key = table.get("primary_key", table.get("primaryKey", []))
    if not isinstance(raw_key, list) or not raw_key:
        raise RelayError("manifest_table_primary_key_missing")
    primary_key = [str(column) for column in raw_key]
    for column in primary_key:
        if not reconcile.IDENTIFIER_RE.fullmatch(column):
            raise RelayError("manifest_table_primary_key_invalid")
    return primary_key


def manifest_identity_check(table: dict[str, Any]) -> dict[str, Any]:
    relation = reconcile.parse_relation(str(table.get("name", "")))
    reasons: list[str] = []
    primary_key: list[str] = []
    try:
        primary_key = table_primary_key(table)
    except RelayError as exc:
        reasons.append(str(exc))
    replica_identity = table.get("replica_identity", table.get("replicaIdentity", {}))
    if not isinstance(replica_identity, dict):
        reasons.append("manifest_replica_identity_invalid")
        replica_identity = {}
    identity_type = replica_identity.get("type")
    identity_columns = replica_identity.get("columns", [])
    if table.get("row_replication", table.get("rowReplication", True)) is not True:
        reasons.append("manifest_row_replication_disabled")
    if identity_type != "primary_key":
        reasons.append("manifest_replica_identity_not_primary_key")
    if identity_columns != primary_key:
        reasons.append("manifest_replica_identity_columns_mismatch")
    return {
        "id": f"manifest-identity.{relation.id}",
        "name": relation.id,
        "status": "pass" if not reasons else "fail",
        "reasons": reasons,
        "primary_key": primary_key,
        "replica_identity_type": identity_type,
        "sensitivity": "metadata_only",
    }


def live_identity_check(table: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    relation = reconcile.parse_relation(str(table.get("name", "")))
    expected_primary_key = table_primary_key(table)
    identity_query = relation_identity_sql(relation)
    source_text, source_error = reconcile.query_value(source_db_url, identity_query)
    target_text, target_error = reconcile.query_value(target_db_url, identity_query)
    errors = {
        "source_identity_error": source_error,
        "target_identity_error": target_error,
    }
    active_errors = {key: value for key, value in errors.items() if value}
    if active_errors:
        return {
            "id": f"live-identity.{relation.id}",
            "name": relation.id,
            "status": "fail",
            "errors": active_errors,
            "sensitivity": "metadata_only",
        }

    source_identity = parse_identity(source_text)
    target_identity = parse_identity(target_text)
    reasons: list[str] = []
    if source_identity is None:
        reasons.append("source_relation_missing_or_not_base_table")
    if target_identity is None:
        reasons.append("target_relation_missing_or_not_base_table")
    if source_identity is not None and source_identity.get("primary_key") != expected_primary_key:
        reasons.append("source_primary_key_mismatch")
    if target_identity is not None and target_identity.get("primary_key") != expected_primary_key:
        reasons.append("target_primary_key_mismatch")
    return {
        "id": f"live-identity.{relation.id}",
        "name": relation.id,
        "status": "pass" if not reasons else "fail",
        "reasons": reasons,
        "expected_primary_key": expected_primary_key,
        "source_primary_key": source_identity.get("primary_key") if source_identity else None,
        "target_primary_key": target_identity.get("primary_key") if target_identity else None,
        "source_relation_kind": source_identity.get("relation_kind") if source_identity else None,
        "target_relation_kind": target_identity.get("relation_kind") if target_identity else None,
        "sensitivity": "metadata_only",
    }


def relay_preflight(contract: dict[str, Any], source_db_url: str, target_db_url: str) -> dict[str, Any]:
    tables = contract.get("tables", [])
    if not isinstance(tables, list) or not tables:
        raise RelayError("contract_tables_missing")
    checks: list[dict[str, Any]] = [reconcile.validate_schema(contract, source_db_url, target_db_url)]
    manifest_identity_checks = [manifest_identity_check(table) for table in tables]
    checks.extend(manifest_identity_checks)
    if all(check["status"] == "pass" for check in manifest_identity_checks):
        checks.extend(live_identity_check(table, source_db_url, target_db_url) for table in tables)
    failed = [check["id"] for check in checks if check["status"] == "fail"]
    return {
        "status": "fail" if failed else "pass",
        "check_count": len(checks),
        "failed_required_checks": failed,
        "checks": checks,
    }


def apply_sync_once(contract: dict[str, Any], source_db_url: str, target_db_url: str, *, batch_size: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nutsnews-supabase-sync-relay-") as tmpdir:
        workdir = Path(tmpdir)
        table_by_name = {str(table["name"]): table for table in contract.get("tables", [])}
        ordered_names = [str(name) for name in contract.get("apply_order", [])]
        if set(ordered_names) != set(table_by_name):
            raise RelayError("apply_order_mismatch")
        table_results = [
            reconcile.apply_table_backfill(source_db_url, target_db_url, table_by_name[name], batch_size=batch_size, workdir=workdir)
            for name in ordered_names
        ]
        sequence_results = [
            reconcile.apply_sequence_safety(source_db_url, target_db_url, sequence)
            for sequence in contract.get("sequences", [])
        ]
    return {
        "status": "applied",
        "table_count": len(table_results),
        "sequence_count": len(sequence_results),
        "tables": table_results,
        "sequences": sequence_results,
        "supported_change_types": ["insert", "update", "delete", "sequence-readiness"],
        "safe_metadata_only": True,
    }


def skipped_preflight(contract: dict[str, Any], reason: str) -> dict[str, Any]:
    checks = [{"id": "schema-fingerprint", "status": "skipped_with_reason", "reason": reason}]
    checks.extend(
        {"id": f"manifest-identity.{table.get('name')}", "status": "skipped_with_reason", "reason": reason}
        for table in contract.get("tables", [])
    )
    checks.extend(
        {"id": f"live-identity.{table.get('name')}", "status": "skipped_with_reason", "reason": reason}
        for table in contract.get("tables", [])
    )
    return {
        "status": "skipped_with_reason",
        "check_count": len(checks),
        "failed_required_checks": [],
        "checks": checks,
    }


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    contract_path = Path(args.contract)
    contract = load_json(contract_path)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    target_db_url = os.environ.get(args.target_db_url_env, "").strip()
    credentials_present = bool(source_db_url and target_db_url)
    report: dict[str, Any] = {
        "status": "blocked",
        "mode": args.mode,
        "checked_at_utc": reconcile.utc_now(),
        "issue": "ramideltoro/nutsnews#499",
        "epic": "ramideltoro/nutsnews#223",
        "contract": safe_contract_path(contract_path),
        "contract_version": contract.get("version"),
        "source_label": "backend_postgres_primary",
        "target_label": "existing_production_supabase_standby",
        "source_db_url_env": args.source_db_url_env,
        "target_db_url_env": args.target_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "target_db_url_present": bool(target_db_url),
        "backend_postgresql_remains_primary": True,
        "backend_postgres_public_5432_allowed": False,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "app_worker_supabase_write_credentials_injected": False,
        "safe_metadata_only": True,
    }

    if args.offline or not credentials_present:
        reason = "offline mode" if args.offline else "missing_source_or_target_db_url"
        report["preflight"] = skipped_preflight(contract, reason)
        report["sync"] = {"status": "skipped_with_reason", "reason": reason, "safe_metadata_only": True}
        report["post_sync"] = reconcile.skipped_validation(contract, reason)
        report["status"] = "skipped_with_reason" if args.offline else "blocked"
        return report, 0 if args.offline or not args.enforce else 1

    preflight = relay_preflight(contract, source_db_url, target_db_url)
    report["preflight"] = preflight
    if args.mode == "dry-run":
        report["sync"] = {"status": "not_run", "reason": "dry-run", "safe_metadata_only": True}
        report["post_sync"] = reconcile.skipped_validation(contract, "dry-run")
        report["status"] = preflight["status"]
        return report, 0 if (not args.enforce or preflight["status"] == "pass") else 1

    if preflight["status"] != "pass":
        report["sync"] = {
            "status": "blocked",
            "reason": "preflight_failed",
            "safe_metadata_only": True,
        }
        report["post_sync"] = reconcile.skipped_validation(contract, "preflight_failed")
        report["status"] = "fail"
        return report, 1 if args.enforce else 0

    sync_result = apply_sync_once(contract, source_db_url, target_db_url, batch_size=args.batch_size)
    post_sync = reconcile.validate_standby(contract, source_db_url, target_db_url)
    report["sync"] = sync_result
    report["post_sync"] = post_sync
    report["status"] = post_sync["status"]
    return report, 0 if (not args.enforce or post_sync["status"] == "pass") else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--source-db-url-env", default=DEFAULT_SOURCE_ENV)
    parser.add_argument("--target-db-url-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument("--output", default="")
    parser.add_argument("--mode", choices=("dry-run", "sync-once"), default="dry-run")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 5000:
        raise RelayError("invalid_batch_size")
    return args


def main_args(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv)
        report, exit_code = build_report(args)
    except RelayError as exc:
        report = {
            "status": "fail",
            "checked_at_utc": reconcile.utc_now(),
            "error": str(exc),
            "safe_metadata_only": True,
        }
        exit_code = 1
    except Exception:
        report = {
            "status": "fail",
            "checked_at_utc": reconcile.utc_now(),
            "error": "unexpected_sync_relay_error",
            "safe_metadata_only": True,
        }
        exit_code = 1

    text = json.dumps(report, indent=2, sort_keys=True)
    output = args.output if args is not None else ""
    if output:
        output_path = Path(output)
        output_path.write_text(text + "\n", encoding="utf-8")
        output_path.chmod(0o644)
    print(text)
    return exit_code


def main() -> int:
    return main_args()


if __name__ == "__main__":
    raise SystemExit(main())
