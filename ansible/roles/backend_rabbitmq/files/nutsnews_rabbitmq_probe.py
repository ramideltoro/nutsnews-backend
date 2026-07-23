#!/usr/bin/env python3
"""RabbitMQ loopback health and durable-message probe.

The script reads root-only RabbitMQ credentials from the managed environment
file. Secret values are never accepted in process arguments or printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:15672"


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
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        if not data:
            return None
        return json.loads(data.decode("utf-8"))


def rabbitmq_credentials(env_path: Path) -> tuple[str, str, str]:
    values = parse_env(env_path)
    username = values.get("RABBITMQ_DEFAULT_USER", "")
    password = values.get("RABBITMQ_DEFAULT_PASS", "")
    vhost = values.get("RABBITMQ_DEFAULT_VHOST", "")
    missing = [name for name, value in (("RABBITMQ_DEFAULT_USER", username), ("RABBITMQ_DEFAULT_PASS", password), ("RABBITMQ_DEFAULT_VHOST", vhost)) if not value]
    if missing:
        raise SystemExit(f"missing required RabbitMQ environment names: {', '.join(missing)}")
    return username, password, vhost


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


def action_publish(args: argparse.Namespace) -> int:
    username, password, vhost = rabbitmq_credentials(args.env)
    wait_for_management(args, username, password)
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
        )
    print(json.dumps({"status": "verified", "queue": args.queue, "message_id": state.get("message_id")}, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("health", "publish", "verify"))
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--queue", default="worker.uplift.probe.durable")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/nutsnews/rabbitmq/durable-probe.json"))
    parser.add_argument("--management-url", default=DEFAULT_MANAGEMENT_URL)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--delete-queue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "health":
        return action_health(args)
    if args.action == "publish":
        return action_publish(args)
    return action_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
