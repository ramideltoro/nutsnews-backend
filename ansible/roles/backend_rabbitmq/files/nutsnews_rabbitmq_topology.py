#!/usr/bin/env python3
"""Bootstrap and verify the worker-uplift RabbitMQ topology.

The script uses the loopback RabbitMQ management API and reads credentials from
root-only env files. Secret values are never accepted in process arguments or
printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:15672"
RETRY_WRITE_EXCHANGE_IDS = ("retry", "dlq")


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


class RabbitMQClient:
    def __init__(self, *, base_url: str, username: str, password: str, timeout: int = 10) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        ignored_statuses: tuple[int, ...] = (),
    ) -> Any:
        return request_json(
            base_url=self.base_url,
            username=self.username,
            password=self.password,
            method=method,
            path=path,
            payload=payload,
            timeout=self.timeout,
            ignored_statuses=ignored_statuses,
        )

    def get_or_none(self, path: str) -> Any:
        return self.request("GET", path, ignored_statuses=(404,))


def load_definition(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ("tracking_issue", "vhost", "exchanges", "routes", "users", "queue_limits")
    missing = [key for key in required if key not in data]
    if missing:
        raise SystemExit(f"topology definition missing required keys: {', '.join(missing)}")
    if data.get("tracking_issue") != 81:
        raise SystemExit("topology definition tracking_issue must be 81")
    return data


def admin_credentials(env_path: Path) -> tuple[str, str, str]:
    values = parse_env(env_path)
    username = values.get("RABBITMQ_DEFAULT_USER", "")
    password = values.get("RABBITMQ_DEFAULT_PASS", "")
    vhost = values.get("RABBITMQ_DEFAULT_VHOST", "")
    missing = [
        name
        for name, value in (
            ("RABBITMQ_DEFAULT_USER", username),
            ("RABBITMQ_DEFAULT_PASS", password),
            ("RABBITMQ_DEFAULT_VHOST", vhost),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"missing required RabbitMQ environment names: {', '.join(missing)}")
    return username, password, vhost


def load_secret_environment(path: Path) -> dict[str, str]:
    values = parse_env(path)
    missing = [name for name in managed_environment_names() if not values.get(name)]
    if missing:
        raise SystemExit(f"missing RabbitMQ topology credential names: {', '.join(missing)}")
    if any("\n" in value or "\r" in value for value in values.values()):
        raise SystemExit("RabbitMQ topology credential values must be single-line values")
    return values


def managed_environment_names() -> tuple[str, ...]:
    return (
        "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME",
        "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
        "RABBITMQ_MONITORING_USERNAME",
        "RABBITMQ_MONITORING_PASSWORD",
        "RABBITMQ_SCHEDULER_PUBLISHER_USERNAME",
        "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD",
        "RABBITMQ_FETCHER_CONSUMER_USERNAME",
        "RABBITMQ_FETCHER_CONSUMER_PASSWORD",
        "RABBITMQ_FETCHER_PUBLISHER_USERNAME",
        "RABBITMQ_FETCHER_PUBLISHER_PASSWORD",
        "RABBITMQ_CANONICALIZER_CONSUMER_USERNAME",
        "RABBITMQ_CANONICALIZER_CONSUMER_PASSWORD",
        "RABBITMQ_CANONICALIZER_PUBLISHER_USERNAME",
        "RABBITMQ_CANONICALIZER_PUBLISHER_PASSWORD",
        "RABBITMQ_ENRICHMENT_CONSUMER_USERNAME",
        "RABBITMQ_ENRICHMENT_CONSUMER_PASSWORD",
        "RABBITMQ_ENRICHMENT_PUBLISHER_USERNAME",
        "RABBITMQ_ENRICHMENT_PUBLISHER_PASSWORD",
        "RABBITMQ_APPROVAL_CONSUMER_USERNAME",
        "RABBITMQ_APPROVAL_CONSUMER_PASSWORD",
        "RABBITMQ_APPROVAL_PUBLISHER_USERNAME",
        "RABBITMQ_APPROVAL_PUBLISHER_PASSWORD",
        "RABBITMQ_TRANSLATION_CONSUMER_USERNAME",
        "RABBITMQ_TRANSLATION_CONSUMER_PASSWORD",
        "RABBITMQ_TRANSLATION_PUBLISHER_USERNAME",
        "RABBITMQ_TRANSLATION_PUBLISHER_PASSWORD",
        "RABBITMQ_PERSISTENCE_CONSUMER_USERNAME",
        "RABBITMQ_PERSISTENCE_CONSUMER_PASSWORD",
        "RABBITMQ_PERSISTENCE_PUBLISHER_USERNAME",
        "RABBITMQ_PERSISTENCE_PUBLISHER_PASSWORD",
        "RABBITMQ_PUBLICATION_CONSUMER_USERNAME",
        "RABBITMQ_PUBLICATION_CONSUMER_PASSWORD",
    )


def exchange_by_id(definition: dict[str, Any], exchange_id: str) -> dict[str, Any]:
    exchanges = {exchange["id"]: exchange for exchange in definition["exchanges"]}
    try:
        return exchanges[exchange_id]
    except KeyError:
        raise SystemExit(f"topology definition missing exchange id: {exchange_id}") from None


def regex_literal(value: str) -> str:
    return re.escape(value)


def queue_arguments(definition: dict[str, Any], route: dict[str, Any], queue: dict[str, Any], kind: str) -> dict[str, Any]:
    queue_type = definition["queue_type"]
    limits = definition["queue_limits"][kind]
    args: dict[str, Any] = {
        "x-queue-type": queue_type,
        "x-max-length": int(limits["x-max-length"]),
        "x-max-length-bytes": int(limits["x-max-length-bytes"]),
        "x-overflow": limits["x-overflow"],
    }
    if kind == "main":
        args["x-dead-letter-exchange"] = exchange_by_id(definition, "dlq")["name"]
        args["x-dead-letter-routing-key"] = route["terminal_dlq"]["routing_key"]
    elif kind == "retry":
        args["x-message-ttl"] = int(queue["ttl_ms"])
        args["x-dead-letter-exchange"] = exchange_by_id(definition, "main")["name"]
        args["x-dead-letter-routing-key"] = route["routing_key"]
    elif kind == "dlq" and "x-message-ttl" in limits:
        args["x-message-ttl"] = int(limits["x-message-ttl"])
    return args


def expected_queues(definition: dict[str, Any]) -> list[dict[str, Any]]:
    queues: list[dict[str, Any]] = []
    for route in definition["routes"]:
        queues.append(
            {
                "kind": "main",
                "stage": route["stage"],
                "name": route["main_queue"],
                "durable": True,
                "auto_delete": False,
                "arguments": queue_arguments(definition, route, {"name": route["main_queue"]}, "main"),
            }
        )
        for retry_queue in route["retry_queues"]:
            queues.append(
                {
                    "kind": "retry",
                    "stage": route["stage"],
                    "name": retry_queue["name"],
                    "durable": True,
                    "auto_delete": False,
                    "arguments": queue_arguments(definition, route, retry_queue, "retry"),
                }
            )
        queues.append(
            {
                "kind": "dlq",
                "stage": route["stage"],
                "name": route["terminal_dlq"]["name"],
                "durable": True,
                "auto_delete": False,
                "arguments": queue_arguments(definition, route, route["terminal_dlq"], "dlq"),
            }
        )
    canary = definition.get("canary")
    if isinstance(canary, dict) and isinstance(canary.get("queue"), dict):
        queue = canary["queue"]
        queues.append(
            {
                "kind": "canary",
                "stage": "canary",
                "name": queue["name"],
                "durable": bool(queue.get("durable", True)),
                "auto_delete": bool(queue.get("auto_delete", False)),
                "arguments": normalize_dict(queue.get("arguments", {})),
            }
        )
    return queues


def expected_bindings(definition: dict[str, Any]) -> list[dict[str, Any]]:
    main_exchange = exchange_by_id(definition, "main")["name"]
    retry_exchange = exchange_by_id(definition, "retry")["name"]
    dlq_exchange = exchange_by_id(definition, "dlq")["name"]
    bindings: list[dict[str, Any]] = []
    for route in definition["routes"]:
        bindings.append(
            {
                "exchange": main_exchange,
                "queue": route["main_queue"],
                "routing_key": route["routing_key"],
                "arguments": {},
            }
        )
        for retry_queue in route["retry_queues"]:
            bindings.append(
                {
                    "exchange": retry_exchange,
                    "queue": retry_queue["name"],
                    "routing_key": retry_queue["routing_key"],
                    "arguments": {},
                }
            )
        bindings.append(
            {
                "exchange": dlq_exchange,
                "queue": route["terminal_dlq"]["name"],
                "routing_key": route["terminal_dlq"]["routing_key"],
                "arguments": {},
            }
        )
    canary = definition.get("canary")
    if isinstance(canary, dict) and isinstance(canary.get("queue"), dict):
        bindings.append(
            {
                "exchange": exchange_by_id(definition, canary["exchange_id"])["name"],
                "queue": canary["queue"]["name"],
                "routing_key": canary["routing_key"],
                "arguments": {},
            }
        )
    return bindings


def expected_policies(definition: dict[str, Any]) -> list[dict[str, Any]]:
    stages = "|".join(regex_literal(route["stage"]) for route in definition["routes"])
    namespace = regex_literal(definition["namespace"])
    main_limits = definition["queue_limits"]["main"]
    retry_limits = definition["queue_limits"]["retry"]
    dlq_limits = definition["queue_limits"]["dlq"]
    policies: list[dict[str, Any]] = [
        {
            "name": "nutsnews-worker-v1-main-bounds",
            "pattern": f"^{namespace}\\.({stages})\\.v1$",
            "apply-to": "queues",
            "priority": 80,
            "definition": {
                "max-length": int(main_limits["x-max-length"]),
                "max-length-bytes": int(main_limits["x-max-length-bytes"]),
                "overflow": main_limits["x-overflow"],
                "dead-letter-exchange": exchange_by_id(definition, "dlq")["name"],
            },
        },
        {
            "name": "nutsnews-worker-v1-retry-bounds",
            "pattern": f"^{namespace}\\..*\\.v1\\.retry-(30s|5m|30m)$",
            "apply-to": "queues",
            "priority": 80,
            "definition": {
                "max-length": int(retry_limits["x-max-length"]),
                "max-length-bytes": int(retry_limits["x-max-length-bytes"]),
                "overflow": retry_limits["x-overflow"],
                "dead-letter-exchange": exchange_by_id(definition, "main")["name"],
            },
        },
        {
            "name": "nutsnews-worker-v1-dlq-retention",
            "pattern": f"^{namespace}\\..*\\.v1\\.dlq$",
            "apply-to": "queues",
            "priority": 80,
            "definition": {
                "max-length": int(dlq_limits["x-max-length"]),
                "max-length-bytes": int(dlq_limits["x-max-length-bytes"]),
                "overflow": dlq_limits["x-overflow"],
                "message-ttl": int(dlq_limits["x-message-ttl"]),
            },
        },
    ]
    return policies


def user_records(definition: dict[str, Any], env_values: dict[str, str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_usernames: set[str] = set()
    for user in definition["users"]:
        user_id = user["id"]
        if user_id in seen_ids:
            raise SystemExit(f"duplicate managed RabbitMQ user id: {user_id}")
        seen_ids.add(user_id)
        username = env_values.get(user["username_variable"], "")
        password = env_values.get(user["password_variable"], "")
        if not username or not password:
            raise SystemExit(f"missing username or password value for managed RabbitMQ user id: {user_id}")
        if username == "guest":
            raise SystemExit(f"managed RabbitMQ user id {user_id} must not use guest")
        if username in seen_usernames:
            raise SystemExit(f"duplicate managed RabbitMQ username value for user id: {user_id}")
        seen_usernames.add(username)
        records.append({**user, "username": username, "password": password})
    return records


def wait_for_management(args: argparse.Namespace, client: RabbitMQClient) -> dict[str, Any]:
    deadline = time.monotonic() + args.timeout_seconds
    last_error = "unknown"
    while time.monotonic() < deadline:
        try:
            overview = client.request("GET", "/api/overview")
            if isinstance(overview, dict):
                return overview
        except Exception as exc:  # pragma: no cover - exercised on host
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"RabbitMQ management API did not become ready: {last_error}")


def normalize_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): item for key, item in sorted(value.items())}


def normalize_tags(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return sorted(tag.strip() for tag in value.split(",") if tag.strip())
    if isinstance(value, list):
        return sorted(str(tag).strip() for tag in value if str(tag).strip())
    return [str(value).strip()] if str(value).strip() else []


def ensure_vhost(client: RabbitMQClient, definition: dict[str, Any], report: dict[str, Any]) -> None:
    vhost = definition["vhost"]
    if client.get_or_none(api_path("api", "vhosts", vhost)) is None:
        client.request("PUT", api_path("api", "vhosts", vhost), payload={})
        report["changed"] = True
        report["changes"].append("created_vhost")


def ensure_user(client: RabbitMQClient, user: dict[str, Any], report: dict[str, Any], *, rotate_passwords: bool) -> None:
    current = client.get_or_none(api_path("api", "users", user["username"]))
    expected_tags = ",".join(user.get("tags", []))
    current_tags: list[str] = []
    if isinstance(current, dict):
        current_tags = normalize_tags(current.get("tags", ""))
    if current is None or current_tags != normalize_tags(user.get("tags", [])) or rotate_passwords:
        client.request(
            "PUT",
            api_path("api", "users", user["username"]),
            payload={"password": user["password"], "tags": expected_tags},
        )
        report["changed"] = True
        report["changes"].append(f"upserted_user:{user['id']}")


def ensure_permissions(
    client: RabbitMQClient,
    definition: dict[str, Any],
    user: dict[str, Any],
    report: dict[str, Any],
) -> None:
    vhost = definition["vhost"]
    expected = user["permissions"]
    current = client.get_or_none(api_path("api", "permissions", vhost, user["username"]))
    current_permissions = {}
    if isinstance(current, dict):
        current_permissions = {
            "configure": current.get("configure", ""),
            "write": current.get("write", ""),
            "read": current.get("read", ""),
        }
    if current_permissions != expected:
        client.request("PUT", api_path("api", "permissions", vhost, user["username"]), payload=expected)
        report["changed"] = True
        report["changes"].append(f"updated_permissions:{user['id']}")

    all_permissions = client.request("GET", api_path("api", "users", user["username"], "permissions"))
    if isinstance(all_permissions, list):
        for permission in all_permissions:
            other_vhost = permission.get("vhost")
            if other_vhost and other_vhost != vhost:
                client.request(
                    "DELETE",
                    api_path("api", "permissions", str(other_vhost), user["username"]),
                    ignored_statuses=(404,),
                )
                report["changed"] = True
                report["changes"].append(f"removed_other_vhost_permission:{user['id']}")


def ensure_guest_deleted(client: RabbitMQClient, report: dict[str, Any]) -> None:
    if client.get_or_none(api_path("api", "users", "guest")) is not None:
        client.request("DELETE", api_path("api", "users", "guest"), ignored_statuses=(404,))
        report["changed"] = True
        report["changes"].append("deleted_guest_user")


def ensure_exchange(client: RabbitMQClient, definition: dict[str, Any], exchange: dict[str, Any], report: dict[str, Any]) -> None:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "exchanges", vhost, exchange["name"]))
    expected_payload = {
        "type": exchange["type"],
        "durable": bool(exchange["durable"]),
        "auto_delete": False,
        "internal": False,
        "arguments": normalize_dict(exchange.get("arguments", {})),
    }
    if current is None:
        client.request("PUT", api_path("api", "exchanges", vhost, exchange["name"]), payload=expected_payload)
        report["changed"] = True
        report["changes"].append(f"created_exchange:{exchange['id']}")
        return
    drift = []
    for key in ("type", "durable", "auto_delete", "internal"):
        if current.get(key) != expected_payload[key]:
            drift.append(f"{key} expected {expected_payload[key]!r} observed {current.get(key)!r}")
    if normalize_dict(current.get("arguments", {})) != expected_payload["arguments"]:
        drift.append("arguments differ")
    if drift:
        report["drift"].append(f"exchange:{exchange['id']}:{'; '.join(drift)}")


def ensure_queue(client: RabbitMQClient, definition: dict[str, Any], queue: dict[str, Any], report: dict[str, Any]) -> None:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "queues", vhost, queue["name"]))
    expected_payload = {
        "durable": True,
        "auto_delete": False,
        "arguments": normalize_dict(queue["arguments"]),
    }
    if current is None:
        client.request("PUT", api_path("api", "queues", vhost, queue["name"]), payload=expected_payload)
        report["changed"] = True
        report["changes"].append(f"created_queue:{queue['kind']}:{queue['stage']}")
        return
    drift = []
    for key in ("durable", "auto_delete"):
        if current.get(key) != expected_payload[key]:
            drift.append(f"{key} expected {expected_payload[key]!r} observed {current.get(key)!r}")
    if normalize_dict(current.get("arguments", {})) != expected_payload["arguments"]:
        drift.append("arguments differ")
    if drift:
        report["drift"].append(f"queue:{queue['name']}:{'; '.join(drift)}")


def ensure_binding(client: RabbitMQClient, definition: dict[str, Any], binding: dict[str, Any], report: dict[str, Any]) -> None:
    vhost = definition["vhost"]
    path = api_path("api", "bindings", vhost, "e", binding["exchange"], "q", binding["queue"])
    current = client.request("GET", path)
    binding_exists = False
    if isinstance(current, list):
        for candidate in current:
            if candidate.get("routing_key") == binding["routing_key"] and normalize_dict(candidate.get("arguments", {})) == normalize_dict(binding.get("arguments", {})):
                binding_exists = True
                break
    if not binding_exists:
        client.request(
            "POST",
            path,
            payload={"routing_key": binding["routing_key"], "arguments": normalize_dict(binding.get("arguments", {}))},
        )
        report["changed"] = True
        report["changes"].append(f"created_binding:{binding['queue']}")


def canary_queue_and_binding(definition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    canary_queues = [queue for queue in expected_queues(definition) if queue.get("kind") == "canary"]
    if len(canary_queues) != 1:
        raise SystemExit("topology definition must declare exactly one canary queue")
    queue = canary_queues[0]
    canary_bindings = [binding for binding in expected_bindings(definition) if binding.get("queue") == queue["name"]]
    if len(canary_bindings) != 1:
        raise SystemExit("topology definition must declare exactly one canary binding")
    return queue, canary_bindings[0]


def ensure_policy(client: RabbitMQClient, definition: dict[str, Any], policy: dict[str, Any], report: dict[str, Any]) -> None:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "policies", vhost, policy["name"]))
    expected_payload = {
        "pattern": policy["pattern"],
        "definition": normalize_dict(policy["definition"]),
        "priority": int(policy["priority"]),
        "apply-to": policy["apply-to"],
    }
    current_payload = {}
    if isinstance(current, dict):
        current_payload = {
            "pattern": current.get("pattern"),
            "definition": normalize_dict(current.get("definition", {})),
            "priority": int(current.get("priority", -1)),
            "apply-to": current.get("apply-to"),
        }
    if current_payload != expected_payload:
        client.request("PUT", api_path("api", "policies", vhost, policy["name"]), payload=expected_payload)
        report["changed"] = True
        report["changes"].append(f"upserted_policy:{policy['name']}")


def check_exchange(client: RabbitMQClient, definition: dict[str, Any], exchange: dict[str, Any]) -> list[str]:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "exchanges", vhost, exchange["name"]))
    if current is None:
        return [f"missing_exchange:{exchange['id']}"]
    expected = {
        "type": exchange["type"],
        "durable": bool(exchange["durable"]),
        "auto_delete": False,
        "internal": False,
        "arguments": normalize_dict(exchange.get("arguments", {})),
    }
    drift = []
    for key in ("type", "durable", "auto_delete", "internal"):
        if current.get(key) != expected[key]:
            drift.append(f"{key} expected {expected[key]!r} observed {current.get(key)!r}")
    if normalize_dict(current.get("arguments", {})) != expected["arguments"]:
        drift.append("arguments differ")
    return [f"exchange:{exchange['id']}:{'; '.join(drift)}"] if drift else []


def check_queue(client: RabbitMQClient, definition: dict[str, Any], queue: dict[str, Any]) -> list[str]:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "queues", vhost, queue["name"]))
    if current is None:
        return [f"missing_queue:{queue['name']}"]
    expected = {
        "durable": True,
        "auto_delete": False,
        "arguments": normalize_dict(queue["arguments"]),
    }
    drift = []
    for key in ("durable", "auto_delete"):
        if current.get(key) != expected[key]:
            drift.append(f"{key} expected {expected[key]!r} observed {current.get(key)!r}")
    if normalize_dict(current.get("arguments", {})) != expected["arguments"]:
        drift.append("arguments differ")
    return [f"queue:{queue['name']}:{'; '.join(drift)}"] if drift else []


def check_policy(client: RabbitMQClient, definition: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    vhost = definition["vhost"]
    current = client.get_or_none(api_path("api", "policies", vhost, policy["name"]))
    if current is None:
        return [f"missing_policy:{policy['name']}"]
    expected = {
        "pattern": policy["pattern"],
        "definition": normalize_dict(policy["definition"]),
        "priority": int(policy["priority"]),
        "apply-to": policy["apply-to"],
    }
    observed = {
        "pattern": current.get("pattern"),
        "definition": normalize_dict(current.get("definition", {})),
        "priority": int(current.get("priority", -1)),
        "apply-to": current.get("apply-to"),
    }
    return [f"policy:{policy['name']}:definition differs"] if observed != expected else []


def live_drift(
    client: RabbitMQClient,
    definition: dict[str, Any],
    users: list[dict[str, Any]],
) -> list[str]:
    drift: list[str] = []
    vhost = definition["vhost"]
    if client.get_or_none(api_path("api", "vhosts", vhost)) is None:
        drift.append("missing_vhost")
    for exchange in definition["exchanges"]:
        drift.extend(check_exchange(client, definition, exchange))
    for queue in expected_queues(definition):
        drift.extend(check_queue(client, definition, queue))
    for policy in expected_policies(definition):
        drift.extend(check_policy(client, definition, policy))
    for binding in expected_bindings(definition):
        path = api_path("api", "bindings", vhost, "e", binding["exchange"], "q", binding["queue"])
        current = client.request("GET", path)
        if not any(
            candidate.get("routing_key") == binding["routing_key"]
            and normalize_dict(candidate.get("arguments", {})) == normalize_dict(binding.get("arguments", {}))
            for candidate in (current if isinstance(current, list) else [])
        ):
            drift.append(f"missing_binding:{binding['exchange']}->{binding['queue']}:{binding['routing_key']}")
    for user in users:
        current = client.get_or_none(api_path("api", "users", user["username"]))
        if current is None:
            drift.append(f"missing_user:{user['id']}")
            continue
        if normalize_tags(current.get("tags", "")) != normalize_tags(user.get("tags", [])):
            drift.append(f"user_tags:{user['id']}")
        permissions = client.get_or_none(api_path("api", "permissions", vhost, user["username"]))
        observed = {}
        if isinstance(permissions, dict):
            observed = {
                "configure": permissions.get("configure", ""),
                "write": permissions.get("write", ""),
                "read": permissions.get("read", ""),
            }
        if observed != user["permissions"]:
            drift.append(f"permissions:{user['id']}")
    if client.get_or_none(api_path("api", "users", "guest")) is not None:
        drift.append("guest_user_present")
    return drift


def regex_allows(pattern: str, resource: str) -> bool:
    return re.search(pattern, resource) is not None


def permission_matrix(definition: dict[str, Any], users: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    exchange_names = [exchange["name"] for exchange in definition["exchanges"]]
    main_exchange = exchange_by_id(definition, "main")["name"]
    retry_exchanges = {exchange_by_id(definition, exchange_id)["name"] for exchange_id in RETRY_WRITE_EXCHANGE_IDS}
    main_queues = {route["main_queue"]: route for route in definition["routes"]}
    all_queues = [queue["name"] for queue in expected_queues(definition)]
    canary = definition.get("canary") if isinstance(definition.get("canary"), dict) else {}
    canary_exchange = exchange_by_id(definition, canary["exchange_id"])["name"] if canary else ""
    canary_queue = str((canary.get("queue") or {}).get("name") or "") if canary else ""

    app_users = [user for user in users if user.get("kind") in {"producer", "consumer", "stage_runtime"}]
    if len({user["username"] for user in app_users}) != len(app_users):
        errors.append("app RabbitMQ users must have distinct username values")

    for user in users:
        user_id = user["id"]
        permissions = user["permissions"]
        kind = user.get("kind")
        configure = permissions.get("configure", "")
        write = permissions.get("write", "")
        read = permissions.get("read", "")
        if kind in {"producer", "consumer", "stage_runtime", "monitoring_canary"} and any(regex_allows(configure, resource) for resource in exchange_names + all_queues):
            errors.append(f"{user_id} unexpectedly has configure access")
        if kind == "producer":
            if not regex_allows(write, main_exchange):
                errors.append(f"{user_id} cannot write the main exchange")
            for exchange in retry_exchanges:
                if regex_allows(write, exchange):
                    errors.append(f"{user_id} can write retry/DLQ exchange {exchange}")
            if any(regex_allows(read, queue) for queue in all_queues):
                errors.append(f"{user_id} unexpectedly has queue read access")
        elif kind == "consumer":
            stage = user["stage"]
            route = next((candidate for candidate in definition["routes"] if candidate["consumer"] == stage), None)
            if route is None:
                errors.append(f"{user_id} does not map to a route consumer")
                continue
            if not regex_allows(read, route["main_queue"]):
                errors.append(f"{user_id} cannot read its route queue")
            for queue_name in main_queues:
                if queue_name != route["main_queue"] and regex_allows(read, queue_name):
                    errors.append(f"{user_id} can read unrelated queue {queue_name}")
            if regex_allows(write, main_exchange):
                errors.append(f"{user_id} can write the main exchange")
            for exchange in retry_exchanges:
                if not regex_allows(write, exchange):
                    errors.append(f"{user_id} cannot write retry/DLQ exchange {exchange}")
        elif kind == "stage_runtime":
            stage = user["stage"]
            route = next((candidate for candidate in definition["routes"] if candidate["consumer"] == stage), None)
            output_route = next((candidate for candidate in definition["routes"] if candidate["producer"] == stage), None)
            if route is None:
                errors.append(f"{user_id} does not map to a route consumer")
                continue
            if output_route is None:
                errors.append(f"{user_id} stage_runtime requires a declared outbound route")
            if not regex_allows(read, route["main_queue"]):
                errors.append(f"{user_id} cannot read its route queue")
            for queue_name in main_queues:
                if queue_name != route["main_queue"] and regex_allows(read, queue_name):
                    errors.append(f"{user_id} can read unrelated queue {queue_name}")
            if not regex_allows(write, main_exchange):
                errors.append(f"{user_id} cannot write the main exchange for its outbound route")
            for exchange in retry_exchanges:
                if not regex_allows(write, exchange):
                    errors.append(f"{user_id} cannot write retry/DLQ exchange {exchange}")
        elif kind == "monitoring_canary":
            if not canary_exchange or not canary_queue:
                errors.append(f"{user_id} requires a declared canary exchange and queue")
                continue
            if not regex_allows(write, canary_exchange):
                errors.append(f"{user_id} cannot write the canary exchange")
            if not regex_allows(read, canary_queue):
                errors.append(f"{user_id} cannot read the canary queue")
            for exchange in exchange_names:
                if exchange != canary_exchange and regex_allows(write, exchange):
                    errors.append(f"{user_id} can write non-canary exchange {exchange}")
            for queue in all_queues:
                if queue != canary_queue and regex_allows(read, queue):
                    errors.append(f"{user_id} can read non-canary queue {queue}")
        elif kind == "break_glass_admin":
            if permissions != {"configure": ".*", "write": ".*", "read": ".*"}:
                errors.append("break_glass_admin permissions must remain administrator-style")
    return errors


def publish_message(
    client: RabbitMQClient,
    definition: dict[str, Any],
    *,
    exchange: str,
    routing_key: str,
    message_id: str,
    probe_kind: str,
) -> bool:
    payload = {
        "probe": "nutsnews-rabbitmq-topology",
        "probe_kind": probe_kind,
        "message_id": message_id,
        "published_at_utc": utc_now(),
    }
    result = client.request(
        "POST",
        api_path("api", "exchanges", definition["vhost"], exchange, "publish"),
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


def get_one_message(client: RabbitMQClient, definition: dict[str, Any], *, queue: str, ackmode: str) -> dict[str, Any]:
    result = client.request(
        "POST",
        api_path("api", "queues", definition["vhost"], queue, "get"),
        payload={
            "count": 1,
            "ackmode": ackmode,
            "encoding": "auto",
            "truncate": 50000,
        },
    )
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"expected one message in queue {queue}")
    payload = json.loads(result[0].get("payload") or "{}")
    return payload


def queue_message_count(client: RabbitMQClient, definition: dict[str, Any], queue: str) -> int:
    result = client.request("GET", api_path("api", "queues", definition["vhost"], queue))
    if not isinstance(result, dict):
        raise RuntimeError(f"could not inspect queue {queue}")
    return int(result.get("messages", 0) or 0)


def queue_consumer_count(client: RabbitMQClient, definition: dict[str, Any], queue: str) -> int:
    result = client.request("GET", api_path("api", "queues", definition["vhost"], queue))
    if not isinstance(result, dict):
        raise RuntimeError(f"could not inspect queue {queue}")
    return int(result.get("consumers", 0) or 0)


def action_bootstrap(args: argparse.Namespace, client: RabbitMQClient, definition: dict[str, Any], users: list[dict[str, Any]]) -> int:
    wait_for_management(args, client)
    report: dict[str, Any] = {"status": "pass", "changed": False, "changes": [], "drift": []}
    ensure_vhost(client, definition, report)
    ensure_guest_deleted(client, report)
    for user in users:
        ensure_user(client, user, report, rotate_passwords=args.rotate_passwords)
    for user in users:
        ensure_permissions(client, definition, user, report)
    for exchange in definition["exchanges"]:
        ensure_exchange(client, definition, exchange, report)
    for policy in expected_policies(definition):
        ensure_policy(client, definition, policy, report)
    for queue in expected_queues(definition):
        ensure_queue(client, definition, queue, report)
    for binding in expected_bindings(definition):
        ensure_binding(client, definition, binding, report)
    if report["drift"]:
        report["status"] = "fail"
        print(json.dumps(redact_report(report), sort_keys=True))
        return 1
    print(json.dumps(redact_report(report), sort_keys=True))
    return 0


def action_check(args: argparse.Namespace, client: RabbitMQClient, definition: dict[str, Any], users: list[dict[str, Any]]) -> int:
    wait_for_management(args, client)
    drift = live_drift(client, definition, users)
    report = {"status": "pass" if not drift else "fail", "drift": drift, "changed": False}
    print(json.dumps(redact_report(report), sort_keys=True))
    return 0 if not drift else 1


def action_permissions(args: argparse.Namespace, client: RabbitMQClient, definition: dict[str, Any], users: list[dict[str, Any]]) -> int:
    wait_for_management(args, client)
    drift = live_drift(client, definition, users)
    matrix_errors = permission_matrix(definition, users)
    errors = drift + matrix_errors
    report = {
        "status": "pass" if not errors else "fail",
        "changed": False,
        "checked_users": len(users),
        "errors": errors,
    }
    print(json.dumps(redact_report(report), sort_keys=True))
    return 0 if not errors else 1


def action_repair_canary(args: argparse.Namespace, client: RabbitMQClient, definition: dict[str, Any], users: list[dict[str, Any]]) -> int:
    del users
    wait_for_management(args, client)
    queue, binding = canary_queue_and_binding(definition)
    vhost = definition["vhost"]
    report: dict[str, Any] = {
        "status": "pass",
        "changed": True,
        "changes": [],
        "drift": [],
        "scope": "canary_queue_only",
        "queue": queue["name"],
        "production_queues_touched": False,
        "safe_metadata_only": True,
    }
    client.request("DELETE", api_path("api", "queues", vhost, queue["name"]), ignored_statuses=(404,))
    report["changes"].append("deleted_canary_queue")
    ensure_queue(client, definition, queue, report)
    ensure_binding(client, definition, binding, report)
    if report["drift"]:
        report["status"] = "fail"
        print(json.dumps(redact_report(report), sort_keys=True))
        return 1
    print(json.dumps(redact_report(report), sort_keys=True))
    return 0


def action_probe_transfers(args: argparse.Namespace, client: RabbitMQClient, definition: dict[str, Any], users: list[dict[str, Any]]) -> int:
    del users
    wait_for_management(args, client)
    main_exchange = exchange_by_id(definition, "main")["name"]
    retry_exchange = exchange_by_id(definition, "retry")["name"]
    dlq_exchange = exchange_by_id(definition, "dlq")["name"]
    probed_stages: list[str] = []
    skipped_stages: list[str] = []
    skipped_queues: list[dict[str, str]] = []
    skipped_consumers: list[dict[str, str]] = []
    skip_non_empty = bool(getattr(args, "skip_non_empty", False))
    for route in definition["routes"]:
        queues_to_probe = [route["main_queue"], route["retry_queues"][0]["name"], route["terminal_dlq"]["name"]]
        non_empty_queues = [queue for queue in queues_to_probe if queue_message_count(client, definition, queue) != 0]
        active_consumer_queues = [queue for queue in queues_to_probe if queue_consumer_count(client, definition, queue) != 0]
        if non_empty_queues or active_consumer_queues:
            if skip_non_empty:
                skipped_stages.append(route["stage"])
                skipped_queues.extend({"stage": route["stage"], "queue": queue} for queue in non_empty_queues)
                skipped_consumers.extend({"stage": route["stage"], "queue": queue} for queue in active_consumer_queues)
                continue
            if active_consumer_queues:
                raise SystemExit(f"refusing transfer probe because queue has active consumers: {active_consumer_queues[0]}")
            raise SystemExit(f"refusing transfer probe because queue is non-empty: {non_empty_queues[0]}")

        source_message_id = str(uuid.uuid4())
        if not publish_message(
            client,
            definition,
            exchange=main_exchange,
            routing_key=route["routing_key"],
            message_id=source_message_id,
            probe_kind="source",
        ):
            raise SystemExit(f"source publish was not routed for stage {route['stage']}")

        unroutable_id = str(uuid.uuid4())
        if publish_message(
            client,
            definition,
            exchange=retry_exchange,
            routing_key=f"{route['routing_key']}.unroutable",
            message_id=unroutable_id,
            probe_kind="unroutable",
        ):
            raise SystemExit(f"unroutable retry publish unexpectedly routed for stage {route['stage']}")
        visible = get_one_message(client, definition, queue=route["main_queue"], ackmode="ack_requeue_true")
        if visible.get("message_id") != source_message_id:
            raise SystemExit(f"source message was not visible after unroutable transfer for stage {route['stage']}")

        retry_message_id = str(uuid.uuid4())
        if not publish_message(
            client,
            definition,
            exchange=retry_exchange,
            routing_key=route["retry_queues"][0]["routing_key"],
            message_id=retry_message_id,
            probe_kind="retry",
        ):
            raise SystemExit(f"retry transfer was not routed for stage {route['stage']}")
        acked_source = get_one_message(client, definition, queue=route["main_queue"], ackmode="ack_requeue_false")
        if acked_source.get("message_id") != source_message_id:
            raise SystemExit(f"source cleanup message mismatch for stage {route['stage']}")
        retry_payload = get_one_message(client, definition, queue=route["retry_queues"][0]["name"], ackmode="ack_requeue_false")
        if retry_payload.get("message_id") != retry_message_id:
            raise SystemExit(f"retry probe message mismatch for stage {route['stage']}")

        dlq_message_id = str(uuid.uuid4())
        if not publish_message(
            client,
            definition,
            exchange=dlq_exchange,
            routing_key=route["terminal_dlq"]["routing_key"],
            message_id=dlq_message_id,
            probe_kind="dlq",
        ):
            raise SystemExit(f"DLQ transfer was not routed for stage {route['stage']}")
        dlq_payload = get_one_message(client, definition, queue=route["terminal_dlq"]["name"], ackmode="ack_requeue_false")
        if dlq_payload.get("message_id") != dlq_message_id:
            raise SystemExit(f"DLQ probe message mismatch for stage {route['stage']}")
        probed_stages.append(route["stage"])

    report = {
        "status": "pass",
        "changed": False,
        "probed_stages": probed_stages,
        "skipped_stages": skipped_stages,
        "skipped_queues": skipped_queues,
        "skipped_consumers": skipped_consumers,
        "non_empty_queue_behavior": "skip_without_mutating_existing_messages" if skip_non_empty else "fail_closed",
        "unroutable_target_behavior": "source_message_visible_until_confirmed_target_publish",
        "full_target_behavior": "all_queues_use_reject-publish_overflow_and_application_ack_requires_confirmed_target_publish",
    }
    print(json.dumps(report, sort_keys=True))
    return 0


def redact_report(report: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(report)
    if "changes" in redacted:
        redacted["changes"] = sorted(set(str(change) for change in redacted["changes"]))
    if "drift" in redacted:
        redacted["drift"] = sorted(set(str(item) for item in redacted["drift"]))
    if "errors" in redacted:
        redacted["errors"] = sorted(set(str(item) for item in redacted["errors"]))
    return redacted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "check", "permissions", "repair-canary", "probe-transfers"))
    parser.add_argument("--env", type=Path, required=True, help="Root-only RabbitMQ admin env file.")
    parser.add_argument("--credentials-env", type=Path, required=True, help="Root-only topology user env file.")
    parser.add_argument("--definition", type=Path, required=True, help="Source-controlled topology definition JSON.")
    parser.add_argument("--management-url", default=DEFAULT_MANAGEMENT_URL)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--rotate-passwords", action="store_true", help="Update managed user passwords from the credential env file.")
    parser.add_argument("--skip-non-empty", action="store_true", help="Skip route transfer probes when any stage queue already has messages.")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        definition = load_definition(args.definition)
        admin_username, admin_password, admin_vhost = admin_credentials(args.env)
        env_values = load_secret_environment(args.credentials_env)
        if definition["vhost"] != admin_vhost:
            raise SystemExit("topology definition vhost must match RabbitMQ admin env vhost")
        if env_values["RABBITMQ_BREAK_GLASS_ADMIN_USERNAME"] != admin_username:
            raise SystemExit("break-glass admin username must match RabbitMQ default admin username")
        if env_values["RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD"] != admin_password:
            raise SystemExit("break-glass admin password must match RabbitMQ default admin password")
        users = user_records(definition, env_values)
        client = RabbitMQClient(base_url=args.management_url, username=admin_username, password=admin_password)
        if args.action == "bootstrap":
            return action_bootstrap(args, client, definition, users)
        if args.action == "check":
            return action_check(args, client, definition, users)
        if args.action == "permissions":
            return action_permissions(args, client, definition, users)
        if args.action == "repair-canary":
            return action_repair_canary(args, client, definition, users)
        return action_probe_transfers(args, client, definition, users)
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
