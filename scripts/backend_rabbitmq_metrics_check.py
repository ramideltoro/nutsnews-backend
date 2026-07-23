#!/usr/bin/env python3
"""Run fixed read-only RabbitMQ metrics checks for backend Alloy collection."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import parse, request


TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)

QUEUE_REGEX = r"^nutsnews\.worker\.(fetch|canonicalization|enrichment|approval|translation|persistence|publication)\.v1(\.retry-(30s|5m|30m)|\.dlq)?$"
REMOTE_COMMANDS: dict[str, str] = {
    "rabbitmq_aggregate_metrics": "curl -fsS --max-time 10 http://127.0.0.1:15692/metrics 2>/dev/null || true",
    "rabbitmq_detailed_metrics": (
        "curl -fsS --max-time 10 "
        "'http://127.0.0.1:15692/metrics/detailed?"
        "family=queue_coarse_metrics&family=queue_consumer_count&family=queue_delivery_metrics"
        "&vhost=nutsnews-worker-uplift"
        "&queue=^nutsnews\\.worker\\.' 2>/dev/null || true"
    ),
    "alloy_active": "systemctl is-active alloy 2>/dev/null || true",
    "alloy_config": "sudo -n alloy validate /etc/alloy/config.alloy 2>&1 || true",
    "rabbitmq_listener": "ss -H -tln 'sport = :15692' 2>/dev/null || true",
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    value = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    value = TOKEN_RE.sub("<redacted-token>", value)
    value = URL_SECRET_RE.sub(r"\1<redacted>\3", value)
    return value


def run_ssh_command(host: str, user: str, key: Path, known_hosts: Path, command: str, timeout: int) -> dict[str, Any]:
    ssh_command = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"ConnectTimeout={timeout}",
        f"{user}@{host}",
        command,
    ]
    completed = subprocess.run(
        ssh_command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout + 15,
    )
    return {
        "rc": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def collect_live(host: str, user: str, key: Path, known_hosts: Path, timeout: int) -> dict[str, Any]:
    return {
        "commands": {
            name: run_ssh_command(host, user, key, known_hosts, command, timeout)
            for name, command in REMOTE_COMMANDS.items()
        }
    }


def command_stdout(evidence: dict[str, Any], name: str) -> str:
    return evidence.get("commands", {}).get(name, {}).get("stdout", "")


def check(name: str, status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "summary": summary, "details": details or {}}


def classify_local(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    aggregate = command_stdout(evidence, "rabbitmq_aggregate_metrics")
    detailed = command_stdout(evidence, "rabbitmq_detailed_metrics")
    alloy_active = command_stdout(evidence, "alloy_active").strip()
    alloy_config = command_stdout(evidence, "alloy_config")
    listener = command_stdout(evidence, "rabbitmq_listener")

    aggregate_has_metrics = "rabbitmq_" in aggregate or "erlang_" in aggregate
    checks.append(
        check(
            "rabbitmq_aggregate_endpoint",
            "healthy" if aggregate_has_metrics else "critical",
            "aggregate metrics present" if aggregate_has_metrics else "aggregate metrics missing",
            {"bytes": len(aggregate)},
        )
    )
    detailed_has_queue_metrics = "rabbitmq_detailed_queue_messages" in detailed or "rabbitmq_detailed_queue_consumers" in detailed
    checks.append(
        check(
            "rabbitmq_detailed_endpoint",
            "healthy" if detailed_has_queue_metrics else "critical",
            "detailed queue metrics present" if detailed_has_queue_metrics else "detailed queue metrics missing",
            {"bytes": len(detailed), "queue_regex": QUEUE_REGEX},
        )
    )
    public_listeners = ("0.0.0.0:15692", "*:15692", "[::]:15692", ":::15692")
    loopback_only = (
        ("127.0.0.1:15692" in listener or "[::1]:15692" in listener)
        and not any(public_listener in listener for public_listener in public_listeners)
    )
    checks.append(
        check(
            "rabbitmq_prometheus_listener",
            "healthy" if loopback_only else "critical",
            "RabbitMQ Prometheus listener is loopback-only" if loopback_only else "RabbitMQ Prometheus listener is not proven loopback-only",
            {"listener": redact(listener.strip())},
        )
    )
    checks.append(
        check(
            "alloy_service",
            "healthy" if alloy_active == "active" else "critical",
            f"alloy={alloy_active or 'unknown'}",
        )
    )
    checks.append(
        check(
            "alloy_config",
            "healthy" if "Config file is valid" in alloy_config or "configuration loaded" in alloy_config.lower() else "critical",
            "Alloy config validates" if "Config file is valid" in alloy_config or "configuration loaded" in alloy_config.lower() else "Alloy config validation did not report success",
            {"output": redact(alloy_config.strip()[-1000:])},
        )
    )
    return checks


def derive_prometheus_query_url(remote_write_url: str) -> str:
    if not remote_write_url:
        return ""
    parsed = parse.urlparse(remote_write_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/prom/push"):
        path = path[: -len("/api/prom/push")] + "/api/prom/api/v1/query"
    elif path.endswith("/api/v1/push"):
        path = path[: -len("/api/v1/push")] + "/api/v1/query"
    elif not path.endswith("/api/v1/query"):
        path = path + "/api/v1/query"
    return parse.urlunparse(parsed._replace(path=path, query="", params="", fragment=""))


def grafana_query(remote_write_url: str, username: str, password: str, expression: str, timeout: int) -> dict[str, Any]:
    query_url = derive_prometheus_query_url(remote_write_url)
    if not query_url or not username or not password:
        return {"status": "not_configured", "summary": "Grafana Cloud Prometheus query credentials missing"}
    body = parse.urlencode({"query": expression}).encode("utf-8")
    req = request.Request(query_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - classify safely for workflow report
        return {"status": "critical", "summary": f"Grafana query failed: {type(exc).__name__}"}
    result = data.get("data", {}).get("result", []) if isinstance(data, dict) else []
    return {
        "status": "healthy" if result else "critical",
        "summary": f"result_count={len(result)}",
        "query_url": redact(query_url),
    }


def build_report(args: argparse.Namespace, evidence: dict[str, Any]) -> dict[str, Any]:
    checks = classify_local(evidence)
    if args.grafana_prometheus_url:
        rabbitmq_query = grafana_query(
            args.grafana_prometheus_url,
            args.grafana_prometheus_username,
            args.grafana_prometheus_password,
            'up{job=~"nutsnews-rabbitmq|nutsnews-rabbitmq-queues",environment="production"}',
            args.timeout,
        )
        checks.append(check("grafana_rabbitmq_query", rabbitmq_query["status"], rabbitmq_query["summary"], rabbitmq_query))
    elif args.require_grafana_data:
        checks.append(check("grafana_rabbitmq_query", "critical", "Grafana data was required but query credentials were not provided"))
    status = "pass" if all(item["status"] in {"healthy", "not_configured"} for item in checks) and not (
        args.require_grafana_data and any(item["name"] == "grafana_rabbitmq_query" and item["status"] != "healthy" for item in checks)
    ) else "fail"
    return {
        "status": status,
        "generated_at_utc": utc_now(),
        "tracking_issue": 87,
        "safe_metadata_only": True,
        "checks": checks,
        "summary": {
            "healthy": sum(1 for item in checks if item["status"] == "healthy"),
            "critical": sum(1 for item in checks if item["status"] == "critical"),
            "not_configured": sum(1 for item in checks if item["status"] == "not_configured"),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check backend RabbitMQ metrics collection.")
    parser.add_argument("--ssh-host", default="65.75.201.18")
    parser.add_argument("--ssh-user", default="rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--grafana-prometheus-url", default="")
    parser.add_argument("--grafana-prometheus-username", default="")
    parser.add_argument("--grafana-prometheus-password", default="")
    parser.add_argument("--require-grafana-data", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    evidence = collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    report = build_report(args, evidence)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
