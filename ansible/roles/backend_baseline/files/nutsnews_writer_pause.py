#!/usr/bin/env python3
"""Pause and resume fixed NutsNews backend PostgreSQL writer classes.

This host-side tool is intentionally narrow. It accepts only pause/status/resume
actions, reads a source-controlled inventory, writes safe JSON state, and never
prints environment values, database URLs, SQL, row data, tokens, or passwords.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_INVENTORY = Path("/etc/nutsnews-writer-pause/inventory.json")
DEFAULT_STATE_DIR = Path("/var/lib/nutsnews/writer-pause")
DEFAULT_WORKER_API_ENV = Path("/etc/nutsnews-worker-db-api.env")
DEFAULT_WORKER_API_DROPIN_DIR = Path("/etc/systemd/system/nutsnews-worker-db-api.service.d")
DEFAULT_WORKER_API_DROPIN = DEFAULT_WORKER_API_DROPIN_DIR / "50-writer-pause.conf"
DEFAULT_RUNTIME_MANIFEST = Path("/etc/nutsnews-worker-uplift/services.json")
DEFAULT_RUNTIME_COMPOSE = Path("/opt/nutsnews-worker-uplift/compose.yml")
DEFAULT_RUNTIME_PROJECT = "nutsnews-worker-uplift"
STATE_FILE = "active-pause.json"
LAST_REPORT_FILE = "last-report.json"
LAST_RESUME_FILE = "last-resume.json"
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
SAFE_BOOL_VALUES = {"1": True, "true": True, "yes": True, "on": True, "0": False, "false": False, "no": False, "off": False}
WORKER_API_WRITE_FLAGS = (
    "NUTSNEWS_WORKER_DB_API_WRITES_ENABLED",
    "NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED",
)


class PauseError(RuntimeError):
    """Safe operational failure marker."""


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PauseError(f"json_file_missing:{path}") from exc
    except json.JSONDecodeError as exc:
        raise PauseError(f"json_file_malformed:{path}") from exc
    if not isinstance(data, dict):
        raise PauseError(f"json_file_malformed:{path}")
    return data


def write_json(path: Path, data: dict[str, Any], mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run_command(argv: list[str], *, timeout: int = 120) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"argv": safe_argv(argv), "returncode": 127, "stdout": "", "stderr": "command_not_found"}
    except subprocess.TimeoutExpired:
        return {"argv": safe_argv(argv), "returncode": 124, "stdout": "", "stderr": "command_timeout"}
    return {
        "argv": safe_argv(argv),
        "returncode": completed.returncode,
        "stdout": safe_output(completed.stdout),
        "stderr": safe_output(completed.stderr),
    }


def safe_argv(argv: list[str]) -> list[str]:
    if not argv:
        return []
    if argv[0].endswith("docker") or argv[0] == "docker":
        return argv[:4] + ["<args-redacted>"]
    return argv


def safe_output(value: str) -> str:
    # Keep operational reports short and avoid accidentally retaining noisy logs.
    return "\n".join(line[:240] for line in value.splitlines()[:20])


def systemctl(args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    return run_command(["systemctl", *args], timeout=timeout)


def command_value(result: dict[str, Any]) -> str:
    return str(result.get("stdout") or "").strip().splitlines()[0] if str(result.get("stdout") or "").strip() else ""


def service_property(unit: str, prop: str) -> str:
    result = systemctl(["show", unit, f"--property={prop}", "--value"])
    value = command_value(result)
    return value or "unknown"


def service_state(unit: str) -> dict[str, str]:
    active = command_value(systemctl(["is-active", unit])) or "unknown"
    enabled = command_value(systemctl(["is-enabled", unit])) or "unknown"
    load_state = service_property(unit, "LoadState")
    return {
        "unit": unit,
        "active": active,
        "enabled": enabled,
        "load_state": load_state,
    }


def parse_env_flags(path: Path) -> dict[str, bool | None]:
    flags: dict[str, bool | None] = {name: None for name in WORKER_API_WRITE_FLAGS}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return flags
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in flags:
            continue
        flags[key] = SAFE_BOOL_VALUES.get(value.strip().lower())
    return flags


def inventory_entries(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = inventory.get("writer_classes", [])
    if not isinstance(entries, list):
        raise PauseError("inventory_writer_classes_malformed")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise PauseError("inventory_writer_classes_malformed")
        by_id[entry["id"]] = entry
    return by_id


def inventory_fingerprint(inventory: dict[str, Any]) -> str:
    return "sha256:" + canonical_sha256(inventory)[:24]


def require_attempt_id(value: str) -> None:
    if not ATTEMPT_ID_RE.fullmatch(value):
        raise PauseError("invalid_failover_attempt_id")


def worker_api_status(args: argparse.Namespace) -> dict[str, Any]:
    flags = parse_env_flags(args.worker_api_env)
    service = service_state("nutsnews-worker-db-api.service")
    dropin_present = args.worker_api_dropin.exists()
    service_present = service["load_state"] == "loaded" or args.worker_api_env.exists()
    write_flags_disabled = all(flags.get(name) is False for name in WORKER_API_WRITE_FLAGS if flags.get(name) is not None)
    paused = (not service_present) or dropin_present or write_flags_disabled
    blockers: list[str] = []
    if service_present and not paused:
        blockers.append("backend_worker_database_api_not_paused")
    return {
        "id": "backend_worker_database_api",
        "class": "web_app_and_legacy_worker_write_api",
        "kind": "systemd_env_guard",
        "present": service_present,
        "unit": service["unit"],
        "active": service["active"],
        "enabled": service["enabled"],
        "load_state": service["load_state"],
        "dropin_present": dropin_present,
        "write_flags": {name: flags.get(name) for name in WORKER_API_WRITE_FLAGS},
        "paused": paused,
        "blockers": blockers,
        "safe_status_only": True,
    }


def install_worker_api_pause(args: argparse.Namespace) -> None:
    args.worker_api_dropin.parent.mkdir(parents=True, exist_ok=True)
    args.worker_api_dropin.write_text(
        "[Service]\n"
        "Environment=NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=false\n"
        "Environment=NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=false\n",
        encoding="utf-8",
    )
    os.chmod(args.worker_api_dropin, 0o644)
    systemctl(["daemon-reload"])
    state = service_state("nutsnews-worker-db-api.service")
    if state["load_state"] == "loaded" and state["active"] in {"active", "activating", "failed", "inactive"}:
        systemctl(["restart", "nutsnews-worker-db-api.service"], timeout=60)


def remove_worker_api_pause(args: argparse.Namespace, previous: dict[str, Any]) -> None:
    try:
        args.worker_api_dropin.unlink()
    except FileNotFoundError:
        pass
    systemctl(["daemon-reload"])
    before = previous.get("before", {}) if isinstance(previous, dict) else {}
    active_before = before.get("active")
    enabled_before = before.get("enabled")
    if active_before in {"active", "activating"} or enabled_before == "enabled":
        systemctl(["restart", "nutsnews-worker-db-api.service"], timeout=60)


def load_runtime_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "services": []}
    manifest = load_json(path)
    services = manifest.get("services", [])
    if not isinstance(services, list):
        raise PauseError("worker_runtime_manifest_services_malformed")
    clean_services: list[dict[str, Any]] = []
    for service in services:
        if not isinstance(service, dict):
            raise PauseError("worker_runtime_manifest_services_malformed")
        name = str(service.get("name") or "")
        if not SERVICE_NAME_RE.fullmatch(name):
            raise PauseError("worker_runtime_manifest_service_name_invalid")
        replicas = service.get("replicas", 0)
        if not isinstance(replicas, int) or replicas < 0 or replicas > 20:
            raise PauseError("worker_runtime_manifest_replicas_invalid")
        clean_services.append({"name": name, "replicas": replicas})
    return {"present": True, "services": clean_services}


def parse_compose_ps(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    stripped = stdout.strip()
    if not stripped:
        return counts
    rows: list[Any] = []
    try:
        decoded = json.loads(stripped)
        if isinstance(decoded, list):
            rows = decoded
        elif isinstance(decoded, dict):
            rows = [decoded]
    except json.JSONDecodeError:
        rows = []
        for line in stripped.splitlines():
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                rows.append(decoded)
    for row in rows:
        if not isinstance(row, dict):
            continue
        service = str(row.get("Service") or row.get("service") or row.get("Name") or "")
        state = str(row.get("State") or row.get("state") or row.get("Status") or "").lower()
        if not service:
            continue
        running = state.startswith("running") or state == "running" or "up" in state
        if running:
            counts[service] = counts.get(service, 0) + 1
    return counts


def compose_base(args: argparse.Namespace) -> list[str]:
    return ["docker", "compose", "-f", str(args.worker_runtime_compose), "--project-name", args.worker_runtime_project]


def runtime_container_counts(args: argparse.Namespace) -> dict[str, int]:
    if not args.worker_runtime_compose.exists():
        return {}
    result = run_command(compose_base(args) + ["ps", "--format", "json"], timeout=60)
    if result["returncode"] != 0:
        raise PauseError("worker_runtime_compose_ps_failed")
    return parse_compose_ps(str(result.get("stdout") or ""))


def worker_runtime_status(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_runtime_manifest(args.worker_runtime_manifest)
    services = manifest["services"]
    compose_present = args.worker_runtime_compose.exists()
    counts: dict[str, int] = {}
    blockers: list[str] = []
    if manifest["present"] and services and not compose_present:
        blockers.append("worker_runtime_compose_missing")
    elif manifest["present"] and services:
        try:
            counts = runtime_container_counts(args)
        except PauseError as exc:
            blockers.append(str(exc))
    service_states = []
    for service in services:
        name = service["name"]
        running = int(counts.get(name, 0))
        service_blockers = [] if running == 0 else ["worker_runtime_service_still_running"]
        service_states.append(
            {
                "name": name,
                "desired_replicas": service["replicas"],
                "running_replicas": running,
                "paused": running == 0,
                "blockers": service_blockers,
            }
        )
        blockers.extend(service_blockers)
    return {
        "id": "worker_uplift_runtime_services",
        "class": "worker_scheduler_and_stage_containers",
        "kind": "docker_compose_scale_zero",
        "manifest_present": bool(manifest["present"]),
        "compose_present": compose_present,
        "service_count": len(services),
        "services": service_states,
        "paused": not blockers,
        "blockers": sorted(set(blockers)),
        "safe_status_only": True,
    }


def unknown_runtime_writers(inventory: dict[str, Any], runtime_status: dict[str, Any]) -> list[str]:
    allowed = inventory.get("known_runtime_services", [])
    if not isinstance(allowed, list) or not allowed:
        return []
    allowed_names = {str(item) for item in allowed if isinstance(item, str)}
    services = runtime_status.get("services", [])
    if not isinstance(services, list):
        return []
    unknown = []
    for service in services:
        if not isinstance(service, dict):
            continue
        name = service.get("name")
        if isinstance(name, str) and name not in allowed_names:
            unknown.append(f"worker_runtime_service:{name}")
    return sorted(set(unknown))


def scale_runtime_service(args: argparse.Namespace, name: str, replicas: int) -> dict[str, Any]:
    return run_command(
        compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{name}={replicas}", name],
        timeout=300,
    )


def pause_runtime(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest = load_runtime_manifest(args.worker_runtime_manifest)
    if manifest["present"] and manifest["services"] and not args.worker_runtime_compose.exists():
        raise PauseError("worker_runtime_compose_missing")
    results: list[dict[str, Any]] = []
    for service in manifest["services"]:
        results.append(scale_runtime_service(args, service["name"], 0))
    failures = [item for item in results if item.get("returncode") != 0]
    if failures:
        raise PauseError("worker_runtime_pause_failed")
    return results


def recorded_runtime_replicas(previous: dict[str, Any]) -> dict[str, int]:
    before = previous.get("before", {}) if isinstance(previous, dict) else {}
    services = before.get("services", [])
    if not isinstance(services, list):
        return {}
    replicas_by_name: dict[str, int] = {}
    for service in services:
        if not isinstance(service, dict):
            continue
        name = str(service.get("name") or "")
        replicas = service.get("running_replicas")
        if not SERVICE_NAME_RE.fullmatch(name) or not isinstance(replicas, int) or replicas < 0 or replicas > 20:
            continue
        replicas_by_name[name] = replicas
    return replicas_by_name


def resume_runtime(args: argparse.Namespace, previous: dict[str, Any]) -> list[dict[str, Any]]:
    replicas_by_name = recorded_runtime_replicas(previous)
    results: list[dict[str, Any]] = []
    if replicas_by_name and not args.worker_runtime_compose.exists():
        raise PauseError("worker_runtime_compose_missing")
    for name, replicas in sorted(replicas_by_name.items()):
        results.append(scale_runtime_service(args, name, replicas))
    failures = [item for item in results if item.get("returncode") != 0]
    if failures:
        raise PauseError("worker_runtime_resume_failed")
    return results


def worker_api_resume_verification(args: argparse.Namespace, previous: dict[str, Any]) -> dict[str, Any]:
    before = previous.get("before", {}) if isinstance(previous, dict) else {}
    after = worker_api_status(args)
    before_paused = before.get("paused")
    before_active = before.get("active")
    before_enabled = before.get("enabled")
    blockers: list[str] = []
    if before_paused is False and after.get("paused") is True:
        blockers.append("backend_worker_database_api_resume_failed")
    if before_paused is True and after.get("paused") is not True:
        blockers.append("backend_worker_database_api_unexpectedly_unpaused")
    if before_active in {"active", "activating"} and after.get("active") not in {"active", "activating"}:
        blockers.append("backend_worker_database_api_active_state_not_restored")
    if before_enabled == "enabled" and after.get("enabled") != "enabled":
        blockers.append("backend_worker_database_api_enabled_state_not_restored")
    return {
        "id": "backend_worker_database_api",
        "class": "web_app_and_legacy_worker_write_api",
        "kind": "systemd_env_guard_restore",
        "expected_paused": before_paused if isinstance(before_paused, bool) else None,
        "observed_paused": after.get("paused"),
        "expected_active": before_active if isinstance(before_active, str) else None,
        "observed_active": after.get("active"),
        "expected_enabled": before_enabled if isinstance(before_enabled, str) else None,
        "observed_enabled": after.get("enabled"),
        "resumed": not blockers,
        "blockers": sorted(set(blockers)),
        "safe_status_only": True,
    }


def runtime_resume_verification(args: argparse.Namespace, previous: dict[str, Any]) -> dict[str, Any]:
    expected = recorded_runtime_replicas(previous)
    status = worker_runtime_status(args)
    observed = {
        str(service.get("name")): service
        for service in status.get("services", [])
        if isinstance(service, dict) and isinstance(service.get("name"), str)
    }
    blockers: list[str] = []
    service_results: list[dict[str, Any]] = []
    for name, replicas in sorted(expected.items()):
        service = observed.get(name, {})
        running = service.get("running_replicas")
        service_blockers: list[str] = []
        if running != replicas:
            service_blockers.append("worker_runtime_service_resume_mismatch")
        service_results.append(
            {
                "name": name,
                "expected_running_replicas": replicas,
                "observed_running_replicas": running if isinstance(running, int) else None,
                "resumed": not service_blockers,
                "blockers": service_blockers,
            }
        )
        blockers.extend(service_blockers)
    status_blockers = status.get("blockers", [])
    if isinstance(status_blockers, list):
        blockers.extend(str(item) for item in status_blockers if item and item != "worker_runtime_service_still_running")
    return {
        "id": "worker_uplift_runtime_services",
        "class": "worker_scheduler_and_stage_containers",
        "kind": "docker_compose_scale_restore",
        "expected_service_count": len(expected),
        "services": service_results,
        "resumed": not blockers,
        "blockers": sorted(set(blockers)),
        "safe_status_only": True,
    }


def resume_verification(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    writer_classes = [
        worker_api_resume_verification(args, state.get("worker_api", {})),
        runtime_resume_verification(args, state.get("worker_runtime", {})),
    ]
    blockers = [
        blocker
        for item in writer_classes
        for blocker in item.get("blockers", [])
    ]
    return {
        "status": "pass" if not blockers else "fail",
        "writer_classes": writer_classes,
        "blockers": sorted(set(blockers)),
        "safe_metadata_only": True,
    }


def drain_until_paused(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.drain_timeout_seconds
    last_status = {}
    while True:
        status = worker_runtime_status(args)
        last_status = status
        if status.get("paused") is True:
            return {
                "status": "pass",
                "timeout_seconds": args.drain_timeout_seconds,
                "checked_at_utc": utc_now(),
                "undrained_services": [],
            }
        if time.monotonic() >= deadline:
            undrained = [
                item["name"]
                for item in status.get("services", [])
                if isinstance(item, dict) and item.get("paused") is not True
            ]
            return {
                "status": "fail",
                "timeout_seconds": args.drain_timeout_seconds,
                "checked_at_utc": utc_now(),
                "undrained_services": undrained,
                "last_status": last_status,
            }
        time.sleep(min(5, max(1, args.drain_timeout_seconds // 10)))


def automation_status(inventory: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    by_id = inventory_entries(inventory)
    status_items = []
    for writer_id in ("backend_mutation_workflows", "manual_database_access", "standby_sync_relay"):
        entry = by_id.get(writer_id, {})
        status_items.append(
            {
                "id": writer_id,
                "class": entry.get("class", writer_id),
                "kind": entry.get("kind", "inventory_only"),
                "paused": True,
                "blockers": [],
                "attempt_scoped_confirmation": args.confirm_action,
                "safe_status_only": True,
            }
        )
    return status_items


def base_report(args: argparse.Namespace, inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "fail",
        "action": args.action,
        "attempt_id": args.failover_attempt_id,
        "generated_at_utc": utc_now(),
        "tracking_issue": "ramideltoro/nutsnews#526",
        "epic": "ramideltoro/nutsnews#521",
        "writer_inventory_version": inventory.get("schema_version"),
        "writer_inventory_fingerprint": inventory_fingerprint(inventory),
        "writer_classes": [],
        "unknown_writers": [],
        "drain": {"status": "not_run"},
        "all_writers_paused": False,
        "active_pause_state": False,
        "resumed_at_utc": None,
        "errors": [],
        "safe_metadata_only": True,
    }


def state_path(args: argparse.Namespace) -> Path:
    return args.state_dir / STATE_FILE


def load_state(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args)
    if not path.exists():
        return {}
    return load_json(path)


def status_report(args: argparse.Namespace, inventory: dict[str, Any]) -> dict[str, Any]:
    report = base_report(args, inventory)
    state = load_state(args)
    runtime_status = worker_runtime_status(args)
    report["active_pause_state"] = bool(state)
    report["pause_started_at_utc"] = state.get("pause_started_at_utc") if isinstance(state, dict) else None
    report["state_attempt_id"] = state.get("attempt_id") if isinstance(state, dict) else None
    report["writer_classes"] = [worker_api_status(args), runtime_status, *automation_status(inventory, args)]
    report["unknown_writers"] = unknown_runtime_writers(inventory, runtime_status)
    blockers = [
        blocker
        for item in report["writer_classes"]
        if isinstance(item, dict)
        for blocker in item.get("blockers", [])
    ]
    if report["unknown_writers"]:
        blockers.append("unknown_writer")
    if state and state.get("attempt_id") != args.failover_attempt_id:
        blockers.append("pause_state_attempt_mismatch")
    if state and state.get("resumed_at_utc"):
        blockers.append("writer_resumed_during_attempt")
        report["resumed_at_utc"] = state.get("resumed_at_utc")
    report["all_writers_paused"] = bool(report["active_pause_state"]) and not blockers
    report["status"] = "pass" if report["all_writers_paused"] else "fail"
    report["errors"] = sorted(set(blockers))
    return report


def pause_report(args: argparse.Namespace, inventory: dict[str, Any]) -> dict[str, Any]:
    if not args.confirm_action:
        raise PauseError("pause_requires_confirm_action")
    existing = load_state(args)
    if existing and existing.get("attempt_id") not in {None, args.failover_attempt_id}:
        raise PauseError("different_pause_attempt_already_active")

    before_api = worker_api_status(args)
    before_runtime = worker_runtime_status(args)
    install_worker_api_pause(args)
    pause_runtime(args)
    drain = drain_until_paused(args)
    after_api = worker_api_status(args)
    after_runtime = worker_runtime_status(args)
    report = base_report(args, inventory)
    report["pause_started_at_utc"] = existing.get("pause_started_at_utc") if existing else utc_now()
    report["writer_classes"] = [after_api, after_runtime, *automation_status(inventory, args)]
    report["unknown_writers"] = unknown_runtime_writers(inventory, after_runtime)
    report["drain"] = drain
    blockers = [
        blocker
        for item in report["writer_classes"]
        if isinstance(item, dict)
        for blocker in item.get("blockers", [])
    ]
    if drain.get("status") != "pass":
        blockers.append("drain_timeout")
    if report["unknown_writers"]:
        blockers.append("unknown_writer")
    report["all_writers_paused"] = not blockers
    report["active_pause_state"] = True
    report["status"] = "pass" if report["all_writers_paused"] else "fail"
    report["errors"] = sorted(set(blockers))
    state = {
        "schema_version": 1,
        "attempt_id": args.failover_attempt_id,
        "pause_started_at_utc": report["pause_started_at_utc"],
        "writer_inventory_fingerprint": report["writer_inventory_fingerprint"],
        "worker_api": {"before": before_api, "after": after_api},
        "worker_runtime": {"before": before_runtime, "after": after_runtime},
        "safe_metadata_only": True,
    }
    write_json(state_path(args), state, mode=0o640)
    return report


def resume_report(args: argparse.Namespace, inventory: dict[str, Any]) -> dict[str, Any]:
    if not args.confirm_action:
        raise PauseError("resume_requires_confirm_action")
    state = load_state(args)
    if not state:
        raise PauseError("pause_state_missing")
    if state.get("attempt_id") != args.failover_attempt_id:
        raise PauseError("pause_state_attempt_mismatch")
    remove_worker_api_pause(args, state.get("worker_api", {}))
    resume_runtime(args, state.get("worker_runtime", {}))
    report = base_report(args, inventory)
    report["pause_started_at_utc"] = state.get("pause_started_at_utc")
    report["resumed_at_utc"] = utc_now()
    runtime_status = worker_runtime_status(args)
    verification = resume_verification(args, state)
    report["writer_classes"] = [worker_api_status(args), runtime_status, *automation_status(inventory, args)]
    report["unknown_writers"] = unknown_runtime_writers(inventory, runtime_status)
    report["active_pause_state"] = verification["status"] != "pass"
    report["all_writers_paused"] = False
    report["resume_verification"] = verification
    report["status"] = "pass" if verification["status"] == "pass" else "fail"
    report["errors"] = verification["blockers"]
    resume_state = {
        **state,
        "resumed_at_utc": report["resumed_at_utc"],
        "resume_verification": verification,
        "safe_metadata_only": True,
    }
    write_json(args.state_dir / LAST_RESUME_FILE, resume_state, mode=0o640)
    if verification["status"] == "pass":
        try:
            state_path(args).unlink()
        except FileNotFoundError:
            pass
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "status", "resume"))
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--worker-api-env", type=Path, default=DEFAULT_WORKER_API_ENV)
    parser.add_argument("--worker-api-dropin", type=Path, default=DEFAULT_WORKER_API_DROPIN)
    parser.add_argument("--worker-runtime-manifest", type=Path, default=DEFAULT_RUNTIME_MANIFEST)
    parser.add_argument("--worker-runtime-compose", type=Path, default=DEFAULT_RUNTIME_COMPOSE)
    parser.add_argument("--worker-runtime-project", default=DEFAULT_RUNTIME_PROJECT)
    parser.add_argument("--failover-attempt-id", required=True)
    parser.add_argument("--drain-timeout-seconds", type=int, default=120)
    parser.add_argument("--confirm-action", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    require_attempt_id(args.failover_attempt_id)
    if args.drain_timeout_seconds < 1 or args.drain_timeout_seconds > 900:
        raise SystemExit("drain_timeout_seconds_out_of_bounds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        inventory = load_json(args.inventory)
        if args.action == "pause":
            report = pause_report(args, inventory)
        elif args.action == "resume":
            report = resume_report(args, inventory)
        else:
            report = status_report(args, inventory)
    except PauseError as exc:
        inventory = {}
        try:
            inventory = load_json(args.inventory)
        except PauseError:
            pass
        report = base_report(args, inventory)
        report["errors"] = [str(exc)]
    if args.report:
        write_json(args.report, report, mode=0o640)
    write_json(args.state_dir / LAST_REPORT_FILE, report, mode=0o640)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
