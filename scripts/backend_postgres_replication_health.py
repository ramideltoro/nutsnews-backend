#!/usr/bin/env python3
"""Collect safe backend PostgreSQL logical replication health metadata."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUBSCRIPTION_QUERY = """
select coalesce(jsonb_agg(jsonb_build_object(
  'subscription', subname,
  'pid_present', pid is not null,
  'received_lsn_present', received_lsn is not null,
  'latest_end_lsn_present', latest_end_lsn is not null,
  'last_msg_receipt_time', last_msg_receipt_time,
  'latest_end_time', latest_end_time,
  'lag_seconds', case when latest_end_time is null then null else extract(epoch from now() - latest_end_time)::bigint end
) order by subname), '[]'::jsonb)::text
from pg_stat_subscription
"""

SLOT_QUERY = """
select coalesce(jsonb_agg(jsonb_build_object(
  'slot_name', slot_name,
  'active', active,
  'restart_lsn_present', restart_lsn is not null,
  'confirmed_flush_lsn_present', confirmed_flush_lsn is not null,
  'wal_status', null,
  'safe_wal_size_present', false
) order by slot_name), '[]'::jsonb)::text
from pg_replication_slots
where slot_name like 'nutsnews_backend_migration_%'
"""


def run_psql(db_url: str, query: str) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        return None, "psql_not_installed"
    except subprocess.TimeoutExpired:
        return None, "query_timeout"
    except subprocess.CalledProcessError:
        return None, "query_failed"
    return proc.stdout.strip(), None


def write_textfile(path: str, report: dict) -> None:
    replication = report["replication"]
    lines = [
        "# HELP nutsnews_backend_postgres_replication_subscription_count Logical replication subscription count.",
        "# TYPE nutsnews_backend_postgres_replication_subscription_count gauge",
        f"nutsnews_backend_postgres_replication_subscription_count {replication['subscription_count']}",
        "# HELP nutsnews_backend_postgres_replication_blocker_count Replication health blocker count.",
        "# TYPE nutsnews_backend_postgres_replication_blocker_count gauge",
        f"nutsnews_backend_postgres_replication_blocker_count {len(report['blockers'])}",
    ]
    if replication.get("max_lag_seconds") is not None:
        lines.extend(
            [
                "# HELP nutsnews_backend_postgres_replication_max_lag_seconds Maximum observed logical replication lag.",
                "# TYPE nutsnews_backend_postgres_replication_max_lag_seconds gauge",
                f"nutsnews_backend_postgres_replication_max_lag_seconds {replication['max_lag_seconds']}",
            ]
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(path: Path, report: dict[str, Any]) -> None:
    status = read_status(path)
    status.setdefault("status", "warning")
    status.setdefault("source_of_truth", "Supabase remains the only production writer until a protected cutover is separately approved.")
    status["replication"] = report["replication"]
    status["replication"]["checked_at_utc"] = report["checked_at_utc"]
    status["replication"]["status"] = report["status"]
    status["replication"]["blockers"] = report["blockers"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_TARGET_DB_URL")
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_SOURCE_DB_URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--textfile-output", default="")
    parser.add_argument("--status-output", default="")
    parser.add_argument("--max-lag-seconds", type=int, default=300)
    parser.add_argument("--validation-stale-seconds", type=int, default=900)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--simulate-broken", action="store_true")
    parser.add_argument(
        "--expected-active",
        action="store_true",
        help="Treat replication readiness failures as production-critical after protected cutover.",
    )
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    target_db_url = os.environ.get(args.target_db_url_env, "").strip()
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    blockers: list[str] = []
    subscriptions: list[dict] = []
    slots: list[dict] = []
    active_subscriptions: list[dict] = []

    if args.simulate_broken:
        status = "fail"
        lag_status = "lagging"
        slot_status = "inactive"
        subscriptions = [
            {
                "subscription": "nutsnews_backend_migration_sub",
                "pid_present": False,
                "received_lsn_present": True,
                "latest_end_lsn_present": True,
                "last_msg_receipt_time": None,
                "latest_end_time": None,
                "lag_seconds": args.max_lag_seconds + 1,
            }
        ]
        slots = [
            {
                "slot_name": "nutsnews_backend_migration_slot",
                "active": False,
                "restart_lsn_present": True,
                "confirmed_flush_lsn_present": True,
                "wal_status": "reserved",
                "safe_wal_size_present": True,
            }
        ]
        blockers.extend(
            [
                "simulated_broken_replication",
                "subscription_inactive",
                "replication_lag_exceeds_threshold",
                "source_replication_slot_inactive",
            ]
        )
    elif args.offline:
        status = "skipped_with_reason"
        lag_status = "not_configured"
        slot_status = "not_configured"
    elif not target_db_url:
        status = "blocked"
        lag_status = "not_configured"
        slot_status = "not_configured"
        blockers.append("missing_target_db_url")
    else:
        raw, error = run_psql(target_db_url, SUBSCRIPTION_QUERY)
        if error:
            status = "fail"
            lag_status = "unknown"
            blockers.append(error)
        else:
            subscriptions = json.loads(raw or "[]")
            active_subscriptions = [item for item in subscriptions if item.get("pid_present")]
            max_lag = max((int(item["lag_seconds"]) for item in subscriptions if item.get("lag_seconds") is not None), default=None)
            if not subscriptions:
                status = "not_configured"
                lag_status = "not_configured"
            elif not active_subscriptions:
                status = "fail"
                lag_status = "inactive"
                blockers.append("subscription_inactive")
            elif max_lag is not None and max_lag > args.max_lag_seconds:
                status = "fail"
                lag_status = "lagging"
                blockers.append("replication_lag_exceeds_threshold")
            else:
                status = "healthy"
                lag_status = "healthy"

        if source_db_url:
            raw_slots, slot_error = run_psql(source_db_url, SLOT_QUERY)
            if slot_error:
                slot_status = "unknown"
                blockers.append("source_slot_check_failed")
            else:
                slots = json.loads(raw_slots or "[]")
                inactive = [item for item in slots if not item.get("active")]
                slots_with_lsn = [
                    item
                    for item in slots
                    if item.get("restart_lsn_present") and item.get("confirmed_flush_lsn_present")
                ]
                if not slots:
                    slot_status = "not_configured"
                elif active_subscriptions and slots_with_lsn:
                    slot_status = "healthy" if not inactive else "healthy_idle"
                else:
                    slot_status = "inactive" if inactive else "healthy"
                if subscriptions and not slots:
                    blockers.append("source_replication_slot_missing")
                if inactive and not active_subscriptions:
                    blockers.append("source_replication_slot_inactive")
        else:
            slot_status = "skipped_missing_source_db_url"
            if subscriptions:
                blockers.append("source_slot_check_skipped_missing_source_db_url")

    if blockers and status == "healthy":
        status = "fail"

    if args.expected_active and status == "blocked":
        status = "fail"
    elif not args.expected_active and status == "fail":
        status = "blocked"

    max_lag_seconds = max((int(item["lag_seconds"]) for item in subscriptions if item.get("lag_seconds") is not None), default=None)
    report = {
        "status": status,
        "expected_active": args.expected_active,
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "target_db_url_env": args.target_db_url_env,
        "target_db_url_present": bool(target_db_url),
        "source_db_url_env": args.source_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "replication": {
            "mode": "logical_replication",
            "expected_active": args.expected_active,
            "lag_status": lag_status,
            "validation_status": "not_configured" if args.offline else "current",
            "validation_stale_threshold_seconds": args.validation_stale_seconds,
            "max_lag_seconds": max_lag_seconds,
            "max_lag_threshold_seconds": args.max_lag_seconds,
            "subscription_count": len(subscriptions),
            "subscriptions": subscriptions,
            "slot_status": slot_status,
            "slot_count": len(slots),
            "slots": slots,
            "split_brain_guard": "no multi-writer topology",
            "recovery_hint": "resync from fresh dump if subscription repair cannot prove parity",
        },
        "blockers": blockers,
        "safe_metadata_only": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    if args.textfile_output:
        write_textfile(args.textfile_output, report)
    if args.status_output:
        write_status(Path(args.status_output), report)
    print(text)
    if args.enforce and status not in {"healthy", "not_configured"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_args())
