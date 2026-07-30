#!/usr/bin/env python3
"""RabbitMQ definition export and recovery drills for worker-uplift.

The production broker message store is not copied by this script. Definition
exports are sanitized before they are written to durable status paths; raw
exports are temporary, root-only, and removed after parsing.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_IMAGE = "rabbitmq@sha256:c427b73a15d01416346f429042125e663452e2a27e07fb3096fadb08f7033fc7"
DEFAULT_CONTAINER = "nutsnews-rabbitmq"
DEFAULT_ENV = Path("/etc/nutsnews-rabbitmq/rabbitmq.env")
DEFAULT_TOPOLOGY_ENV = Path("/etc/nutsnews-rabbitmq/topology.env")
DEFAULT_TOPOLOGY_DEFINITION = Path("/etc/nutsnews-rabbitmq/worker-uplift-topology.json")
DEFAULT_TOPOLOGY_SCRIPT = Path("/usr/local/sbin/nutsnews-rabbitmq-topology")
DEFAULT_STATE_DIR = Path("/var/lib/nutsnews/rabbitmq-recovery")
DEFAULT_RUNTIME_MANIFEST = Path("/etc/nutsnews-worker-uplift/services.json")
DEFAULT_RUNTIME_COMPOSE = Path("/opt/nutsnews-worker-uplift/compose.yml")
SANITIZED_DEFINITIONS = "definitions.sanitized.json"
STATUS_FILES = {
    "definition_export": "last-definition-export.json",
    "clean_rebuild_drill": "last-clean-rebuild-drill.json",
    "current_candidate_reconciliation_drill": "last-current-candidate-reconciliation-drill.json",
    "stopped_volume_restore_drill": "last-stopped-volume-restore-drill.json",
    "scheduled_check": "last-scheduled-check.json",
}
RECOVERY_TARGETS = {
    "definition_export_fresh_within_hours": 24,
    "clean_rebuild_drill_rto_seconds": 1800,
    "current_candidate_reconciliation_drill_rto_seconds": 900,
    "stopped_volume_restore_drill_rto_seconds": 3600,
    "broker_transport_rpo": "0 broker-only committed messages; PostgreSQL outbox/reconciliation is authoritative",
}
SENSITIVE_DEFINITION_KEYS = {"password", "password_hash", "password_hashing_algorithm"}
CANDIDATE_CONSUMER_STAGES = (
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
RECONCILIATION_CONFIRMATION = "persistence:replay-outbox:v1"
RECONCILIATION_MAX_ITEMS = 1
RECONCILIATION_MIN_AGE_SECONDS = 900
SECRET_ENV_MARKERS = ("DATABASE_URL", "RABBITMQ_URL", "PASSWORD", "TOKEN", "SECRET", "API_KEY", "PRIVATE_KEY")


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
    if isinstance(definition.get("canary"), dict) and isinstance(definition["canary"].get("queue"), dict):
        queue_count += 1
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
    return env_path, credentials_path, admin_username, admin_password


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
    publish_amqp: bool = False,
) -> dict[str, str]:
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
    ]
    if publish_amqp:
        command.extend(["-p", "127.0.0.1::5672"])
    command.append(image)
    result = run(command, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start drill container: {result.stderr[-500:]}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ports = run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_name], timeout=30)
        if ports.returncode == 0:
            try:
                data = json.loads(ports.stdout)
                management_port = str(data["15672/tcp"][0]["HostPort"])
                amqp_bindings = data.get("5672/tcp") or []
                amqp_port = str(amqp_bindings[0]["HostPort"]) if amqp_bindings else ""
                if management_port and (not publish_amqp or amqp_port):
                    return {
                        "management": management_port,
                        "amqp": amqp_port,
                    }
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                pass
        time.sleep(1)
    raise RuntimeError("drill container ports were not published")


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


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_json(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql_rows(database_url: str, query: str, timeout: int = 60) -> list[list[str]]:
    try:
        completed = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", database_url, "-c", query],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("psql is not installed on the backend host") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"PostgreSQL evidence query failed: {completed.stderr[-500:]}")
    return [line.split("\t") for line in completed.stdout.splitlines() if line]


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def safe_environment_fingerprint(values: dict[str, str]) -> dict[str, Any]:
    redacted = {
        key: "<present>" if any(marker in key for marker in SECRET_ENV_MARKERS) else value
        for key, value in sorted(values.items())
    }
    return {
        "redacted_sha256": sha256_json(redacted),
        "keys": sorted(values),
        "protected_value_keys": sorted(
            key for key in values if any(marker in key for marker in SECRET_ENV_MARKERS)
        ),
    }


def stage_consumer_credentials(
    definition: dict[str, Any],
    credentials: dict[str, str],
    stage: str,
) -> tuple[str, str]:
    matches = [
        user
        for user in definition.get("users", [])
        if isinstance(user, dict)
        and user.get("stage") == stage
        and user.get("id") == f"{stage}_consumer"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"topology must declare exactly one consumer identity for {stage}")
    user = matches[0]
    username = credentials.get(str(user.get("username_variable") or ""), "")
    password = credentials.get(str(user.get("password_variable") or ""), "")
    if not username or not password:
        raise RuntimeError(f"throwaway credentials are incomplete for {stage}")
    return username, password


def write_candidate_environment(
    *,
    source_path: Path,
    destination_path: Path,
    definition: dict[str, Any],
    credentials: dict[str, str],
    stage: str,
    amqp_port: str,
    http_port: int,
) -> dict[str, Any]:
    values = parse_env(source_path)
    source_fingerprint = safe_environment_fingerprint(values)
    prefix = f"NUTSNEWS_{stage.upper()}"
    rabbitmq_key = f"{prefix}_RABBITMQ_URL"
    username, password = stage_consumer_credentials(definition, credentials, stage)
    vhost = str(definition["vhost"])
    values[rabbitmq_key] = (
        f"amqp://{parse.quote(username, safe='')}:{parse.quote(password, safe='')}"
        f"@127.0.0.1:{amqp_port}/{parse.quote(vhost, safe='')}"
    )
    values[f"{prefix}_HTTP_HOST"] = "127.0.0.1"
    values[f"{prefix}_HTTP_PORT"] = str(http_port)
    if stage == "persistence":
        values["NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_APPLY_ENABLED"] = "true"
        values["NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_STOP"] = "false"

    if values.get(f"{prefix}_PRODUCTION_WRITES_ENABLED", "false").strip().lower() == "true":
        raise RuntimeError(f"{stage} candidate environment enables production writes")
    if stage != "publication" and values.get(f"{prefix}_SHADOW_MODE", "").strip().lower() != "true":
        raise RuntimeError(f"{stage} candidate environment is not shadow-only")
    if stage == "publication" and values.get("NUTSNEWS_PUBLICATION_WRITE_MODE") != "shadow_comparison":
        raise RuntimeError("publication candidate environment is not shadow comparison")
    if any("\n" in value or "\r" in value for value in values.values()):
        raise RuntimeError(f"{stage} candidate environment contains a newline")

    destination_path.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
        encoding="utf-8",
    )
    destination_path.chmod(0o600)
    return {
        "source": str(source_path),
        "source_fingerprint": source_fingerprint,
        "effective_fingerprint": safe_environment_fingerprint(values),
        "overrides": {
            "rabbitmq": "throwaway-loopback-broker",
            "http_port": http_port,
            "reconciliation_apply_enabled": stage == "persistence",
        },
        "production_writes_enabled": False,
        "runtime_mode": "shadow",
    }


def start_candidate_container(
    *,
    image: str,
    container_name: str,
    env_path: Path,
) -> None:
    completed = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            "host",
            "--env-file",
            str(env_path),
            "--memory",
            "512m",
            "--cpus",
            "0.50",
            "--pids-limit",
            "256",
            image,
        ],
        timeout=240,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"failed to start {container_name}: {completed.stderr[-500:]}")


def candidate_container_snapshot(container_name: str) -> dict[str, Any]:
    completed = run(["docker", "inspect", container_name], timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(f"candidate container inspection failed for {container_name}")
    data = json.loads(completed.stdout)
    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError(f"candidate container inspection returned an unexpected shape for {container_name}")
    container = data[0]
    return {
        "configured_image": str(container.get("Config", {}).get("Image") or ""),
        "content_image_id": str(container.get("Image") or ""),
        "network_mode": str(container.get("HostConfig", {}).get("NetworkMode") or ""),
    }


def wait_for_http_ready(port: int, timeout_seconds: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unreachable"
    while time.monotonic() < deadline:
        try:
            with request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=5) as response:
                last_status = str(response.status)
                if 200 <= response.status < 300:
                    return {"status": "healthy", "http_status": response.status}
        except (error.URLError, TimeoutError):
            last_status = "unreachable"
        time.sleep(2)
    return {"status": "critical", "http_status": last_status}


def rabbitmq_management_get(
    *,
    management_port: str,
    admin_username: str,
    admin_password: str,
    path: str,
) -> Any:
    encoded = base64.b64encode(f"{admin_username}:{admin_password}".encode("utf-8")).decode("ascii")
    req = request.Request(
        f"http://127.0.0.1:{management_port}/api/{path.lstrip('/')}",
        headers={"authorization": f"Basic {encoded}"},
    )
    with request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def queue_snapshot(
    *,
    management_port: str,
    admin_username: str,
    admin_password: str,
    vhost: str,
    queue: str,
) -> dict[str, Any]:
    data = rabbitmq_management_get(
        management_port=management_port,
        admin_username=admin_username,
        admin_password=admin_password,
        path=f"queues/{parse.quote(vhost, safe='')}/{parse.quote(queue, safe='')}",
    )
    stats = data.get("message_stats", {}) if isinstance(data, dict) else {}
    return {
        "queue": queue,
        "consumers": int(data.get("consumers", 0) or 0),
        "messages": int(data.get("messages", 0) or 0),
        "messages_ready": int(data.get("messages_ready", 0) or 0),
        "messages_unacknowledged": int(data.get("messages_unacknowledged", 0) or 0),
        "published": int(stats.get("publish", 0) or 0),
        "delivered": int(stats.get("deliver_get", 0) or 0),
        "acked": int(stats.get("ack", 0) or 0),
    }


def wait_for_queue_drain(
    *,
    management_port: str,
    admin_username: str,
    admin_password: str,
    vhost: str,
    queue: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = queue_snapshot(
            management_port=management_port,
            admin_username=admin_username,
            admin_password=admin_password,
            vhost=vhost,
            queue=queue,
        )
        if last["messages"] == 0 and last["consumers"] >= 1 and last["acked"] >= 1:
            return last
        time.sleep(2)
    return last


def post_json(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"reconciliation endpoint returned HTTP {exc.code}: {body_text[-500:]}") from exc


def select_reconciliation_candidate(database_url: str) -> dict[str, str]:
    rows = psql_rows(
        database_url,
        f"""
select id::text, entity_id, idempotency_key, pipeline_run_id, created_at::text,
       coalesce(jsonb_array_length(diagnostic_metadata->'reconciliationAuditHistory'), 0)::text
from worker_uplift_persistence.outbox
where status = 'confirmed'
  and confirmed_at is not null
  and created_at <= now() - ({RECONCILIATION_MIN_AGE_SECONDS}::integer * interval '1 second')
order by created_at asc, id asc
limit {RECONCILIATION_MAX_ITEMS};
""",
    )
    if len(rows) != 1 or len(rows[0]) != 6:
        raise RuntimeError("authoritative PostgreSQL range did not return exactly one bounded candidate")
    row = rows[0]
    return {
        "outbox_id": row[0],
        "entity_id": row[1],
        "idempotency_key": row[2],
        "pipeline_run_id": row[3],
        "created_at": row[4],
        "audit_count": row[5],
    }


def reconciliation_candidate_evidence(candidate: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": "worker_uplift_persistence",
        "table": "outbox",
        "primary_key_range": {
            "minimum": candidate["outbox_id"],
            "maximum": candidate["outbox_id"],
        },
        "created_at": candidate["created_at"],
        "entity_sha256": sha256_bytes(candidate["entity_id"].encode("utf-8")),
        "idempotency_key_sha256": sha256_bytes(candidate["idempotency_key"].encode("utf-8")),
        "pipeline_run_id_sha256": sha256_bytes(candidate["pipeline_run_id"].encode("utf-8")),
        "pre_reconciliation_audit_count": int(candidate["audit_count"]),
        "limit": RECONCILIATION_MAX_ITEMS,
        "minimum_age_seconds": RECONCILIATION_MIN_AGE_SECONDS,
    }


def side_effect_counts(database_url: str, candidate: dict[str, str]) -> dict[str, int]:
    entity = sql_literal(candidate["entity_id"])
    idempotency = sql_literal(candidate["idempotency_key"])
    rows = psql_rows(
        database_url,
        f"""
select 'publication_inbox', count(*)::text
from worker_uplift_publication.inbox
where idempotency_key = {idempotency}
union all
select 'publication_readiness', count(*)::text
from worker_uplift_publication.publication_readiness
where article_identity_hash = {entity}
union all
select 'publication_decisions', count(*)::text
from worker_uplift_publication.publication_decisions
where article_identity_hash = {entity}
union all
select 'shadow_api_write_requests', count(*)::text
from worker_uplift_persistence.write_requests
where article_identity_hash = {entity};
""",
    )
    return {key: int(value) for key, value in rows}


def reconciliation_audit_count(database_url: str, outbox_id: str) -> int:
    rows = psql_rows(
        database_url,
        f"""
select coalesce(jsonb_array_length(diagnostic_metadata->'reconciliationAuditHistory'), 0)::text
from worker_uplift_persistence.outbox
where id = {int(outbox_id)};
""",
    )
    if len(rows) != 1 or len(rows[0]) != 1:
        raise RuntimeError("reconciliation audit row was not found after replay")
    return int(rows[0][0])


def live_broker_snapshot(container_name: str) -> dict[str, Any]:
    completed = run(["docker", "inspect", container_name], timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(f"live RabbitMQ container inspection failed: {completed.stderr[-500:]}")
    data = json.loads(completed.stdout)
    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError("live RabbitMQ container inspection returned an unexpected shape")
    container = data[0]
    state = container.get("State", {})
    return {
        "container_id_sha256": sha256_bytes(str(container.get("Id") or "").encode("utf-8")),
        "image": str(container.get("Config", {}).get("Image") or ""),
        "started_at": str(state.get("StartedAt") or ""),
        "restart_count": int(container.get("RestartCount", 0) or 0),
        "status": str(state.get("Status") or ""),
    }


def safe_topology_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": report.get("name"),
            "returncode": report.get("returncode"),
            "status": report.get("json", {}).get("status"),
        }
        for report in reports
    ]


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
            ports = start_drill_container(
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
                management_port=ports["management"],
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


def action_current_candidate_reconciliation_drill(args: argparse.Namespace) -> dict[str, Any]:
    ensure_state_dir(args.state_dir)
    started = time.monotonic()
    started_at = utc_now()
    definition = load_topology(args.definition)
    manifest = read_json(args.runtime_manifest)
    if manifest.get("mode") != "shadow" or manifest.get("production_writes_enabled") is not False:
        raise RuntimeError("deployed worker candidate is not shadow-only")
    if manifest.get("cutover_state") not in {"disabled", "shadow", "not_started"}:
        raise RuntimeError("deployed worker candidate cutover state is not disabled")

    services = {
        str(service.get("name") or ""): service
        for service in manifest.get("services", [])
        if isinstance(service, dict)
    }
    missing_services = sorted(set(CANDIDATE_CONSUMER_STAGES) - set(services))
    if missing_services:
        raise RuntimeError(f"deployed worker manifest is missing consumers: {', '.join(missing_services)}")
    for stage in CANDIDATE_CONSUMER_STAGES:
        service = services[stage]
        image = str(service.get("image") or "")
        if not image.startswith("ghcr.io/ramideltoro/") or "@sha256:" not in image:
            raise RuntimeError(f"{stage} candidate image is not digest-pinned")
        if service.get("runtime_mode") != "shadow":
            raise RuntimeError(f"{stage} candidate runtime mode is not shadow")
        if service.get("postgres", {}).get("production_write_path") is not False:
            raise RuntimeError(f"{stage} candidate declares a production PostgreSQL write path")

    live_broker_before = live_broker_snapshot(args.container_name)
    broker_container = f"nutsnews-rabbitmq-candidate-drill-{uuid.uuid4().hex[:10]}"
    candidate_containers: list[str] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "action": "current-candidate-reconciliation-drill",
        "tracking_issue": 159,
        "status": "critical",
        "started_at_utc": started_at,
        "candidate": {
            "manifest_path": str(args.runtime_manifest),
            "manifest_sha256": sha256_file(args.runtime_manifest),
            "compose_path": str(args.runtime_compose),
            "compose_sha256": sha256_file(args.runtime_compose),
            "contract_version": manifest.get("contract_version"),
            "runtime_version": manifest.get("runtime_version"),
            "mode": manifest.get("mode"),
            "production_writes_enabled": manifest.get("production_writes_enabled"),
            "cutover_state": manifest.get("cutover_state"),
            "images": {
                name: {
                    "image": service.get("image"),
                    "source_commit": service.get("image_tag"),
                    "contract_version": service.get("contract_version"),
                    "runtime_package_version": service.get("runtime_package_version"),
                }
                for name, service in sorted(services.items())
            },
        },
        "topology": {
            "path": str(args.definition),
            "sha256": sha256_file(args.definition),
            "counts": topology_counts(definition),
        },
        "limits": {
            "candidate_count": RECONCILIATION_MAX_ITEMS,
            "minimum_age_seconds": RECONCILIATION_MIN_AGE_SECONDS,
            "target_rto_seconds": RECOVERY_TARGETS["current_candidate_reconciliation_drill_rto_seconds"],
        },
        "guardrails": {
            "broker_target": "throwaway-loopback-container",
            "live_production_broker_commands": ["docker inspect"],
            "live_production_broker_mutated": False,
            "legacy_ingestion_mutated": False,
            "production_writes_enabled": False,
            "dns_or_failover_mutated": False,
            "scheduler_started": False,
            "scheduler_exclusion": "scheduler is a producer, not an expected recovery consumer",
        },
        "live_broker_before": live_broker_before,
    }

    with tempfile.TemporaryDirectory(prefix="nutsnews-rabbitmq-candidate-drill-") as temp:
        temp_dir = Path(temp)
        env_path, credentials_path, admin_username, admin_password = generated_drill_environment(definition, temp_dir)
        credentials = parse_env(credentials_path)
        data_dir = temp_dir / "rabbitmq-data"
        try:
            ports = start_drill_container(
                image=args.image,
                container_name=broker_container,
                hostname="nutsnews-rabbitmq-candidate-drill",
                data_dir=data_dir,
                env_path=env_path,
                publish_amqp=True,
            )
            topology_ok, topology_reports = run_topology_sequence(
                args,
                env_path=env_path,
                credentials_path=credentials_path,
                management_port=ports["management"],
                actions=("bootstrap", "check", "permissions", "probe-transfers"),
            )
            report["topology"]["actions"] = safe_topology_reports(topology_reports)
            if not topology_ok:
                raise RuntimeError("throwaway broker topology bootstrap failed")

            runtime_services: dict[str, Any] = {}
            service_ports: dict[str, int] = {}
            for stage in CANDIDATE_CONSUMER_STAGES:
                service = services[stage]
                source_env = args.runtime_manifest.parent / "services" / f"{stage}.env"
                if not source_env.exists():
                    raise RuntimeError(f"deployed environment is missing for {stage}")
                candidate_env = temp_dir / f"{stage}.env"
                http_port = free_loopback_port()
                service_ports[stage] = http_port
                config_evidence = write_candidate_environment(
                    source_path=source_env,
                    destination_path=candidate_env,
                    definition=definition,
                    credentials=credentials,
                    stage=stage,
                    amqp_port=ports["amqp"],
                    http_port=http_port,
                )
                container_name = f"nutsnews-{stage}-candidate-drill-{uuid.uuid4().hex[:8]}"
                candidate_containers.append(container_name)
                start_candidate_container(
                    image=str(service["image"]),
                    container_name=container_name,
                    env_path=candidate_env,
                )
                runtime_services[stage] = {
                    "config": config_evidence,
                    "container": candidate_container_snapshot(container_name),
                    "main_queue": service.get("queues", {}).get("main"),
                    "dlq": service.get("queues", {}).get("dlq"),
                }

            for stage in CANDIDATE_CONSUMER_STAGES:
                runtime_services[stage]["readiness"] = wait_for_http_ready(service_ports[stage])
                if runtime_services[stage]["readiness"]["status"] != "healthy":
                    raise RuntimeError(f"{stage} exact-candidate consumer did not become ready")

            main_before: dict[str, Any] = {}
            dlq_before: dict[str, Any] = {}
            for stage in CANDIDATE_CONSUMER_STAGES:
                main_queue = str(runtime_services[stage]["main_queue"] or "")
                dlq = str(runtime_services[stage]["dlq"] or "")
                main_before[stage] = queue_snapshot(
                    management_port=ports["management"],
                    admin_username=admin_username,
                    admin_password=admin_password,
                    vhost=str(definition["vhost"]),
                    queue=main_queue,
                )
                dlq_before[stage] = queue_snapshot(
                    management_port=ports["management"],
                    admin_username=admin_username,
                    admin_password=admin_password,
                    vhost=str(definition["vhost"]),
                    queue=dlq,
                )
            report["runtime"] = {
                "services": runtime_services,
                "main_queues_before": main_before,
                "dlqs_before": dlq_before,
            }
            if any(snapshot["consumers"] != 1 for snapshot in main_before.values()):
                raise RuntimeError("not all seven exact-candidate consumers registered on the isolated topology")
            if any(snapshot["messages"] != 0 for snapshot in main_before.values()):
                raise RuntimeError("isolated main queues were not empty before reconciliation")
            if any(snapshot["messages"] != 0 for snapshot in dlq_before.values()):
                raise RuntimeError("isolated DLQs were not empty before reconciliation")

            persistence_env = parse_env(temp_dir / "persistence.env")
            database_url = persistence_env.get("NUTSNEWS_PERSISTENCE_DATABASE_URL", "")
            token = persistence_env.get("NUTSNEWS_WORKER_UPLIFT_RECONCILIATION_TOKEN", "")
            if not database_url or not token:
                raise RuntimeError("persistence drill prerequisites are incomplete")
            candidate = select_reconciliation_candidate(database_url)
            pre_side_effects = side_effect_counts(database_url, candidate)
            if any(count < 1 for count in pre_side_effects.values()):
                raise RuntimeError("bounded candidate lacks prior shadow/API state required for duplicate-side-effect proof")
            run_id = f"backend:isolated-recovery:{uuid.uuid4()}"
            service_report = post_json(
                f"http://127.0.0.1:{service_ports['persistence']}/reconcile/outbox",
                token,
                {
                    "mode": "apply",
                    "runId": run_id,
                    "reason": "backend-worker-uplift-isolated-empty-broker-recovery",
                    "maxItems": RECONCILIATION_MAX_ITEMS,
                    "minAgeSeconds": RECONCILIATION_MIN_AGE_SECONDS,
                    "protectedConfirmation": RECONCILIATION_CONFIRMATION,
                },
            )
            safe_service_report = {
                key: service_report.get(key)
                for key in (
                    "service",
                    "mode",
                    "status",
                    "selectedCount",
                    "replayedCount",
                    "failedClosedCount",
                    "skippedCount",
                    "writesPerformed",
                    "dryRun",
                    "productionVisibilityEnabled",
                    "legacyRuntimeRequired",
                    "protectedApplyRequired",
                    "errors",
                    "metrics",
                )
            }
            report["reconciliation"] = {
                "run_id": run_id,
                "source": reconciliation_candidate_evidence(candidate),
                "pre_side_effect_counts": pre_side_effects,
                "service_report": safe_service_report,
            }
            if (
                service_report.get("status") != "applied"
                or service_report.get("selectedCount") != 1
                or service_report.get("replayedCount") != 1
                or service_report.get("failedClosedCount") != 0
                or service_report.get("productionVisibilityEnabled") is not False
                or service_report.get("legacyRuntimeRequired") is not False
                or service_report.get("errors") not in ([], None)
            ):
                raise RuntimeError("service-owned persistence reconciliation did not replay exactly one safe candidate")

            publication_queue = str(runtime_services["publication"]["main_queue"])
            publication_after = wait_for_queue_drain(
                management_port=ports["management"],
                admin_username=admin_username,
                admin_password=admin_password,
                vhost=str(definition["vhost"]),
                queue=publication_queue,
            )
            post_side_effects = side_effect_counts(database_url, candidate)
            post_audit_count = reconciliation_audit_count(database_url, candidate["outbox_id"])
            report["reconciliation"]["post_side_effect_counts"] = post_side_effects
            report["reconciliation"]["post_reconciliation_audit_count"] = post_audit_count
            report["reconciliation"]["duplicate_domain_or_api_side_effects"] = (
                post_side_effects != pre_side_effects
            )

            main_after: dict[str, Any] = {}
            dlq_after: dict[str, Any] = {}
            for stage in CANDIDATE_CONSUMER_STAGES:
                main_after[stage] = queue_snapshot(
                    management_port=ports["management"],
                    admin_username=admin_username,
                    admin_password=admin_password,
                    vhost=str(definition["vhost"]),
                    queue=str(runtime_services[stage]["main_queue"]),
                )
                dlq_after[stage] = queue_snapshot(
                    management_port=ports["management"],
                    admin_username=admin_username,
                    admin_password=admin_password,
                    vhost=str(definition["vhost"]),
                    queue=str(runtime_services[stage]["dlq"]),
                )
            report["runtime"]["main_queues_after"] = main_after
            report["runtime"]["dlqs_after"] = dlq_after
            report["runtime"]["publication_replay_drain"] = publication_after
            if publication_after.get("messages") != 0 or publication_after.get("acked", 0) < 1:
                raise RuntimeError("publication replay did not drain and acknowledge on the isolated broker")
            if any(snapshot["consumers"] != 1 or snapshot["messages"] != 0 for snapshot in main_after.values()):
                raise RuntimeError("isolated exact-candidate consumers were not restored with drained queues")
            if any(snapshot["messages"] != 0 for snapshot in dlq_after.values()):
                raise RuntimeError("isolated recovery produced DLQ messages")
            if post_side_effects != pre_side_effects:
                raise RuntimeError("isolated recovery produced a duplicate domain or shadow API side effect")
            if post_audit_count != int(candidate["audit_count"]) + 1:
                raise RuntimeError("authoritative persistence outbox did not record exactly one recovery audit")

        except Exception as exc:
            report["error"] = exc.__class__.__name__
            report["detail"] = str(exc)[-500:]
        finally:
            for container_name in reversed(candidate_containers):
                remove_container(container_name)
            remove_container(broker_container)

    live_broker_after = live_broker_snapshot(args.container_name)
    report["live_broker_after"] = live_broker_after
    report["guardrails"]["live_production_broker_unchanged"] = live_broker_after == live_broker_before
    duration = round(time.monotonic() - started, 3)
    report["finished_at_utc"] = utc_now()
    report["duration_seconds"] = duration
    if (
        "error" not in report
        and live_broker_after == live_broker_before
        and duration <= RECOVERY_TARGETS["current_candidate_reconciliation_drill_rto_seconds"]
    ):
        report["status"] = "healthy"
    write_json(args.state_dir / STATUS_FILES["current_candidate_reconciliation_drill"], report)
    return report


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
            first_ports = start_drill_container(
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
                management_port=first_ports["management"],
                actions=("bootstrap", "check", "permissions"),
            )
            reports.extend(bootstrap_reports)
            stop_result = run(["docker", "stop", first_container], timeout=120)
            reports.append(command_report("docker stop source drill broker", stop_result))
            ok = ok and stop_result.returncode == 0
            if ok:
                shutil.copytree(original_data, restored_data, symlinks=True)
                chown_rabbitmq_data(restored_data)
                second_ports = start_drill_container(
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
                    management_port=second_ports["management"],
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
        "current_candidate_reconciliation_drill": read_json(
            args.state_dir / STATUS_FILES["current_candidate_reconciliation_drill"]
        ),
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
        choices=(
            "status",
            "export-definitions",
            "clean-rebuild-drill",
            "current-candidate-reconciliation-drill",
            "stopped-volume-restore-drill",
            "scheduled-check",
        ),
    )
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--topology-env", type=Path, default=DEFAULT_TOPOLOGY_ENV)
    parser.add_argument("--definition", type=Path, default=DEFAULT_TOPOLOGY_DEFINITION)
    parser.add_argument("--topology-script", type=Path, default=DEFAULT_TOPOLOGY_SCRIPT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--runtime-manifest", type=Path, default=DEFAULT_RUNTIME_MANIFEST)
    parser.add_argument("--runtime-compose", type=Path, default=DEFAULT_RUNTIME_COMPOSE)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    actions = {
        "status": action_status,
        "export-definitions": export_definitions,
        "clean-rebuild-drill": action_clean_rebuild_drill,
        "current-candidate-reconciliation-drill": action_current_candidate_reconciliation_drill,
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
