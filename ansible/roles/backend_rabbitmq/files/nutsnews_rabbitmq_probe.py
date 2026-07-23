#!/usr/bin/env python3
"""RabbitMQ loopback health and durable-message probe.

The script reads root-only RabbitMQ credentials from the managed environment
file. Secret values are never accepted in process arguments or printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:15672"
PROBE_QUEUE_PREFIX = "worker.uplift.probe."
SMOKE_RESOURCE_PREFIX = "worker.uplift.probe.smoke."
DEFAULT_TOPOLOGY_ENV = Path("/etc/nutsnews-rabbitmq/topology.env")
DEFAULT_TOPOLOGY_DEFINITION = Path("/etc/nutsnews-rabbitmq/worker-uplift-topology.json")
DEFAULT_METADATA_PATH = Path("/var/lib/nutsnews/rabbitmq-probes/apply-metadata.json")
DEFAULT_SMOKE_REPORT_PATH = Path("/var/lib/nutsnews/rabbitmq-probes/last-smoke.json")
DEFAULT_TOPOLOGY_PATH = Path("/usr/local/sbin/nutsnews-rabbitmq-topology")
DEFAULT_NETWORK_CHECK_PATH = Path("/usr/local/sbin/nutsnews-rabbitmq-network-check")
DEFAULT_BACKUP_PATH = Path("/usr/local/sbin/nutsnews-backup")
DEFAULT_RABBITMQ_SERVICE = "nutsnews-rabbitmq.service"
DEFAULT_RABBITMQ_CONTAINER = "nutsnews-rabbitmq"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def api_path(*parts: str) -> str:
    return "/" + "/".join(urllib.parse.quote(part, safe="") for part in parts)


def request_json(
    *,
    base_url: str,
    username: str,
    password: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 10,
    ignored_statuses: tuple[int, ...] = (),
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            if not data:
                return None
            return json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace").strip()
        finally:
            exc.close()
        if exc.code in ignored_statuses:
            return None
        if len(error_body) > 500:
            error_body = f"{error_body[:500]}..."
        detail = f"RabbitMQ management API {method} {path} returned HTTP {exc.code}"
        if error_body:
            detail = f"{detail}: {error_body}"
        raise RuntimeError(detail) from exc


def rabbitmq_credentials(env_path: Path) -> tuple[str, str, str]:
    values = parse_env(env_path)
    username = values.get("RABBITMQ_DEFAULT_USER", "")
    password = values.get("RABBITMQ_DEFAULT_PASS", "")
    vhost = values.get("RABBITMQ_DEFAULT_VHOST", "")
    missing = [name for name, value in (("RABBITMQ_DEFAULT_USER", username), ("RABBITMQ_DEFAULT_PASS", password), ("RABBITMQ_DEFAULT_VHOST", vhost)) if not value]
    if missing:
        raise SystemExit(f"missing required RabbitMQ environment names: {', '.join(missing)}")
    return username, password, vhost


def require_probe_queue_name(queue: str) -> None:
    if not queue.startswith(PROBE_QUEUE_PREFIX):
        raise SystemExit(f"refusing to mutate non-probe RabbitMQ queue: {queue}")


def require_probe_resource_name(name: str) -> None:
    if not name.startswith(PROBE_QUEUE_PREFIX):
        raise SystemExit(f"refusing to mutate non-probe RabbitMQ resource: {name}")


def wait_for_management(args: argparse.Namespace, username: str, password: str) -> dict[str, Any]:
    deadline = time.monotonic() + args.timeout_seconds
    last_error = "unknown"
    while time.monotonic() < deadline:
        try:
            overview = request_json(
                base_url=args.management_url,
                username=username,
                password=password,
                method="GET",
                path="/api/overview",
                timeout=5,
            )
            if isinstance(overview, dict):
                return overview
        except Exception as exc:  # pragma: no cover - exercised on host
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"RabbitMQ management API did not become ready: {last_error}")


def declare_probe_queue(args: argparse.Namespace, username: str, password: str, vhost: str) -> None:
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="PUT",
        path=api_path("api", "queues", vhost, args.queue),
        payload={
            "durable": True,
            "auto_delete": False,
            "arguments": {
                "x-queue-type": "classic",
                "x-max-length": 10,
                "x-max-length-bytes": 1048576,
                "x-overflow": "reject-publish",
            },
        },
    )


def delete_probe_queue_if_present(args: argparse.Namespace, username: str, password: str, vhost: str) -> None:
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="DELETE",
        path=api_path("api", "queues", vhost, args.queue),
        ignored_statuses=(404,),
    )


def declare_exchange(args: argparse.Namespace, username: str, password: str, vhost: str, exchange: str) -> None:
    require_probe_resource_name(exchange)
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="PUT",
        path=api_path("api", "exchanges", vhost, exchange),
        payload={"type": "direct", "durable": True, "auto_delete": False, "internal": False, "arguments": {}},
    )


def delete_exchange_if_present(args: argparse.Namespace, username: str, password: str, vhost: str, exchange: str) -> None:
    require_probe_resource_name(exchange)
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="DELETE",
        path=api_path("api", "exchanges", vhost, exchange),
        ignored_statuses=(404,),
    )


def declare_queue(
    args: argparse.Namespace,
    username: str,
    password: str,
    vhost: str,
    queue: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    require_probe_queue_name(queue)
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="PUT",
        path=api_path("api", "queues", vhost, queue),
        payload={
            "durable": True,
            "auto_delete": False,
            "arguments": {
                "x-queue-type": "classic",
                "x-max-length": 10,
                "x-max-length-bytes": 1048576,
                "x-overflow": "reject-publish",
                **(arguments or {}),
            },
        },
    )


def delete_queue_if_present(args: argparse.Namespace, username: str, password: str, vhost: str, queue: str) -> None:
    require_probe_queue_name(queue)
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="DELETE",
        path=api_path("api", "queues", vhost, queue),
        ignored_statuses=(404,),
    )


def bind_queue(args: argparse.Namespace, username: str, password: str, vhost: str, exchange: str, queue: str, routing_key: str) -> None:
    require_probe_resource_name(exchange)
    require_probe_queue_name(queue)
    request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="POST",
        path=api_path("api", "bindings", vhost, "e", exchange, "q", queue),
        payload={"routing_key": routing_key, "arguments": {}},
    )


def publish_message(
    args: argparse.Namespace,
    username: str,
    password: str,
    vhost: str,
    exchange: str,
    routing_key: str,
    message_id: str,
    probe_kind: str,
) -> bool:
    payload = {
        "probe": "nutsnews-rabbitmq-smoke",
        "probe_kind": probe_kind,
        "message_id": message_id,
        "published_at_utc": utc_now(),
    }
    result = request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="POST",
        path=api_path("api", "exchanges", vhost, exchange, "publish"),
        payload={
            "properties": {
                "delivery_mode": 2,
                "message_id": message_id,
                "content_type": "application/json",
            },
            "routing_key": routing_key,
            "payload": json.dumps(payload, sort_keys=True),
            "payload_encoding": "string",
        },
    )
    return isinstance(result, dict) and result.get("routed") is True


def get_message(
    args: argparse.Namespace,
    username: str,
    password: str,
    vhost: str,
    queue: str,
    ackmode: str,
) -> dict[str, Any] | None:
    require_probe_queue_name(queue)
    result = request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="POST",
        path=api_path("api", "queues", vhost, queue, "get"),
        payload={
            "count": 1,
            "ackmode": ackmode,
            "encoding": "auto",
            "truncate": 50000,
        },
    )
    if not isinstance(result, list) or not result:
        return None
    return json.loads(result[0].get("payload") or "{}")


def wait_for_message(
    args: argparse.Namespace,
    username: str,
    password: str,
    vhost: str,
    queue: str,
    expected_message_id: str,
    ackmode: str = "ack_requeue_false",
) -> bool:
    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        payload = get_message(args, username, password, vhost, queue, ackmode)
        if payload and payload.get("message_id") == expected_message_id:
            return True
        time.sleep(0.5)
    return False


def add_check(report: dict[str, Any], name: str, status: str, summary: str, **details: Any) -> None:
    check = {"name": name, "status": status, "summary": summary}
    check.update(details)
    report["checks"].append(check)
    if status not in {"healthy", "pass", "expected"}:
        report["status"] = "fail"


def completed_process(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"timed out after {timeout}s",
        }
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-2000:],
    }


def parse_json_output(output: str) -> dict[str, Any] | None:
    output = output.strip()
    if not output.startswith("{"):
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def action_health(args: argparse.Namespace) -> int:
    username, password, _ = rabbitmq_credentials(args.env)
    overview = wait_for_management(args, username, password)
    print(
        json.dumps(
            {
                "status": "healthy",
                "rabbitmq_version": overview.get("rabbitmq_version"),
                "erlang_version": overview.get("erlang_version"),
                "management_version": overview.get("management_version"),
            },
            sort_keys=True,
        )
    )
    return 0


def action_smoke(args: argparse.Namespace) -> int:
    username, password, vhost = rabbitmq_credentials(args.env)
    topology_env = parse_env(args.credentials_env) if args.credentials_env.exists() else {}
    wait_for_management(args, username, password)

    suffix = uuid.uuid4().hex
    prefix = f"{SMOKE_RESOURCE_PREFIX}{suffix}"
    main_exchange = f"{prefix}.main"
    retry_exchange = f"{prefix}.retry"
    dlq_exchange = f"{prefix}.dlq"
    main_queue = f"{prefix}.main"
    retry_queue = f"{prefix}.retry"
    dlq_queue = f"{prefix}.dlq"
    restart_queue = f"{prefix}.restart"
    queues = [main_queue, retry_queue, dlq_queue, restart_queue]
    exchanges = [main_exchange, retry_exchange, dlq_exchange]

    report: dict[str, Any] = {
        "schema_version": 1,
        "action": "smoke",
        "status": "pass",
        "started_at_utc": utc_now(),
        "resource_prefix": prefix,
        "checks": [],
        "cleanup_errors": [],
        "secret_redaction": "only probe resource names and generated message ids are reported; credentials and message contents are not emitted",
    }

    try:
        for exchange in exchanges:
            declare_exchange(args, username, password, vhost, exchange)
        declare_queue(args, username, password, vhost, main_queue, {"x-dead-letter-exchange": dlq_exchange, "x-dead-letter-routing-key": "dlq"})
        declare_queue(args, username, password, vhost, retry_queue, {"x-message-ttl": args.retry_ttl_ms, "x-dead-letter-exchange": main_exchange, "x-dead-letter-routing-key": "main"})
        declare_queue(args, username, password, vhost, dlq_queue)
        declare_queue(args, username, password, vhost, restart_queue)
        bind_queue(args, username, password, vhost, main_exchange, main_queue, "main")
        bind_queue(args, username, password, vhost, retry_exchange, retry_queue, "retry")
        bind_queue(args, username, password, vhost, dlq_exchange, dlq_queue, "dlq")

        publish_id = str(uuid.uuid4())
        routed = publish_message(args, username, password, vhost, main_exchange, "main", publish_id, "publish_confirm")
        add_check(report, "publish_confirm", "healthy" if routed else "critical", "management publish route confirmation", message_id=publish_id)

        requeue_payload = get_message(args, username, password, vhost, main_queue, "ack_requeue_true")
        manual_ack_id = str(requeue_payload.get("message_id") if requeue_payload else "")
        manual_ack_ok = manual_ack_id == publish_id and wait_for_message(args, username, password, vhost, main_queue, publish_id)
        add_check(report, "consume_manual_ack", "healthy" if manual_ack_ok else "critical", "message was visible, requeued, then acknowledged", message_id=publish_id)

        retry_id = str(uuid.uuid4())
        retry_routed = publish_message(args, username, password, vhost, retry_exchange, "retry", retry_id, "retry")
        retry_ok = retry_routed and wait_for_message(args, username, password, vhost, main_queue, retry_id)
        add_check(report, "retry", "healthy" if retry_ok else "critical", "retry queue dead-lettered back to the main exchange", message_id=retry_id)

        dlq_id = str(uuid.uuid4())
        dlq_routed = publish_message(args, username, password, vhost, main_exchange, "main", dlq_id, "dlq")
        rejected = get_message(args, username, password, vhost, main_queue, "reject_requeue_false")
        dlq_ok = dlq_routed and bool(rejected and rejected.get("message_id") == dlq_id) and wait_for_message(args, username, password, vhost, dlq_queue, dlq_id)
        add_check(report, "dlq", "healthy" if dlq_ok else "critical", "negative acknowledgement routed message to DLQ", message_id=dlq_id)

        restart_id = str(uuid.uuid4())
        restart_routed = publish_message(args, username, password, vhost, "amq.default", restart_queue, restart_id, "restart_persistence")
        restart_rc = 0
        if restart_routed and not args.skip_restart:
            restart_result = completed_process(["systemctl", "restart", args.restart_service], args.restart_timeout_seconds)
            restart_rc = int(restart_result["returncode"])
            wait_for_management(args, username, password)
        restart_ok = restart_routed and restart_rc == 0 and wait_for_message(args, username, password, vhost, restart_queue, restart_id)
        add_check(
            report,
            "restart_persistence",
            "healthy" if restart_ok else "critical",
            "durable probe message survived fixed service restart" if not args.skip_restart else "durable probe message verified without service restart",
            message_id=restart_id,
            restart_performed=not args.skip_restart,
        )

        monitor_username = topology_env.get("RABBITMQ_MONITORING_USERNAME", "")
        monitor_password = topology_env.get("RABBITMQ_MONITORING_PASSWORD", "")
        if monitor_username and monitor_password:
            denied = request_json(
                base_url=args.management_url,
                username=monitor_username,
                password=monitor_password,
                method="POST",
                path=api_path("api", "exchanges", vhost, main_exchange, "publish"),
                payload={
                    "properties": {"delivery_mode": 2},
                    "routing_key": "main",
                    "payload": "{}",
                    "payload_encoding": "string",
                },
                ignored_statuses=(401, 403),
            )
            add_check(report, "permission_denial", "healthy" if denied is None else "critical", "monitoring identity cannot publish to probe exchange")
        else:
            add_check(report, "permission_denial", "critical", "monitoring credentials were not available by name")
    except Exception as exc:
        report["status"] = "fail"
        report["checks"].append({"name": "smoke_exception", "status": "critical", "summary": str(exc)})
    finally:
        for queue in queues:
            try:
                delete_queue_if_present(args, username, password, vhost, queue)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                report["cleanup_errors"].append(f"queue:{queue}:{exc}")
        for exchange in exchanges:
            try:
                delete_exchange_if_present(args, username, password, vhost, exchange)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                report["cleanup_errors"].append(f"exchange:{exchange}:{exc}")

    report["finished_at_utc"] = utc_now()
    if report["cleanup_errors"]:
        report["status"] = "fail"
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output_path = args.output or DEFAULT_SMOKE_REPORT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    output_path.chmod(0o640)
    print(output, end="")
    return 0 if report["status"] == "pass" else 1


def add_drift_check(report: dict[str, Any], surface: str, status: str, severity: str, expected: Any, observed: Any, details: Any = None) -> None:
    check = {
        "surface": surface,
        "status": status,
        "severity": severity,
        "expected": expected,
        "observed": observed,
    }
    if details is not None:
        check["details"] = details
    report["checks"].append(check)


def run_json_check(report: dict[str, Any], surface: str, command: list[str], timeout: int) -> dict[str, Any] | None:
    result = completed_process(command, timeout)
    data = parse_json_output(str(result["stdout"]))
    status = "expected" if result["returncode"] == 0 and data and data.get("status") in {"pass", "healthy"} else "unexpected"
    observed = data if data is not None else {"returncode": result["returncode"], "stdout_tail": result["stdout"], "stderr_tail": result["stderr"]}
    add_drift_check(report, surface, status, "high", "pass", observed)
    return data


def inspect_expected_rabbitmq_image(container_name: str, expected_image: str, timeout: int) -> tuple[bool, dict[str, Any]]:
    observed: dict[str, Any] = {
        "container_image": "",
        "container_image_id": "",
        "image_repo_digests": [],
        "match_source": "",
    }
    container_result = completed_process(["docker", "inspect", container_name, "--format", "{{.Config.Image}} {{.Image}}"], timeout)
    observed["container_inspect_returncode"] = container_result["returncode"]
    if container_result["returncode"] != 0:
        observed["container_inspect_stderr_tail"] = str(container_result["stderr"])[-500:]
        return False, observed

    parts = str(container_result["stdout"]).strip().split()
    container_image = parts[0] if parts else ""
    container_image_id = parts[1] if len(parts) > 1 else ""
    observed["container_image"] = container_image
    observed["container_image_id"] = container_image_id
    if container_image == expected_image:
        observed["match_source"] = "container_config_image"
        return True, observed

    if not container_image_id:
        return False, observed

    image_result = completed_process(["docker", "image", "inspect", container_image_id, "--format", "{{range .RepoDigests}}{{.}} {{end}}"], timeout)
    observed["image_inspect_returncode"] = image_result["returncode"]
    if image_result["returncode"] != 0:
        observed["image_inspect_stderr_tail"] = str(image_result["stderr"])[-500:]
        return False, observed

    repo_digests = str(image_result["stdout"]).strip().split()
    observed["image_repo_digests"] = repo_digests
    if expected_image in repo_digests:
        observed["match_source"] = "image_repo_digest"
        return True, observed
    return False, observed


def action_drift(args: argparse.Namespace) -> int:
    username, password, _ = rabbitmq_credentials(args.env)
    report: dict[str, Any] = {
        "schema_version": 1,
        "action": "drift",
        "checked_at_utc": utc_now(),
        "status": "pass",
        "checks": [],
        "secret_redaction": "fixed read-only commands only; credentials and message contents are not emitted",
    }

    metadata = load_json_file(args.metadata)
    if metadata is None:
        add_drift_check(report, "rabbitmq_apply_metadata", "missing", "high", str(args.metadata), "missing_or_invalid")
    else:
        add_drift_check(report, "rabbitmq_apply_metadata", "expected", "info", str(args.metadata), "present")
        expected_image = str(metadata.get("image") or "")
        if expected_image:
            image_ok, image_observed = inspect_expected_rabbitmq_image(args.container_name, expected_image, args.timeout_seconds)
            add_drift_check(report, "rabbitmq_image_digest", "expected" if image_ok else "unexpected", "high", expected_image, image_observed)
        paths = metadata.get("paths", {})
        checksums = metadata.get("checksums", {})
        if isinstance(paths, dict) and isinstance(checksums, dict):
            for key, path_text in sorted(paths.items()):
                expected_checksum = str(checksums.get(key) or "")
                if not expected_checksum:
                    continue
                observed_checksum = sha256_file(Path(str(path_text)))
                status = "expected" if observed_checksum == expected_checksum else "unexpected"
                add_drift_check(report, f"rabbitmq_config_checksum:{key}", status, "high", expected_checksum, observed_checksum or "missing")

    try:
        overview = wait_for_management(args, username, password)
        add_drift_check(report, "rabbitmq_health", "expected", "high", "management reachable", {"rabbitmq_version": overview.get("rabbitmq_version")})
    except SystemExit as exc:
        add_drift_check(report, "rabbitmq_health", "unexpected", "high", "management reachable", str(exc))

    run_json_check(
        report,
        "rabbitmq_topology",
        [str(args.topology_path), "check", "--env", str(args.env), "--credentials-env", str(args.credentials_env), "--definition", str(args.definition)],
        args.timeout_seconds,
    )
    run_json_check(
        report,
        "rabbitmq_permissions_metadata",
        [str(args.topology_path), "permissions", "--env", str(args.env), "--credentials-env", str(args.credentials_env), "--definition", str(args.definition)],
        args.timeout_seconds,
    )
    run_json_check(
        report,
        "rabbitmq_listeners_network",
        [str(args.network_check_path), "--container-name", args.container_name, "--env", str(args.env), "--topology-env", str(args.credentials_env)],
        args.timeout_seconds,
    )

    backup_result = completed_process([str(args.backup_path), "status"], args.timeout_seconds)
    backup_data = parse_json_output(str(backup_result["stdout"]))
    if backup_result["returncode"] != 0 or backup_data is None:
        add_drift_check(report, "rabbitmq_backup_freshness", "unknown", "medium", "backup status readable", backup_result)
    else:
        backup_status = str((backup_data.get("backup") or {}).get("status") or "unknown")
        recovery = backup_data.get("rabbitmq_recovery") if isinstance(backup_data.get("rabbitmq_recovery"), dict) else {}
        definition_status = str((recovery.get("definition_export") or {}).get("status") or "unknown") if isinstance(recovery, dict) else "unknown"
        clean_status = str((recovery.get("clean_rebuild_drill") or {}).get("status") or "unknown") if isinstance(recovery, dict) else "unknown"
        ok = backup_status == "healthy" and definition_status == "healthy" and clean_status == "healthy"
        add_drift_check(
            report,
            "rabbitmq_backup_freshness",
            "expected" if ok else "missing",
            "medium",
            "backup healthy plus fresh RabbitMQ definition export and clean rebuild drill",
            {"backup": backup_status, "definition_export": definition_status, "clean_rebuild_drill": clean_status},
        )

    high_priority_unexpected = [
        check["surface"]
        for check in report["checks"]
        if check["severity"] == "high" and check["status"] not in {"expected"}
    ]
    report["summary"] = {
        "total": len(report["checks"]),
        "expected": sum(1 for check in report["checks"] if check["status"] == "expected"),
        "missing": sum(1 for check in report["checks"] if check["status"] == "missing"),
        "unexpected": sum(1 for check in report["checks"] if check["status"] == "unexpected"),
        "unknown": sum(1 for check in report["checks"] if check["status"] == "unknown"),
        "high_priority_unexpected": high_priority_unexpected,
    }
    report["status"] = "fail" if high_priority_unexpected else "pass"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


def action_publish(args: argparse.Namespace) -> int:
    require_probe_queue_name(args.queue)
    username, password, vhost = rabbitmq_credentials(args.env)
    wait_for_management(args, username, password)
    delete_probe_queue_if_present(args, username, password, vhost)
    declare_probe_queue(args, username, password, vhost)
    message_id = str(uuid.uuid4())
    payload = {
        "probe": "nutsnews-rabbitmq-durable-restart",
        "message_id": message_id,
        "published_at_utc": utc_now(),
    }
    result = request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="POST",
        path=api_path("api", "exchanges", vhost, "amq.default", "publish"),
        payload={
            "properties": {
                "delivery_mode": 2,
                "message_id": message_id,
                "content_type": "application/json",
            },
            "routing_key": args.queue,
            "payload": json.dumps(payload, sort_keys=True),
            "payload_encoding": "string",
        },
    )
    if not isinstance(result, dict) or result.get("routed") is not True:
        raise SystemExit("durable probe publish was not routed")
    state = {
        "queue": args.queue,
        "message_id": message_id,
        "payload": payload,
        "published_at_utc": payload["published_at_utc"],
    }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.state.chmod(0o640)
    print(json.dumps({"status": "published", "queue": args.queue, "message_id": message_id}, sort_keys=True))
    return 0


def action_verify(args: argparse.Namespace) -> int:
    require_probe_queue_name(args.queue)
    username, password, vhost = rabbitmq_credentials(args.env)
    wait_for_management(args, username, password)
    state = json.loads(args.state.read_text(encoding="utf-8"))
    result = request_json(
        base_url=args.management_url,
        username=username,
        password=password,
        method="POST",
        path=api_path("api", "queues", vhost, args.queue, "get"),
        payload={
            "count": 1,
            "ackmode": "ack_requeue_false",
            "encoding": "auto",
            "truncate": 50000,
        },
    )
    if not isinstance(result, list) or not result:
        raise SystemExit("durable probe message was not present after restart")
    payload = json.loads(result[0].get("payload") or "{}")
    if payload.get("message_id") != state.get("message_id"):
        raise SystemExit("durable probe message id mismatch")
    if args.delete_queue:
        request_json(
            base_url=args.management_url,
            username=username,
            password=password,
            method="DELETE",
            path=api_path("api", "queues", vhost, args.queue),
            ignored_statuses=(404,),
        )
    print(json.dumps({"status": "verified", "queue": args.queue, "message_id": state.get("message_id")}, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("health", "publish", "verify", "smoke", "drift"))
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_TOPOLOGY_ENV)
    parser.add_argument("--definition", type=Path, default=DEFAULT_TOPOLOGY_DEFINITION)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--topology-path", type=Path, default=DEFAULT_TOPOLOGY_PATH)
    parser.add_argument("--network-check-path", type=Path, default=DEFAULT_NETWORK_CHECK_PATH)
    parser.add_argument("--backup-path", type=Path, default=DEFAULT_BACKUP_PATH)
    parser.add_argument("--container-name", default=DEFAULT_RABBITMQ_CONTAINER)
    parser.add_argument("--restart-service", default=DEFAULT_RABBITMQ_SERVICE)
    parser.add_argument("--queue", default="worker.uplift.probe.durable")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/nutsnews/rabbitmq/durable-probe.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--management-url", default=DEFAULT_MANAGEMENT_URL)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--restart-timeout-seconds", type=int, default=180)
    parser.add_argument("--retry-ttl-ms", type=int, default=1000)
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--delete-queue", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        if args.action == "health":
            return action_health(args)
        if args.action == "smoke":
            return action_smoke(args)
        if args.action == "drift":
            return action_drift(args)
        if args.action == "publish":
            return action_publish(args)
        return action_verify(args)
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
