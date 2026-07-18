#!/usr/bin/env python3
"""Write low-cardinality NutsNews backend metrics for Alloy textfile scraping."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("/var/lib/nutsnews/metrics/nutsnews.prom")
BACKUP_STATE_DIR = Path("/var/lib/nutsnews/backups")
POSTGRES_STATE_DIR = Path("/var/lib/nutsnews/postgres")
POSTGRES_REPLICATION_HEALTH_PATH = POSTGRES_STATE_DIR / "replication-health.json"
SERVICES = (
    "ssh",
    "ufw",
    "fail2ban",
    "caddy",
    "docker",
    "postgresql",
    "alloy",
    "sysstat",
    "nutsnews-backup.timer",
    "nutsnews-backup-verify.timer",
    "nutsnews-restore-drill.timer",
    "nutsnews-ops-dashboard-collect.timer",
)
STATUS_VALUE = {
    "healthy": 1,
    "warning": 0,
    "critical": 0,
    "unknown": 0,
    "not_configured": 0,
}


def run(command: list[str], timeout: int = 8) -> str:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def shell(command: str, timeout: int = 8) -> str:
    return run(["sh", "-lc", command], timeout=timeout)


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if labels:
        rendered = ",".join(f'{key}="{label(raw)}"' for key, raw in sorted(labels.items()))
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_configured"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
    return data if isinstance(data, dict) else {"status": "unknown"}


def service_active(service: str) -> int:
    return 1 if shell(f"systemctl is-active {service} 2>/dev/null || true") == "active" else 0


def backup_stage_status(stage: str, data: dict[str, Any]) -> tuple[str, int]:
    status = str(data.get("freshness_status") or data.get("status") or "not_configured")
    return status, STATUS_VALUE.get(status, 0)


def postgres_status_value(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "not_configured")
    if status in STATUS_VALUE:
        return status
    return "unknown"


def collect() -> list[str]:
    now = int(datetime.now(UTC).timestamp())
    lines = [
        "# HELP nutsnews_backend_metric_scrape_timestamp_seconds Unix timestamp of this textfile metric snapshot.",
        "# TYPE nutsnews_backend_metric_scrape_timestamp_seconds gauge",
        metric("nutsnews_backend_metric_scrape_timestamp_seconds", now),
        "# HELP nutsnews_backend_service_active Whether a backend service or timer is active.",
        "# TYPE nutsnews_backend_service_active gauge",
    ]

    for service in SERVICES:
        lines.append(metric("nutsnews_backend_service_active", service_active(service), {"unit": service}))

    failed_units = shell("systemctl --failed --no-legend --no-pager | wc -l")
    upgradable = shell("apt list --upgradable 2>/dev/null | tail -n +2 | wc -l")
    reboot_required = 1 if Path("/var/run/reboot-required").exists() else 0
    endpoint = shell(
        "curl -fsS --connect-timeout 5 --resolve backend.nutsnews.com:443:127.0.0.1 "
        "https://backend.nutsnews.com/healthz 2>/dev/null || true"
    )

    lines.extend(
        [
            "# HELP nutsnews_backend_failed_systemd_units Failed systemd units on the backend host.",
            "# TYPE nutsnews_backend_failed_systemd_units gauge",
            metric("nutsnews_backend_failed_systemd_units", int(failed_units or "0")),
            "# HELP nutsnews_backend_upgradable_packages APT packages visible as upgradable.",
            "# TYPE nutsnews_backend_upgradable_packages gauge",
            metric("nutsnews_backend_upgradable_packages", int(upgradable or "0")),
            "# HELP nutsnews_backend_reboot_required Whether /var/run/reboot-required exists.",
            "# TYPE nutsnews_backend_reboot_required gauge",
            metric("nutsnews_backend_reboot_required", reboot_required),
            "# HELP nutsnews_backend_public_endpoint_healthy Whether the local HTTPS health endpoint returns ok.",
            "# TYPE nutsnews_backend_public_endpoint_healthy gauge",
            metric("nutsnews_backend_public_endpoint_healthy", 1 if endpoint == "ok" else 0),
        ]
    )

    backup = read_json(BACKUP_STATE_DIR / "last-backup.json")
    verification = read_json(BACKUP_STATE_DIR / "last-verification.json")
    restore_drill = read_json(BACKUP_STATE_DIR / "last-restore-verification.json")
    lines.extend(
        [
            "# HELP nutsnews_backend_backup_stage_healthy Whether a backup stage is healthy.",
            "# TYPE nutsnews_backend_backup_stage_healthy gauge",
            "# HELP nutsnews_backend_backup_stage_status Status value for a backup stage.",
            "# TYPE nutsnews_backend_backup_stage_status gauge",
        ]
    )
    for stage, data in {
        "backup": backup,
        "verification": verification,
        "restore_drill": restore_drill,
    }.items():
        status, healthy = backup_stage_status(stage, data)
        lines.append(metric("nutsnews_backend_backup_stage_healthy", healthy, {"stage": stage}))
        lines.append(metric("nutsnews_backend_backup_stage_status", 1, {"stage": stage, "status": status}))

    quota = backup.get("quota", {}) if isinstance(backup, dict) else {}
    quota_status = str(quota.get("status") or "not_configured") if isinstance(quota, dict) else "not_configured"
    verified = 1 if backup.get("latest_snapshot_verified_at_utc") else 0
    lines.extend(
        [
            "# HELP nutsnews_backend_backup_latest_snapshot_verified Whether the latest backup snapshot has a successful verification record.",
            "# TYPE nutsnews_backend_backup_latest_snapshot_verified gauge",
            metric("nutsnews_backend_backup_latest_snapshot_verified", verified),
            "# HELP nutsnews_backend_backup_storage_quota_configured Whether backup storage quota guardrail is configured.",
            "# TYPE nutsnews_backend_backup_storage_quota_configured gauge",
            metric("nutsnews_backend_backup_storage_quota_configured", 0 if quota_status == "not_configured" else 1),
        ]
    )

    postgres = read_json(POSTGRES_STATE_DIR / "status.json")
    replication_health = read_json(POSTGRES_REPLICATION_HEALTH_PATH)
    if isinstance(replication_health.get("replication"), dict):
        postgres["replication"] = replication_health["replication"]
    restore_drill_status = postgres_status_value(postgres.get("last_restore_drill", {}) if isinstance(postgres.get("last_restore_drill"), dict) else {})
    postgres_status = postgres_status_value(postgres)
    replication = postgres.get("replication", {}) if isinstance(postgres, dict) else {}
    lag_status = str(replication.get("lag_status") or "not_configured") if isinstance(replication, dict) else "not_configured"
    max_lag_seconds = replication.get("max_lag_seconds") if isinstance(replication, dict) else None
    blocker_count = len(replication.get("blockers", [])) if isinstance(replication.get("blockers"), list) else 0
    failover_ready = 1 if postgres_status == "healthy" and restore_drill_status == "healthy" else 0
    lines.extend(
        [
            "# HELP nutsnews_backend_postgres_failover_ready Whether the private PostgreSQL failover target has a healthy restore drill.",
            "# TYPE nutsnews_backend_postgres_failover_ready gauge",
            metric("nutsnews_backend_postgres_failover_ready", failover_ready),
            "# HELP nutsnews_backend_postgres_restore_drill_healthy Whether the latest PostgreSQL restore drill is healthy.",
            "# TYPE nutsnews_backend_postgres_restore_drill_healthy gauge",
            metric("nutsnews_backend_postgres_restore_drill_healthy", STATUS_VALUE.get(restore_drill_status, 0), {"status": restore_drill_status}),
            "# HELP nutsnews_backend_postgres_replication_lag_configured Whether continuous replication lag is configured for the selected topology.",
            "# TYPE nutsnews_backend_postgres_replication_lag_configured gauge",
            metric("nutsnews_backend_postgres_replication_lag_configured", 0 if lag_status == "not_configured" else 1, {"status": lag_status}),
            "# HELP nutsnews_backend_postgres_replication_blockers Current replication health blocker count.",
            "# TYPE nutsnews_backend_postgres_replication_blockers gauge",
            metric("nutsnews_backend_postgres_replication_blockers", blocker_count),
        ]
    )
    if isinstance(max_lag_seconds, (int, float)):
        lines.extend(
            [
                "# HELP nutsnews_backend_postgres_replication_max_lag_seconds Maximum observed logical replication lag.",
                "# TYPE nutsnews_backend_postgres_replication_max_lag_seconds gauge",
                metric("nutsnews_backend_postgres_replication_max_lag_seconds", max_lag_seconds),
            ]
        )

    return lines


def write_atomic(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    write_atomic(args.output, collect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
