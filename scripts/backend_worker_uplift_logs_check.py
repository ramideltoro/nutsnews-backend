#!/usr/bin/env python3
"""Run fixed read-only worker-uplift log and trace guardrail checks."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TRACKING_ISSUE = 88
DEPLOYMENT_ENVIRONMENT = "production"
WORKER_SERVICES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
RABBITMQ_LOG_QUERY = (
    '{deployment_environment="production",host="backend.nutsnews.com",'
    'source="container",service="rabbitmq"}'
)
WORKER_LOG_QUERY = (
    '{deployment_environment="production",host="backend.nutsnews.com",source="container",'
    'service=~"scheduler|fetcher|canonicalizer|enrichment|approval|translation|persistence|publication"}'
)


def worker_log_query(service: str) -> str:
    if service not in WORKER_SERVICES:
        raise ValueError("worker service must come from the fixed allow-list")
    return (
        f'{{deployment_environment="{DEPLOYMENT_ENVIRONMENT}",host="backend.nutsnews.com",'
        f'source="container",service="{service}"}}'
    )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def redact(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        return urllib.parse.urlunparse(parsed._replace(query="", params="", fragment=""))
    return value


def check(name: str, status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": details or {},
    }


def run_ssh(args: argparse.Namespace, command: str, timeout: int = 20) -> dict[str, Any]:
    ssh_target = f"{args.ssh_user}@{args.ssh_host}"
    argv = [
        "ssh",
        "-i",
        str(args.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        ssh_target,
        "bash",
        "-lc",
        shlex.quote(command),
    ]
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": "timeout"}
    return {
        "rc": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def numeric_stdout(result: dict[str, Any]) -> int:
    try:
        return int(str(result.get("stdout", "")).strip())
    except ValueError:
        return 0


def local_checks(args: argparse.Namespace) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    alloy_active = run_ssh(args, "systemctl is-active alloy 2>/dev/null || true")
    checks.append(
        check(
            "alloy_service",
            "healthy" if alloy_active["stdout"] == "active" else "critical",
            f"alloy={alloy_active['stdout'] or 'unknown'}",
        )
    )

    alloy_config = run_ssh(args, "sudo -n alloy validate /etc/alloy/config.alloy >/dev/null 2>&1 && echo valid || echo invalid")
    checks.append(
        check(
            "alloy_config",
            "healthy" if alloy_config["stdout"] == "valid" else "critical",
            "Alloy config validates" if alloy_config["stdout"] == "valid" else "Alloy config failed validation",
        )
    )

    loki_write = run_ssh(args, "sudo -n grep -q 'loki.write \"grafana_cloud_loki\"' /etc/alloy/config.alloy && echo yes || echo no")
    checks.append(
        check(
            "loki_write_config",
            "healthy" if loki_write["stdout"] == "yes" else "critical",
            "Loki write endpoint configured" if loki_write["stdout"] == "yes" else "Loki write endpoint missing",
        )
    )

    container_sources = run_ssh(args, "sudo -n grep -c 'CONTAINER_TAG=nutsnews-worker-uplift-' /etc/alloy/config.alloy || true")
    source_count = numeric_stdout(container_sources)
    checks.append(
        check(
            "worker_uplift_container_sources",
            "healthy" if source_count >= 9 else "critical",
            f"container_source_count={source_count}",
        )
    )

    trace_export = run_ssh(
        args,
        "if sudo -n grep -Eq 'otelcol.receiver.otlp|otelcol.exporter.otlp|tempo|traces:write' /etc/alloy/config.alloy; then echo enabled; else echo disabled; fi",
    )
    checks.append(
        check(
            "trace_export_deferred",
            "healthy" if trace_export["stdout"] == "disabled" else "critical",
            "trace export disabled" if trace_export["stdout"] == "disabled" else "unexpected trace export config found",
        )
    )

    rabbitmq_journal = run_ssh(
        args,
        "sudo -n journalctl CONTAINER_TAG=nutsnews-worker-uplift-rabbitmq --since '24 hours ago' --no-pager -q | wc -l",
    )
    rabbitmq_line_count = numeric_stdout(rabbitmq_journal)
    checks.append(
        check(
            "rabbitmq_journal_logs",
            "healthy" if rabbitmq_line_count > 0 else "critical",
            f"line_count={rabbitmq_line_count}",
        )
    )

    return checks


def derive_loki_query_range_url(loki_url: str) -> str:
    if not loki_url:
        return ""
    parsed = urllib.parse.urlparse(loki_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/loki/api/v1/push"):
        path = path[: -len("/loki/api/v1/push")] + "/loki/api/v1/query_range"
    elif path.endswith("/loki/api/v1/query_range"):
        pass
    elif path.endswith("/loki"):
        path = path + "/api/v1/query_range"
    else:
        path = path + "/loki/api/v1/query_range"
    return urllib.parse.urlunparse(parsed._replace(path=path, query="", params="", fragment=""))


def loki_query_range(loki_url: str, username: str, password: str, query: str, *, hours: int, timeout: int) -> dict[str, Any]:
    query_url = derive_loki_query_range_url(loki_url)
    if not query_url or not username or not password:
        return {
            "status": "not_configured",
            "summary": "Grafana Cloud Loki query credentials missing",
            "credential_error": True,
        }
    now_ns = int(time.time() * 1_000_000_000)
    start_ns = now_ns - hours * 60 * 60 * 1_000_000_000
    encoded = urllib.parse.urlencode(
        {
            "query": query,
            "start": str(start_ns),
            "end": str(now_ns),
            "limit": "20",
            "direction": "backward",
        }
    )
    req = urllib.request.Request(f"{query_url}?{encoded}", method="GET")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "status": "critical",
            "summary": f"Loki query failed: HTTP {exc.code}",
            "credential_error": exc.code in {401, 403},
            "query_url": redact(query_url),
        }
    except Exception as exc:  # noqa: BLE001 - safe workflow classification
        return {
            "status": "critical",
            "summary": f"Loki query failed: {type(exc).__name__}",
            "credential_error": False,
            "query_url": redact(query_url),
        }
    result = data.get("data", {}).get("result", []) if isinstance(data, dict) else []
    line_count = 0
    if isinstance(result, list):
        for stream in result:
            values = stream.get("values", []) if isinstance(stream, dict) else []
            if isinstance(values, list):
                line_count += len(values)
    return {
        "status": "healthy" if line_count > 0 else "critical",
        "summary": f"stream_count={len(result) if isinstance(result, list) else 0}, line_count={line_count}",
        "credential_error": False,
        "query_url": redact(query_url),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    checks = local_checks(args)
    rabbitmq_loki = loki_query_range(
        args.loki_url,
        args.loki_username,
        args.loki_password,
        RABBITMQ_LOG_QUERY,
        hours=args.query_hours,
        timeout=args.timeout,
    )
    if not args.require_loki_data and rabbitmq_loki["status"] != "healthy":
        rabbitmq_loki = {
            **rabbitmq_loki,
            "status": "not_configured",
            "summary": f"optional Loki query did not pass: {rabbitmq_loki['summary']}",
        }
    checks.append(check("loki_rabbitmq_query", rabbitmq_loki["status"], rabbitmq_loki["summary"], rabbitmq_loki))

    worker_results: dict[str, dict[str, Any]] = {}
    for service in WORKER_SERVICES:
        worker_results[service] = loki_query_range(
            args.loki_url,
            args.loki_username,
            args.loki_password,
            worker_log_query(service),
            hours=args.query_hours,
            timeout=args.timeout,
        )
    missing_worker_logs = [
        service
        for service, result in worker_results.items()
        if result.get("status") != "healthy"
    ]
    worker_status = "healthy" if not missing_worker_logs else (
        "critical" if args.require_loki_data else "not_configured"
    )
    checks.append(
        check(
            "loki_worker_service_query",
            worker_status,
            (
                "all eight current worker services have recent logs"
                if not missing_worker_logs
                else f"missing_worker_services={','.join(missing_worker_logs)}"
            ),
            {
                "expected_service_count": len(WORKER_SERVICES),
                "healthy_service_count": len(WORKER_SERVICES) - len(missing_worker_logs),
                "missing_worker_services": missing_worker_logs,
                "credential_error": any(
                    result.get("credential_error") is True
                    for result in worker_results.values()
                ),
                "services": worker_results,
            },
        )
    )

    credential_error = any(item.get("details", {}).get("credential_error") for item in checks)
    required_loki_failed = bool(
        args.require_loki_data
        and (rabbitmq_loki["status"] != "healthy" or worker_status != "healthy")
    )
    status = "pass" if all(item["status"] in {"healthy", "not_configured"} for item in checks) and not required_loki_failed else "fail"
    return {
        "status": status,
        "generated_at_utc": utc_now(),
        "tracking_issue": TRACKING_ISSUE,
        "safe_metadata_only": True,
        "credential_error": credential_error,
        "traces_enabled": False,
        "queries": {
            "rabbitmq": RABBITMQ_LOG_QUERY,
            "worker_service_group": WORKER_LOG_QUERY,
            "worker_services": {
                service: worker_log_query(service)
                for service in WORKER_SERVICES
            },
        },
        "checks": checks,
        "summary": {
            "healthy": sum(1 for item in checks if item["status"] == "healthy"),
            "not_configured": sum(1 for item in checks if item["status"] == "not_configured"),
            "critical": sum(1 for item in checks if item["status"] == "critical"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="65.75.201.18")
    parser.add_argument("--ssh-user", default="rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--loki-url", default="")
    parser.add_argument("--loki-username", default="")
    parser.add_argument("--loki-password", default="")
    parser.add_argument("--require-loki-data", action="store_true")
    parser.add_argument("--query-hours", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
