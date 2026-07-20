#!/usr/bin/env python3
"""Run service-aware NutsNews backend backup actions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import re
from pathlib import Path
from typing import Any


DEFAULT_MATRIX_PATH = Path("/etc/nutsnews-backup/service-matrix.json")
DEFAULT_STATE_DIR = Path("/var/lib/nutsnews/backups")
STATUS_FILES = {
    "backup": "last-backup.json",
    "verification": "last-verification.json",
    "restore_drill": "last-restore-verification.json",
}
RESTORE_DRILL_CANDIDATES = (
    "/etc/hostname",
    "/etc/caddy/Caddyfile",
    "/var/www/nutsnews-ops-dashboard/status.json",
    "/var/lib/nutsnews/backups/last-backup.json",
)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "verify", "restore-drill", "status"))
    parser.add_argument("--matrix", type=Path, default=Path(os.environ.get("NUTSNEWS_BACKUP_MATRIX", DEFAULT_MATRIX_PATH)))
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("NUTSNEWS_BACKUP_STATE_DIR", DEFAULT_STATE_DIR)))
    parser.add_argument("--read-data-subset", default=os.environ.get("NUTSNEWS_BACKUP_CHECK_READ_DATA_SUBSET", "1%"))
    parser.add_argument("--quota-warn-bytes", type=int, default=int(os.environ.get("NUTSNEWS_BACKUP_QUOTA_WARN_BYTES", "0") or "0"))
    parser.add_argument("--stale-after-hours", type=int, default=int(os.environ.get("NUTSNEWS_BACKUP_STALE_AFTER_HOURS", "30") or "30"))
    parser.add_argument("--init-if-missing", default=os.environ.get("NUTSNEWS_BACKUP_RESTIC_INIT_IF_MISSING", "true"))
    return parser.parse_args(argv)


def load_matrix(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(path)


def redact_restic_text(text: str) -> str:
    redacted = re.sub(r"s3:[^\s]+", "s3:[REDACTED]", text)
    redacted = re.sub(r"https?://[^\s]+", "URL_REDACTED", redacted)
    redacted = re.sub(r"(?i)(access[_ -]?key|secret|password)[^\s]*", "REDACTED", redacted)
    return redacted


def repository_normalization_metadata() -> dict[str, str]:
    provider = (
        os.environ.get("NUTSNEWS_BACKUP_RESTIC_PROVIDER")
        or os.environ.get("NUTSNEWS_BACKEND_RESTIC_PROVIDER")
        or ""
    ).strip().lower()
    repository = os.environ.get("RESTIC_REPOSITORY", "").strip()
    if provider == "s3" and repository.startswith(("https://", "http://")):
        return {"status": "applied", "provider": "s3", "rule": "prefix_s3_for_http_repository"}
    return {"status": "not_needed", "provider": provider or "unspecified"}


def restic_env() -> dict[str, str]:
    env = os.environ.copy()
    metadata = repository_normalization_metadata()
    repository = env.get("RESTIC_REPOSITORY", "").strip()
    if metadata["status"] == "applied":
        env["RESTIC_REPOSITORY"] = f"s3:{repository}"
    return env


def run_restic(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["restic", *args],
        check=False,
        env=restic_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def require_restic_env() -> list[str]:
    required = ["RESTIC_REPOSITORY", "RESTIC_PASSWORD"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    return missing


def existing_backup_paths(matrix: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for service in matrix.get("services", []):
        method = service.get("backup_method", "")
        if "restic_paths" not in method:
            continue
        for raw_path in service.get("data_sources", []):
            path = Path(raw_path)
            if path.exists():
                paths.append(str(path))
    if "/etc/hostname" not in paths and Path("/etc/hostname").exists():
        paths.append("/etc/hostname")
    return sorted(set(paths))


def parse_restic_json_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def latest_snapshot_id() -> str | None:
    result = run_restic(["snapshots", "--json", "--last", "1"], timeout=600)
    if result.returncode != 0:
        return None
    try:
        snapshots = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not snapshots:
        return None
    return snapshots[-1].get("short_id") or snapshots[-1].get("id")


def init_repository_if_needed(enabled: bool) -> dict[str, Any] | None:
    result = run_restic(["snapshots", "--json"], timeout=600)
    if result.returncode == 0:
        return None
    if not enabled:
        return {"status": "error", "detail": "restic repository is not readable and init is disabled"}
    init = run_restic(["init"], timeout=600)
    return {
        "status": "initialized" if init.returncode == 0 else "error",
        "returncode": init.returncode,
        "stderr_tail": redact_restic_text(init.stderr[-1000:]),
        "repository_normalization": repository_normalization_metadata(),
    }


def quota_status(snapshot_id: str | None, warn_bytes: int) -> dict[str, Any]:
    if not warn_bytes:
        return {"status": "not_configured", "warn_bytes": 0}
    if not snapshot_id:
        return {"status": "unknown", "warn_bytes": warn_bytes}
    result = run_restic(["stats", "latest", "--mode", "raw-data", "--json"], timeout=1800)
    if result.returncode != 0:
        return {"status": "unknown", "warn_bytes": warn_bytes}
    try:
        stats = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "warn_bytes": warn_bytes}
    total_size = int(stats.get("total_size", 0) or 0)
    return {
        "status": "warning" if total_size >= warn_bytes else "healthy",
        "warn_bytes": warn_bytes,
        "observed_bytes": total_size,
    }


def alert_list(*, backup_status: str, stale: bool, verified: bool, quota: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if backup_status != "healthy":
        alerts.append({"kind": "backup_failure", "status": "critical"})
    if stale:
        alerts.append({"kind": "stale_backup", "status": "warning"})
    if not verified:
        alerts.append({"kind": "unverified_latest_snapshot", "status": "warning"})
    if quota.get("status") in {"warning", "critical", "unknown", "not_configured"}:
        alerts.append({"kind": "storage_quota_warning", "status": str(quota.get("status"))})
    return alerts


def snapshot_ids_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left = left.strip()
    right = right.strip()
    if len(left) < 8 or len(right) < 8:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def latest_backup_is_verified(state_dir: Path, snapshot_id: str | None) -> bool:
    if not snapshot_id:
        return False
    path = state_dir / STATUS_FILES["verification"]
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return data.get("status") == "healthy" and snapshot_ids_match(str(data.get("snapshot_id") or ""), snapshot_id)


def latest_backup_snapshot_id_from_state(state_dir: Path) -> str | None:
    path = state_dir / STATUS_FILES["backup"]
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("status") != "healthy" or data.get("freshness_status") != "healthy":
        return None
    snapshot_id = str(data.get("snapshot_id") or "").strip()
    return snapshot_id or None


def mark_backup_verified(state_dir: Path, snapshot_id: str | None, verified_at: str) -> None:
    if not snapshot_id:
        return
    path = state_dir / STATUS_FILES["backup"]
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not snapshot_ids_match(str(data.get("snapshot_id") or ""), snapshot_id):
        return
    alerts = [
        alert
        for alert in data.get("alerts", [])
        if alert.get("kind") != "unverified_latest_snapshot"
    ]
    data["alerts"] = alerts
    data["latest_snapshot_verified_at_utc"] = verified_at
    write_json(path, data)


def action_backup(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    missing = require_restic_env()
    matrix = load_matrix(args.matrix)
    started_at = utc_now()
    paths = existing_backup_paths(matrix)
    if missing:
        status = {
            "schema_version": 1,
            "action": "backup",
            "status": "critical",
            "freshness_status": "critical",
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "snapshot_id": None,
            "included_paths": paths,
            "missing_secret_names": missing,
            "repository_normalization": repository_normalization_metadata(),
            "alerts": [{"kind": "backup_failure", "status": "critical"}],
        }
        write_json(args.state_dir / STATUS_FILES["backup"], status)
        return status

    init = init_repository_if_needed(args.init_if_missing.lower() in {"1", "true", "yes"})
    if init and init["status"] == "error":
        status = {
            "schema_version": 1,
            "action": "backup",
            "status": "critical",
            "freshness_status": "critical",
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "snapshot_id": None,
            "included_paths": paths,
            "repository_initialization": init,
            "alerts": [{"kind": "backup_failure", "status": "critical"}],
        }
        write_json(args.state_dir / STATUS_FILES["backup"], status)
        return status

    result = run_restic(["backup", "--json", "--tag", "nutsnews-backend", "--tag", "service-aware", *paths])
    rows = parse_restic_json_lines(result.stdout)
    summary = next((row for row in reversed(rows) if row.get("message_type") == "summary"), {})
    snapshot_id = summary.get("snapshot_id") or latest_snapshot_id()
    healthy = result.returncode == 0 and bool(snapshot_id)
    quota = quota_status(snapshot_id, args.quota_warn_bytes)
    verified = latest_backup_is_verified(args.state_dir, snapshot_id)
    status = {
        "schema_version": 1,
        "action": "backup",
        "status": "healthy" if healthy else "critical",
        "freshness_status": "healthy" if healthy else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "snapshot_id": snapshot_id,
        "included_paths": paths,
        "service_matrix_version": matrix.get("version"),
        "repository_normalization": repository_normalization_metadata(),
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "total_bytes_processed": summary.get("total_bytes_processed"),
        "repository_initialization": init,
        "quota": quota,
        "alerts": alert_list(backup_status="healthy" if healthy else "critical", stale=False, verified=verified, quota=quota),
        "stderr_tail": redact_restic_text(result.stderr[-1000:]) if result.returncode != 0 else "",
    }
    write_json(args.state_dir / STATUS_FILES["backup"], status)
    return status


def action_verify(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started_at = utc_now()
    snapshot_id = latest_snapshot_id()
    snapshot_source = "restic_latest"
    if not snapshot_id:
        snapshot_id = latest_backup_snapshot_id_from_state(args.state_dir)
        snapshot_source = "backup_status" if snapshot_id else "unavailable"
    if not snapshot_id:
        status = {
            "schema_version": 1,
            "action": "verify",
            "status": "critical",
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "snapshot_id": None,
            "snapshot_source": snapshot_source,
            "alerts": [{"kind": "unverified_latest_snapshot", "status": "critical"}],
        }
        write_json(args.state_dir / STATUS_FILES["verification"], status)
        return status
    check_args = ["check"]
    if args.read_data_subset:
        check_args.extend(["--read-data-subset", args.read_data_subset])
    result = run_restic(check_args, timeout=7200)
    healthy = result.returncode == 0
    finished_at = utc_now()
    status = {
        "schema_version": 1,
        "action": "verify",
        "status": "healthy" if healthy else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "snapshot_id": snapshot_id,
        "snapshot_source": snapshot_source,
        "read_data_subset": args.read_data_subset,
        "alerts": [] if healthy else [{"kind": "unverified_latest_snapshot", "status": "critical"}],
        "repository_normalization": repository_normalization_metadata(),
        "stderr_tail": redact_restic_text(result.stderr[-1000:]) if result.returncode != 0 else "",
    }
    write_json(args.state_dir / STATUS_FILES["verification"], status)
    if healthy:
        mark_backup_verified(args.state_dir, snapshot_id, finished_at)
    return status


def action_restore_drill(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started_at = utc_now()
    snapshot_id = latest_snapshot_id()
    selected = [path for path in RESTORE_DRILL_CANDIDATES if Path(path).exists()]
    if not selected and Path("/etc/hostname").exists():
        selected = ["/etc/hostname"]
    if not snapshot_id:
        status = {
            "schema_version": 1,
            "action": "restore-drill",
            "status": "critical",
            "started_at_utc": started_at,
            "finished_at_utc": utc_now(),
            "snapshot_id": None,
            "selected_paths": selected,
            "alerts": [{"kind": "unverified_latest_snapshot", "status": "critical"}],
        }
        write_json(args.state_dir / STATUS_FILES["restore_drill"], status)
        return status
    with tempfile.TemporaryDirectory(prefix="nutsnews-restore-drill-") as tmpdir:
        restore_args = ["restore", "latest", "--target", tmpdir]
        for path in selected:
            restore_args.extend(["--include", path])
        result = run_restic(restore_args, timeout=3600)
        restored = [path for path in selected if (Path(tmpdir) / path.removeprefix("/")).exists()]
    healthy = result.returncode == 0 and sorted(restored) == sorted(selected)
    status = {
        "schema_version": 1,
        "action": "restore-drill",
        "status": "healthy" if healthy else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "snapshot_id": snapshot_id,
        "selected_paths": selected,
        "restored_paths": restored,
        "alerts": [] if healthy else [{"kind": "unverified_latest_snapshot", "status": "critical"}],
        "repository_normalization": repository_normalization_metadata(),
        "stderr_tail": redact_restic_text(result.stderr[-1000:]) if result.returncode != 0 else "",
    }
    write_json(args.state_dir / STATUS_FILES["restore_drill"], status)
    return status


def read_status_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_configured"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "unknown"}


def action_status(args: argparse.Namespace) -> dict[str, Any]:
    backup = read_status_file(args.state_dir / STATUS_FILES["backup"])
    verification = read_status_file(args.state_dir / STATUS_FILES["verification"])
    restore_drill = read_status_file(args.state_dir / STATUS_FILES["restore_drill"])
    return {
        "schema_version": 1,
        "action": "status",
        "generated_at_utc": utc_now(),
        "backup": backup,
        "verification": verification,
        "restore_drill": restore_drill,
        "secret_redaction": "status files contain secret names only; restic credentials are never written",
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    actions = {
        "backup": action_backup,
        "verify": action_verify,
        "restore-drill": action_restore_drill,
        "status": action_status,
    }
    result = actions[args.action](args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"healthy", "not_configured"} or args.action == "status" else 1


if __name__ == "__main__":
    raise SystemExit(main())
