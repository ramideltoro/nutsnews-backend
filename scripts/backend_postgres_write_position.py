#!/usr/bin/env python3
"""Capture a safe backend PostgreSQL write-position snapshot.

The snapshot uses required-table row counts and row-content hashes from the
standby relay contract. It never prints database URLs, credentials, SQL text,
PostgreSQL errors, or row data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts import backend_supabase_standby_reconcile as reconcile
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    import backend_supabase_standby_reconcile as reconcile  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "backend-supabase-sync-relay.json"
DEFAULT_DB_URL_ENV = "NUTSNEWS_BACKEND_PRIMARY_DB_URL"
ISSUE = "ramideltoro/nutsnews#526"
EPIC = "ramideltoro/nutsnews#521"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def safe_digest(value: Any) -> str:
    return "sha256:" + canonical_sha256(value)[:24]


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("contract_unavailable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("contract_malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("contract_malformed")
    return data


def source_tables(contract: dict[str, Any]) -> list[dict[str, Any]]:
    tables = contract.get("tables", [])
    if not isinstance(tables, list) or not tables:
        raise ValueError("contract_tables_missing")
    result = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            raise ValueError("contract_tables_malformed")
        primary_key = table.get("primary_key", [])
        if not isinstance(primary_key, list) or not primary_key or not all(isinstance(item, str) for item in primary_key):
            raise ValueError("contract_primary_key_malformed")
        result.append({"name": table["name"], "primary_key": primary_key})
    return result


def parse_count(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def table_position(db_url: str, table: dict[str, Any]) -> dict[str, Any]:
    relation = reconcile.parse_relation(str(table["name"]))
    primary_key = [str(column) for column in table["primary_key"]]
    count_text, count_error = reconcile.query_value(db_url, reconcile.count_sql(relation))
    columns_text, columns_error = reconcile.query_value(db_url, reconcile.relation_column_metadata_sql(relation))
    errors = {
        "count_error": count_error,
        "columns_error": columns_error,
    }
    active_errors = {key: value for key, value in errors.items() if value}
    if active_errors:
        return {
            "id": f"table.{relation.id}",
            "name": relation.id,
            "status": "fail",
            "errors": active_errors,
            "sensitivity": "aggregate_and_hash_only",
        }

    assert columns_text is not None
    try:
        columns = reconcile.usable_columns(columns_text)
    except reconcile.ReconcileError:
        return {
            "id": f"table.{relation.id}",
            "name": relation.id,
            "status": "fail",
            "errors": {"columns_error": "invalid_column_metadata"},
            "sensitivity": "aggregate_and_hash_only",
        }

    checksum_query = reconcile.row_checksum_sql(relation, columns, primary_key)
    checksum_text, checksum_error = reconcile.query_value(db_url, checksum_query)
    if checksum_error:
        return {
            "id": f"table.{relation.id}",
            "name": relation.id,
            "status": "fail",
            "errors": {"checksum_error": checksum_error},
            "sensitivity": "aggregate_and_hash_only",
        }

    return {
        "id": f"table.{relation.id}",
        "name": relation.id,
        "status": "pass",
        "row_count": parse_count(count_text),
        "row_checksum_sha256": hashlib.sha256(str(checksum_text or "").encode("utf-8")).hexdigest(),
        "column_contract_sha256": hashlib.sha256(columns_text.encode("utf-8")).hexdigest(),
        "primary_key_fingerprint": safe_digest(primary_key),
        "sensitivity": "aggregate_and_hash_only",
    }


def capture(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract))
    db_url = os.environ.get(args.db_url_env, "").strip()
    if not db_url:
        raise ValueError("source_db_url_missing")
    if "\n" in db_url or "\r" in db_url:
        raise ValueError("source_db_url_multiline")
    tables = source_tables(contract)
    positions = [table_position(db_url, table) for table in tables]
    failed = [item for item in positions if item.get("status") != "pass"]
    stable_payload = {
        "source_label": contract.get("source", {}).get("label"),
        "tables": [
            {
                "name": item.get("name"),
                "row_count": item.get("row_count"),
                "row_checksum_sha256": item.get("row_checksum_sha256"),
                "column_contract_sha256": item.get("column_contract_sha256"),
            }
            for item in positions
            if item.get("status") == "pass"
        ],
    }
    return {
        "status": "fail" if failed else "pass",
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "source_label": contract.get("source", {}).get("label"),
        "target_label": contract.get("target", {}).get("label"),
        "manifest_fingerprint": contract.get("source_manifest", {}).get("schema_fingerprint"),
        "relay_contract_fingerprint": "sha256:" + canonical_sha256(contract)[:24],
        "db_url_env": args.db_url_env,
        "required_table_count": len(tables),
        "passed_table_count": len(positions) - len(failed),
        "failed_table_count": len(failed),
        "write_position_fingerprint": safe_digest(stable_payload),
        "tables": positions,
        "safe_metadata_only": True,
    }


def fail_result(args: argparse.Namespace, blocker: str) -> dict[str, Any]:
    return {
        "status": "fail",
        "issue": ISSUE,
        "epic": EPIC,
        "checked_at_utc": utc_now(),
        "db_url_env": args.db_url_env,
        "blockers": [blocker],
        "safe_metadata_only": True,
    }


def write_output(report: dict[str, Any], output: str) -> None:
    text = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    print(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--db-url-env", default=DEFAULT_DB_URL_ENV)
    parser.add_argument("--output", default="")
    parser.add_argument("--enforce", action="store_true")
    return parser.parse_args(argv)


def main_args(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = capture(args)
    except (ValueError, reconcile.ReconcileError) as exc:
        report = fail_result(args, str(exc))
    write_output(report, args.output)
    return 1 if args.enforce and report.get("status") != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main_args())
