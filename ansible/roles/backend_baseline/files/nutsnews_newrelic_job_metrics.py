#!/usr/bin/env python3
"""Publish low-cardinality backend job telemetry to New Relic Metric API."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_JOBS_CONFIG = Path("/etc/nutsnews-newrelic/background-jobs.json")
DEFAULT_OUTPUT = Path("/var/lib/nutsnews/newrelic/job-metrics-last.json")
ENDPOINTS = {
    "us": "https://metric-api.newrelic.com/metric/v1",
    "eu": "https://metric-api.eu.newrelic.com/metric/v1",
    "jp": "https://metric-api.jp.nr-data.net/metric/v1",
}
SYSTEMD_PROPERTIES = (
    "ActiveState",
    "SubState",
    "LoadState",
    "Result",
    "NRestarts",
    "ExecMainStatus",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "ActiveEnterTimestampMonotonic",
    "InactiveEnterTimestampMonotonic",
)
SAFE_ATTRIBUTE = re.compile(r"[^A-Za-z0-9_.-]+")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_attribute(value: Any, default: str = "unknown") -> str:
    rendered = SAFE_ATTRIBUTE.sub("_", str(value or default).strip())[:96].strip("._-")
    return rendered or default


def parse_usec(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def run_systemctl_show(unit: str) -> dict[str, str]:
    command = ["systemctl", "show", unit]
    for prop in SYSTEMD_PROPERTIES:
        command.append(f"--property={prop}")
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"LoadState": "unknown", "ActiveState": "unknown", "Result": "unknown"}
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    if completed.returncode != 0:
        properties.setdefault("LoadState", "not-found")
    return properties


def read_jobs_config(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("jobs config must be a list")
    jobs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("job config entries must be objects")
        name = safe_attribute(item.get("name"))
        service_unit = safe_attribute(item.get("service_unit"))
        timer_unit = safe_attribute(item.get("timer_unit", "none"))
        if service_unit == "unknown":
            raise ValueError(f"job {name} missing service_unit")
        jobs.append({"name": name, "service_unit": service_unit, "timer_unit": timer_unit})
    return jobs


def job_status(properties: dict[str, str]) -> str:
    load_state = properties.get("LoadState") or "unknown"
    active_state = properties.get("ActiveState") or "unknown"
    result = properties.get("Result") or "unknown"
    exec_status = properties.get("ExecMainStatus") or "0"
    if load_state == "not-found":
        return "not_loaded"
    if active_state in {"active", "activating"}:
        return "running"
    if result == "success" and exec_status in {"", "0"}:
        return "success"
    if result in {"exit-code", "signal", "timeout", "core-dump", "watchdog", "resources"} or exec_status not in {"", "0"}:
        return "failure"
    return safe_attribute(result or active_state)


def duration_ms(properties: dict[str, str], now_monotonic_usec: int) -> int | None:
    start = parse_usec(properties.get("ExecMainStartTimestampMonotonic")) or parse_usec(properties.get("ActiveEnterTimestampMonotonic"))
    end = parse_usec(properties.get("ExecMainExitTimestampMonotonic")) or parse_usec(properties.get("InactiveEnterTimestampMonotonic"))
    if start is None:
        return None
    if end is None and properties.get("ActiveState") in {"active", "activating"}:
        end = now_monotonic_usec
    if end is None or end < start:
        return None
    return int((end - start) / 1000)


def build_metric(name: str, value: int | float, timestamp: int, attributes: dict[str, str]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "gauge",
        "value": value,
        "timestamp": timestamp,
        "attributes": attributes,
    }


def build_payload(jobs: list[dict[str, str]], *, now: int | None = None, now_monotonic_usec: int | None = None) -> list[dict[str, Any]]:
    timestamp = now if now is not None else int(time.time())
    monotonic_usec = now_monotonic_usec if now_monotonic_usec is not None else int(time.monotonic_ns() / 1000)
    environment = safe_attribute(os.environ.get("NUTSNEWS_BACKEND_ENVIRONMENT", "production"))
    service_name = safe_attribute(os.environ.get("NEW_RELIC_SERVICE_NAME", "nutsnews-backend-production"))
    host_name = safe_attribute(os.environ.get("NUTSNEWS_BACKEND_HOST", "backend.nutsnews.com"))
    metrics: list[dict[str, Any]] = []
    for job in jobs:
        properties = run_systemctl_show(job["service_unit"])
        status = job_status(properties)
        attributes = {
            "job.name": job["name"],
            "workflow.name": "backend_host_scheduled_tasks",
            "systemd.unit": job["service_unit"],
            "systemd.timer": job["timer_unit"],
            "environment": environment,
            "status": status,
        }
        metrics.append(build_metric("Custom/NutsNews/job/success", 1 if status == "success" else 0, timestamp, attributes))
        metrics.append(build_metric("Custom/NutsNews/job/failure", 1 if status == "failure" else 0, timestamp, attributes))
        metrics.append(build_metric("Custom/NutsNews/job/active", 1 if status == "running" else 0, timestamp, attributes))
        restarts = properties.get("NRestarts")
        if restarts and restarts.isdigit():
            metrics.append(build_metric("Custom/NutsNews/job/restartCount", int(restarts), timestamp, attributes))
        runtime = duration_ms(properties, monotonic_usec)
        if runtime is not None:
            metrics.append(build_metric("Custom/NutsNews/job/durationMs", runtime, timestamp, attributes))
    return [
        {
            "common": {
                "attributes": {
                    "service.name": service_name,
                    "environment": environment,
                    "host.name": host_name,
                }
            },
            "metrics": metrics,
        }
    ]


def metric_endpoint() -> str:
    explicit = os.environ.get("NEW_RELIC_METRIC_API_ENDPOINT", "").strip()
    if explicit:
        return explicit
    region = os.environ.get("NEW_RELIC_REGION", "us").strip().lower() or "us"
    if region not in ENDPOINTS:
        raise ValueError(f"unsupported NEW_RELIC_REGION: {region}")
    return ENDPOINTS[region]


def post_payload(payload: list[dict[str, Any]], endpoint: str, license_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Api-Key": license_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.status, "response": body[:200]}


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def main_args(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-config", type=Path, default=DEFAULT_JOBS_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    jobs = read_jobs_config(args.jobs_config)
    payload = build_payload(jobs)
    report: dict[str, Any] = {
        "status": "dry_run" if args.dry_run else "pending",
        "checked_at_utc": utc_now(),
        "job_count": len(jobs),
        "metric_count": len(payload[0]["metrics"]),
        "safe_metadata_only": True,
    }
    if args.dry_run:
        report["payload"] = payload
        write_report(args.output, report)
        print(json.dumps(report, sort_keys=True))
        return 0

    license_key = os.environ.get("NEW_RELIC_LICENSE_KEY", "").strip()
    if not license_key:
        report.update({"status": "fail", "reason": "missing_new_relic_license_key"})
        write_report(args.output, report)
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 1
    try:
        result = post_payload(payload, metric_endpoint(), license_key)
    except (ValueError, urllib.error.URLError, TimeoutError) as exc:
        report.update({"status": "fail", "reason": exc.__class__.__name__})
        write_report(args.output, report)
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 1
    report.update({"status": "pass", "status_code": result["status_code"]})
    write_report(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_args())
