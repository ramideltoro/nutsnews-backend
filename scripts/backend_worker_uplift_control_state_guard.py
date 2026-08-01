#!/usr/bin/env python3
"""Read the authoritative cutover row and fail closed before generic mutation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_ENV_FILE = Path("/etc/nutsnews-worker-uplift/cutover.env")
EXPECTED_STATES = {
    "shadow": ("legacy_shards", True, True, False, "shadow_comparison"),
    "fenced": ("legacy_shards", False, False, False, "shadow_comparison"),
    "cutover_active": ("worker_uplift", False, True, True, "production"),
    "rollback_pending": ("legacy_shards", False, False, False, "shadow_comparison"),
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("cutover environment is malformed")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def read_control_row(path: Path = DEFAULT_ENV_FILE) -> dict[str, Any]:
    values = read_env_file(path)
    required = (
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_HOST",
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PORT",
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_NAME",
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_USER",
        "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PASSWORD",
    )
    if any(not values.get(key) for key in required):
        raise ValueError("cutover environment is incomplete")
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise RuntimeError("PostgreSQL client is unavailable") from exc
    with psycopg2.connect(
        host=values[required[0]],
        port=int(values[required[1]]),
        dbname=values[required[2]],
        user=values[required[3]],
        password=values[required[4]],
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
        cursor_factory=psycopg2.extras.RealDictCursor,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select state, active_ingestion_owner, legacy_dispatch_enabled,
                       uplift_scheduler_enabled,
                       uplift_production_writes_enabled, publication_write_mode,
                       candidate_sha256, watermark_sha256,
                       to_char(rollback_deadline at time zone 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS"Z"') as rollback_deadline_utc
                from worker_uplift_final.cutover_control
                where control_id = 'production'
                """
            )
            row = cursor.fetchone()
    if row is None:
        raise ValueError("production cutover control row is missing")
    return dict(row)


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "")
    observed = (
        row.get("active_ingestion_owner"),
        row.get("legacy_dispatch_enabled"),
        row.get("uplift_scheduler_enabled"),
        row.get("uplift_production_writes_enabled"),
        row.get("publication_write_mode"),
    )
    errors: list[str] = []
    if state not in EXPECTED_STATES:
        errors.append("unknown_control_state")
    elif observed != EXPECTED_STATES[state]:
        errors.append("control_state_tuple_mismatch")
    maintenance_safe = not errors and state == "shadow"
    return {
        "schema_version": 1,
        "status": "pass" if not errors else "error",
        "state": state or "unknown",
        "active_ingestion_owner": row.get("active_ingestion_owner"),
        "legacy_dispatch_enabled": row.get("legacy_dispatch_enabled"),
        "uplift_scheduler_enabled": row.get("uplift_scheduler_enabled"),
        "uplift_production_writes_enabled": row.get(
            "uplift_production_writes_enabled"
        ),
        "publication_write_mode": row.get("publication_write_mode"),
        "candidate_bound": bool(row.get("candidate_sha256")),
        "watermark_bound": bool(row.get("watermark_sha256")),
        "rollback_deadline_present": bool(row.get("rollback_deadline_utc")),
        "generic_mutation_safe": maintenance_safe,
        "errors": errors,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--require-maintenance-safe", action="store_true")
    parser.add_argument("--require-state", choices=tuple(EXPECTED_STATES))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = evaluate(read_control_row(args.db_env_file))
    except Exception:
        report = {
            "schema_version": 1,
            "status": "error",
            "state": "unknown",
            "generic_mutation_safe": False,
            "errors": ["control_state_unavailable_or_invalid"],
        }
    if args.require_state and report.get("state") != args.require_state:
        report["status"] = "block"
        report.setdefault("errors", []).append(
            f"required_control_state_{args.require_state}_absent"
        )
    if args.require_maintenance_safe and not report.get("generic_mutation_safe"):
        report["status"] = "block"
        report.setdefault("errors", []).append("generic_mutation_blocked_by_cutover_state")
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
