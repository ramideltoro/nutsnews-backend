#!/usr/bin/env python3
"""Run an ephemeral RabbitMQ classic-vs-single-replica-quorum benchmark.

This script is for CI or local disposable brokers only. It does not provision
the production backend host and it does not mutate legacy worker paths.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROUTE_COUNT = 7
RETRY_TIERS_PER_ROUTE = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amqp-url", default=os.environ.get("RABBITMQ_BENCHMARK_URL", "amqp://guest:guest@127.0.0.1:5672/%2F"))
    parser.add_argument("--management-url", default=os.environ.get("RABBITMQ_MANAGEMENT_URL", "http://127.0.0.1:15672"))
    parser.add_argument("--management-user", default=os.environ.get("RABBITMQ_MANAGEMENT_USER", "guest"))
    parser.add_argument("--management-password", default=os.environ.get("RABBITMQ_MANAGEMENT_PASSWORD", "guest"))
    parser.add_argument("--messages-per-queue", type=int, default=50)
    parser.add_argument("--message-bytes", type=int, default=1024)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--connect-timeout-seconds", type=int, default=90)
    return parser.parse_args()


def import_pika() -> Any:
    try:
        import pika  # type: ignore
    except ModuleNotFoundError:
        print("ERROR: missing Python dependency 'pika'; install pika==1.3.2 before running the benchmark", file=sys.stderr)
        raise SystemExit(2) from None
    return pika


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def management_get(base_url: str, user: str, password: str, path: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def node_snapshot(base_url: str, user: str, password: str) -> dict[str, Any]:
    nodes = management_get(base_url, user, password, "/api/nodes")
    if not isinstance(nodes, list) or not nodes:
        return {"status": "unavailable"}
    node = nodes[0]
    keys = (
        "name",
        "mem_used",
        "mem_limit",
        "disk_free",
        "disk_free_limit",
        "fd_used",
        "fd_total",
        "proc_used",
        "proc_total",
        "run_queue",
        "sockets_used",
        "sockets_total",
    )
    return {"status": "available", **{key: node.get(key) for key in keys if key in node}}


def queue_names(queue_type: str) -> list[str]:
    names: list[str] = []
    for route in range(1, ROUTE_COUNT + 1):
        names.append(f"bench.{queue_type}.route{route}.main")
        for retry in range(1, RETRY_TIERS_PER_ROUTE + 1):
            names.append(f"bench.{queue_type}.route{route}.retry.{retry}")
        names.append(f"bench.{queue_type}.route{route}.dlq")
    return names


def connect_with_retry(pika: Any, amqp_url: str, timeout_seconds: int) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            parameters = pika.URLParameters(amqp_url)
            parameters.heartbeat = 30
            parameters.blocked_connection_timeout = 30
            return pika.BlockingConnection(parameters)
        except Exception as exc:  # pragma: no cover - exercised in CI workflow
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"could not connect to RabbitMQ within {timeout_seconds}s: {last_error}") from last_error


def queue_arguments(queue_type: str) -> dict[str, Any]:
    args: dict[str, Any] = {
        "x-queue-type": "quorum" if queue_type == "quorum_single_replica" else "classic",
        "x-max-length": 2000,
        "x-max-length-bytes": 268435456,
        "x-overflow": "reject-publish",
    }
    if queue_type == "quorum_single_replica":
        args["x-quorum-initial-group-size"] = 1
    return args


def declare_queues(channel: Any, names: list[str], queue_type: str) -> None:
    args = queue_arguments(queue_type)
    for name in names:
        channel.queue_declare(queue=name, durable=True, exclusive=False, auto_delete=False, arguments=args)


def delete_queues(channel: Any, names: list[str]) -> None:
    for name in names:
        try:
            channel.queue_delete(queue=name)
        except Exception:
            pass


def publish_messages(pika: Any, channel: Any, names: list[str], messages_per_queue: int, body: bytes) -> int:
    channel.confirm_delivery()
    properties = pika.BasicProperties(
        delivery_mode=2,
        content_type="application/octet-stream",
        headers={"benchmark": "worker-uplift-79"},
    )
    count = 0
    for name in names:
        for _ in range(messages_per_queue):
            channel.basic_publish(exchange="", routing_key=name, body=body, properties=properties, mandatory=True)
            count += 1
    return count


def drain_messages(channel: Any, names: list[str], messages_per_queue: int, timeout_seconds: int = 60) -> int:
    deadline = time.monotonic() + timeout_seconds
    drained = 0
    expected = len(names) * messages_per_queue
    while drained < expected and time.monotonic() < deadline:
        progressed = False
        for name in names:
            method_frame, _, _ = channel.basic_get(queue=name, auto_ack=False)
            if method_frame is None:
                continue
            channel.basic_ack(delivery_tag=method_frame.delivery_tag)
            drained += 1
            progressed = True
        if not progressed:
            time.sleep(0.05)
    if drained != expected:
        raise RuntimeError(f"drained {drained} messages, expected {expected}")
    return drained


def run_one(pika: Any, connection: Any, args: argparse.Namespace, queue_type: str) -> dict[str, Any]:
    names = queue_names(queue_type)
    channel = connection.channel()
    body = b"x" * args.message_bytes
    result: dict[str, Any] = {
        "queue_type": queue_type,
        "queue_count": len(names),
        "messages_per_queue": args.messages_per_queue,
        "message_body_bytes": args.message_bytes,
        "durable": True,
        "publisher_confirms": True,
        "queue_arguments": queue_arguments(queue_type),
    }
    try:
        delete_queues(channel, names)
        before = node_snapshot(args.management_url, args.management_user, args.management_password)

        declare_start = time.perf_counter()
        declare_queues(channel, names, queue_type)
        declare_seconds = time.perf_counter() - declare_start

        publish_start = time.perf_counter()
        published = publish_messages(pika, channel, names, args.messages_per_queue, body)
        publish_seconds = time.perf_counter() - publish_start

        after_publish = node_snapshot(args.management_url, args.management_user, args.management_password)

        drain_start = time.perf_counter()
        drained = drain_messages(channel, names, args.messages_per_queue)
        drain_seconds = time.perf_counter() - drain_start

        after_drain = node_snapshot(args.management_url, args.management_user, args.management_password)
        result.update(
            {
                "published_messages": published,
                "drained_messages": drained,
                "declare_seconds": round(declare_seconds, 6),
                "publish_seconds": round(publish_seconds, 6),
                "drain_seconds": round(drain_seconds, 6),
                "publish_messages_per_second": round(published / publish_seconds, 3) if publish_seconds > 0 else None,
                "drain_messages_per_second": round(drained / drain_seconds, 3) if drain_seconds > 0 else None,
                "node_before": before,
                "node_after_publish": after_publish,
                "node_after_drain": after_drain,
            }
        )
    finally:
        delete_queues(channel, names)
        channel.close()
    return result


def main() -> int:
    args = parse_args()
    if args.messages_per_queue <= 0 or args.message_bytes <= 0:
        print("ERROR: messages-per-queue and message-bytes must be positive", file=sys.stderr)
        return 2

    pika = import_pika()
    connection = connect_with_retry(pika, args.amqp_url, args.connect_timeout_seconds)
    try:
        overview = management_get(args.management_url, args.management_user, args.management_password, "/api/overview")
        results = {
            "benchmark": "worker-uplift-rabbitmq-capacity",
            "tracking_issue": 79,
            "captured_at_utc": now_utc(),
            "scenario": {
                "route_count": ROUTE_COUNT,
                "retry_tiers_per_route": RETRY_TIERS_PER_ROUTE,
                "queues_per_type": len(queue_names("classic")),
                "messages_per_queue": args.messages_per_queue,
                "message_body_bytes": args.message_bytes,
            },
            "broker": overview if isinstance(overview, dict) else {"status": "management_api_unavailable"},
            "results": [
                run_one(pika, connection, args, "classic"),
                run_one(pika, connection, args, "quorum_single_replica"),
            ],
            "selection_policy": "durable classic remains selected for single-node bootstrap unless a later approved capacity decision changes the queue type.",
        }
    finally:
        connection.close()

    rendered = json.dumps(results, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
