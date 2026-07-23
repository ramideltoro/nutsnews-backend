#!/usr/bin/env python3
"""RabbitMQ definition export and recovery drills for worker-uplift.

The production broker message store is not copied by this script. Definition
exports are sanitized before they are written to durable status paths; raw
exports are temporary, root-only, and removed after parsing.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_IMAGE = "rabbitmq@sha256:c427b73a15d01416346f429042125e663452e2a27e07fb3096fadb08f7033fc7"
DEFAULT_CONTAINER = "nutsnews-rabbitmq"
DEFAULT_ENV = Path("/etc/nutsnews-rabbitmq/rabbitmq.env")
DEFAULT_TOPOLOGY_ENV = Path("/etc/nutsnews-rabbitmq/topology.env")
DEFAULT_TOPOLOGY_DEFINITION = Path("/etc/nutsnews-rabbitmq/worker-uplift-topology.json")
DEFAULT_TOPOLOGY_SCRIPT = Path("/usr/local/sbin/nutsnews-rabbitmq-topology")
DEFAULT_STATE_DIR = Path("/var/lib/nutsnews/rabbitmq-recovery")
SANITIZED_DEFINITIONS = "definitions.sanitized.json"
STATUS_FILES = {
    "definition_export": "last-definition-export.json",
    "clean_rebuild_drill": "last-clean-rebuild-drill.json",
    "stopped_volume_restore_drill": "last-stopped-volume-restore-drill.json",
    "scheduled_check": "last-scheduled-check.json",
}
RECOVERY_TARGETS = {
    "definition_export_fresh_within_hours": 24,
    "clean_rebuild_drill_rto_seconds": 1800,
    "stopped_volume_restore_drill_rto_seconds": 3600,
    "broker_transport_rpo": "0 broker-only committed messages; PostgreSQL outbox/reconciliation is authoritative",
}
SENSITIVE_DEFINITION_KEYS = {"password", "password_hash", "password_hashing_algorithm"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def command_report(name: str, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "name": name,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-1000:],
        "stderr_tail": completed.stderr[-1000:],
    }


def ensure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o750)


def write_json(path: Path, data: dict[str, Any], mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_configured"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
    return data if isinstance(data, dict) else {"status": "unknown"}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def load_topology(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"vhost", "exchanges", "routes", "users"}
    missing = sorted(required - set(data))
    if missing:
        raise SystemExit(f"topology definition missing keys: {', '.join(missing)}")
    return data


def topology_counts(definition: dict[str, Any]) -> dict[str, int]:
    routes = definition.get("routes", [])
    queue_count = 0
    retry_count = 0
    for route in routes:
        queue_count += 2 + len(route.get("retry_queues", []))
        retry_count += len(route.get("retry_queues", []))
    return {
        "exchanges": len(definition.get("exchanges", [])),
        "routes": len(routes),
        "queues": queue_count,
        "retry_queues": retry_count,
        "users": len(definition.get("users", [])),
    }


def exported_counts(definitions: dict[str, Any]) -> dict[str, int]:
    return {
        "bindings": len(definitions.get("bindings", [])),
        "exchanges": len(definitions.get("exchanges", [])),
        "permissions": len(definitions.get("permissions", [])),
        "policies": len(definitions.get("policies", [])),
        "queues": len(definitions.get("queues", [])),
        "users": len(definitions.get("users", [])),
        "vhosts": len(definitions.get("vhosts", [])),
    }


def sanitize_definitions(definitions: dict[str, Any]) -> tuple[dict[str, Any], int]:
    sanitized = copy.deepcopy(definitions)
    removed = 0
    for section in ("users", "auth_backend_cache"):
        values = sanitized.get(section)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for key in list(item):
                if key in SENSITIVE_DEFINITION_KEYS or key.endswith("_hash"):
                    item[key] = "<redacted>"
                    removed += 1
    sanitized["x_nutsnews_sanitized"] = {
        "status": "sanitized",
        "sensitive_fields_redacted": removed,
        "raw_export_retained": False,
    }
    return sanitized, removed


def export_definitions(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started = time.monotonic()
    started_at = utc_now()
    raw_in_container = f"/tmp/nutsnews-definitions-{uuid.uuid4().hex}.json"
    with tempfile.TemporaryDirectory(prefix="nutsnews-rabbitmq-definitions-") as temp:
        raw_path = Path(temp) / "definitions.raw.json"
        export = run(["docker", "exec", args.container_name, "rabbitmqctl", "export_definitions", raw_in_container], timeout=180)
        try:
            if export.returncode != 0:
                status = {
                    "schema_version": 1,
                    "action": "export-definitions",
                    "status": "critical",
                    "started_at_utc": started_at,
                    "finished_at_utc": utc_now(),
                    "commands": [command_report("rabbitmqctl export_definitions", export)],
                    "raw_export_retained": False,
                }
                write_json(args.state_dir / STATUS_FILES["definition_export"], status)
                return status
            copy_result = run(["docker", "cp", f"{args.container_name}:{raw_in_container}", str(raw_path)], timeout=60)
            if copy_result.returncode != 0:
                status = {
                    "schema_version": 1,
                    "action": "export-definitions",
                    "status": "critical",
                    "started_at_utc": started_at,
                    "finished_at_utc": utc_now(),
                    "commands": [command_report("docker cp definitions", copy_result)],
                    "raw_export_retained": False,
                }
                write_json(args.state_dir / STATUS_FILES["definition_export"], status)
                return status
            raw_path.chmod(0o600)
            definitions = json.loads(raw_path.read_text(encoding="utf-8"))
            sanitized, redacted = sanitize_definitions(definitions if isinstance(definitions, dict) else {})
            sanitized_path = args.state_dir / SANITIZED_DEFINITIONS
            write_json(sanitized_path, sanitized, mode=0o640)
            topology = load_topology(args.definition)
            status = {
                "schema_version": 1,
                "action": "export-definitions",
                "status": "healthy",
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "sanitized_definitions_path": str(sanitized_path),
                "raw_export_retained": False,
                "sensitive_fields_redacted": redacted,
                "exported_counts": exported_counts(sanitized),
                "source_topology_counts": topology_counts(topology),
                "definition_fresh_within_hours": RECOVERY_TARGETS["definition_export_fresh_within_hours"],
                "recovery_targets": RECOVERY_TARGETS,
            }
            write_json(args.state_dir / STATUS_FILES["definition_export"], status)
            return status
        finally:
            run(["docker", "exec", args.container_name, "rm", "-f", raw_in_container], timeout=30)


def generated_drill_environment(definition: dict[str, Any], directory: Path) -> tuple[Path, Path, str, str]:
    admin_username = "nutsnews_recovery_admin"
    admin_password = secrets.token_urlsafe(24)
    erlang_cookie = secrets.token_urlsafe(32)
    vhost = definition["vhost"]
    env_path = directory / "rabbitmq.env"
    credentials_path = directory / "topology.env"
    env_path.write_text(
        "\n".join(
            [
                f"RABBITMQ_DEFAULT_USER={admin_username}",
                f"RABBITMQ_DEFAULT_PASS={admin_password}",
                f"RABBITMQ_DEFAULT_VHOST={vhost}",
                f"RABBITMQ_ERLANG_COOKIE={erlang_cookie}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    lines: list[str] = []
    for user in definition["users"]:
        username_variable = user["username_variable"]
        password_variable = user["password_variable"]
        if user["id"] == "break_glass_admin":
            username = admin_username
            password = admin_password
        else:
            username = f"drill_{user['id']}"
            password = secrets.token_urlsafe(24)
        lines.append(f"{username_variable}={username}")
        lines.append(f"{password_variable}={password}")
    credentials_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    credentials_path.chmod(0o600)
    return env_path, credentials_path, admin_username, erlang_cookie


def chown_rabbitmq_data(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run(["chown", "-R", "999:999", str(path)], timeout=60)


def start_drill_container(
    *,
    image: str,
    container_name: str,
    hostname: str,
    data_dir: Path,
    env_path: Path,
) -> str:
    chown_rabbitmq_data(data_dir)
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--hostname",
        hostname,
        "--env-file",
        str(env_path),
        "-v",
        f"{data_dir}:/var/lib/rabbitmq",
        "-p",
        "127.0.0.1::15672",
        image,
    ]
    result = run(command, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start drill container: {result.stderr[-500:]}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ports = run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_name], timeout=30)
        if ports.returncode == 0:
            try:
                data = json.loads(ports.stdout)
                host_port = data["15672/tcp"][0]["HostPort"]
                if host_port:
                    return str(host_port)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                pass
        time.sleep(1)
    raise RuntimeError("drill container management port was not published")


def remove_container(container_name: str) -> None:
    run(["docker", "rm", "-f", container_name], timeout=60)


def topology_command(
    args: argparse.Namespace,
    action: str,
    *,
    env_path: Path,
    credentials_path: Path,
    management_port: str,
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            str(args.topology_script),
            action,
            "--env",
            str(env_path),
            "--credentials-env",
            str(credentials_path),
            "--definition",
            str(args.definition),
            "--management-url",
            f"http://127.0.0.1:{management_port}",
            "--timeout-seconds",
            str(args.timeout_seconds),
        ],
        timeout=args.timeout_seconds + 60,
    )


def run_topology_sequence(
    args: argparse.Namespace,
    *,
    env_path: Path,
    credentials_path: Path,
    management_port: str,
    actions: tuple[str, ...],
) -> tuple[bool, list[dict[str, Any]]]:
    reports: list[dict[str, Any]] = []
    ok = True
    for action in actions:
        completed = topology_command(
            args,
            action,
            env_path=env_path,
            credentials_path=credentials_path,
            management_port=management_port,
        )
        report = command_report(f"topology {action}", completed)
        try:
            report["json"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            report["json"] = {}
        reports.append(report)
        if completed.returncode != 0:
            ok = False
            break
    return ok, reports


def action_clean_rebuild_drill(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started = time.monotonic()
    started_at = utc_now()
    definition = load_topology(args.definition)
    container_name = f"nutsnews-rabbitmq-clean-drill-{uuid.uuid4().hex[:10]}"
    with tempfile.TemporaryDirectory(prefix="nutsnews-rabbitmq-clean-drill-") as temp:
        temp_dir = Path(temp)
        env_path, credentials_path, _, _ = generated_drill_environment(definition, temp_dir)
        data_dir = temp_dir / "rabbitmq-data"
        try:
            port = start_drill_container(
                image=args.image,
                container_name=container_name,
                hostname="nutsnews-rabbitmq-clean-drill",
                data_dir=data_dir,
                env_path=env_path,
            )
            ok, reports = run_topology_sequence(
                args,
                env_path=env_path,
                credentials_path=credentials_path,
                management_port=port,
                actions=("bootstrap", "check", "permissions", "probe-transfers"),
            )
        except Exception as exc:
            ok = False
            reports = [{"name": "clean_rebuild_drill", "returncode": 1, "stderr_tail": str(exc), "stdout_tail": ""}]
        finally:
            remove_container(container_name)
    duration = round(time.monotonic() - started, 3)
    status = {
        "schema_version": 1,
        "action": "clean-rebuild-drill",
        "status": "healthy" if ok and duration <= RECOVERY_TARGETS["clean_rebuild_drill_rto_seconds"] else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "duration_seconds": duration,
        "target_rto_seconds": RECOVERY_TARGETS["clean_rebuild_drill_rto_seconds"],
        "topology_source": str(args.definition),
        "rebuild_source_of_truth": "pinned image + source-controlled topology + protected credentials + PostgreSQL outbox/reconciliation replay",
        "postgresql_replay_status": "authoritative_source_required_for_real_repopulation; worker outbox replay is exercised by later service issues",
        "commands": reports,
        "recovery_targets": RECOVERY_TARGETS,
    }
    write_json(args.state_dir / STATUS_FILES["clean_rebuild_drill"], status)
    return status


def action_stopped_volume_restore_drill(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started = time.monotonic()
    started_at = utc_now()
    definition = load_topology(args.definition)
    hostname = "nutsnews-rabbitmq-stopped-restore-drill"
    first_container = f"nutsnews-rabbitmq-stop-drill-a-{uuid.uuid4().hex[:10]}"
    second_container = f"nutsnews-rabbitmq-stop-drill-b-{uuid.uuid4().hex[:10]}"
    reports: list[dict[str, Any]] = []
    ok = True
    with tempfile.TemporaryDirectory(prefix="nutsnews-rabbitmq-stopped-restore-drill-") as temp:
        temp_dir = Path(temp)
        env_path, credentials_path, _, _ = generated_drill_environment(definition, temp_dir)
        original_data = temp_dir / "original-data"
        restored_data = temp_dir / "restored-data"
        try:
            first_port = start_drill_container(
                image=args.image,
                container_name=first_container,
                hostname=hostname,
                data_dir=original_data,
                env_path=env_path,
            )
            ok, bootstrap_reports = run_topology_sequence(
                args,
                env_path=env_path,
                credentials_path=credentials_path,
                management_port=first_port,
                actions=("bootstrap", "check", "permissions"),
            )
            reports.extend(bootstrap_reports)
            stop_result = run(["docker", "stop", first_container], timeout=120)
            reports.append(command_report("docker stop source drill broker", stop_result))
            ok = ok and stop_result.returncode == 0
            if ok:
                shutil.copytree(original_data, restored_data, symlinks=True)
                chown_rabbitmq_data(restored_data)
                second_port = start_drill_container(
                    image=args.image,
                    container_name=second_container,
                    hostname=hostname,
                    data_dir=restored_data,
                    env_path=env_path,
                )
                restored_ok, restored_reports = run_topology_sequence(
                    args,
                    env_path=env_path,
                    credentials_path=credentials_path,
                    management_port=second_port,
                    actions=("check", "permissions"),
                )
                reports.extend(restored_reports)
                ok = ok and restored_ok
        except Exception as exc:
            ok = False
            reports.append({"name": "stopped_volume_restore_drill", "returncode": 1, "stderr_tail": str(exc), "stdout_tail": ""})
        finally:
            remove_container(first_container)
            remove_container(second_container)
    duration = round(time.monotonic() - started, 3)
    status = {
        "schema_version": 1,
        "action": "stopped-volume-restore-drill",
        "status": "healthy" if ok and duration <= RECOVERY_TARGETS["stopped_volume_restore_drill_rto_seconds"] else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "duration_seconds": duration,
        "target_rto_seconds": RECOVERY_TARGETS["stopped_volume_restore_drill_rto_seconds"],
        "same_node_name_required": True,
        "same_erlang_cookie_required": True,
        "production_message_store_snapshot": "not_taken_by_normal_backup; this drill uses disposable stopped data only",
        "recovery_point_gap": "messages published after a quiesced snapshot must be replayed from PostgreSQL outbox/reconciliation state",
        "commands": reports,
        "recovery_targets": RECOVERY_TARGETS,
    }
    write_json(args.state_dir / STATUS_FILES["stopped_volume_restore_drill"], status)
    return status


def action_scheduled_check(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now()
    export = export_definitions(args)
    clean = action_clean_rebuild_drill(args)
    status = {
        "schema_version": 1,
        "action": "scheduled-check",
        "status": "healthy" if export.get("status") == "healthy" and clean.get("status") == "healthy" else "critical",
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "definition_export_status": export.get("status"),
        "clean_rebuild_drill_status": clean.get("status"),
        "stopped_volume_restore_drill_status": read_json(args.state_dir / STATUS_FILES["stopped_volume_restore_drill"]).get("status"),
        "artifact_policy": "workflow artifacts contain status JSON only; raw definitions and password hashes are not uploaded",
    }
    write_json(args.state_dir / STATUS_FILES["scheduled_check"], status)
    return status


def action_status(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "action": "status",
        "generated_at_utc": utc_now(),
        "definition_export": read_json(args.state_dir / STATUS_FILES["definition_export"]),
        "clean_rebuild_drill": read_json(args.state_dir / STATUS_FILES["clean_rebuild_drill"]),
        "stopped_volume_restore_drill": read_json(args.state_dir / STATUS_FILES["stopped_volume_restore_drill"]),
        "scheduled_check": read_json(args.state_dir / STATUS_FILES["scheduled_check"]),
        "sanitized_definitions_path": str(args.state_dir / SANITIZED_DEFINITIONS),
        "message_store_policy": "normal Restic jobs exclude live /var/lib/nutsnews/rabbitmq; stopped-volume restore requires quiesced broker snapshot",
        "rebuild_policy": "normal broker rebuild uses pinned image/config, topology bootstrap, credential provisioning, and PostgreSQL outbox/reconciliation replay",
        "recovery_targets": RECOVERY_TARGETS,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("status", "export-definitions", "clean-rebuild-drill", "stopped-volume-restore-drill", "scheduled-check"),
    )
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--topology-env", type=Path, default=DEFAULT_TOPOLOGY_ENV)
    parser.add_argument("--definition", type=Path, default=DEFAULT_TOPOLOGY_DEFINITION)
    parser.add_argument("--topology-script", type=Path, default=DEFAULT_TOPOLOGY_SCRIPT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    actions = {
        "status": action_status,
        "export-definitions": export_definitions,
        "clean-rebuild-drill": action_clean_rebuild_drill,
        "stopped-volume-restore-drill": action_stopped_volume_restore_drill,
        "scheduled-check": action_scheduled_check,
    }
    try:
        result = actions[args.action](args)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result = {
            "schema_version": 1,
            "action": args.action,
            "status": "critical",
            "finished_at_utc": utc_now(),
            "error": exc.__class__.__name__,
            "detail": str(exc)[-500:],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"healthy", "not_configured"} or args.action == "status" else 1


if __name__ == "__main__":
    raise SystemExit(main())
