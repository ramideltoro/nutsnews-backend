#!/usr/bin/env python3
"""Run fixed deployment-safety gates for backend-changing workflows."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from scripts import backend_controlled_maintenance as maintenance
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    import backend_controlled_maintenance as maintenance  # type: ignore[no-redef]


REMOTE_SAFETY_COMMANDS: dict[str, str] = {
    "caddy_config": (
        "if command -v caddy >/dev/null 2>&1 && test -f /etc/caddy/Caddyfile; then "
        "(sudo -n caddy validate --config /etc/caddy/Caddyfile 2>&1 || caddy validate --config /etc/caddy/Caddyfile 2>&1); "
        "else echo not_configured; fi"
    ),
    "docker_health": (
        "if systemctl is-active docker >/dev/null 2>&1; then "
        "sudo -n docker info --format '{{.ServerVersion}}' 2>/dev/null || docker info --format '{{.ServerVersion}}' 2>/dev/null || true; "
        "else echo not_configured; fi"
    ),
    "worker_runtime_observability": (
        "if test -x /usr/local/sbin/nutsnews-worker-runtime "
        "&& test -f /etc/nutsnews-worker-uplift/services.json "
        "&& test -f /opt/nutsnews-worker-uplift/compose.yml; then "
        "sudo -n /usr/local/sbin/nutsnews-worker-runtime status "
        "--manifest /etc/nutsnews-worker-uplift/services.json "
        "--compose /opt/nutsnews-worker-uplift/compose.yml "
        "--project nutsnews-worker-uplift --dry-run; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_health": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1; then "
        "if sudo -n docker exec nutsnews-rabbitmq rabbitmq-diagnostics -q ping >/dev/null 2>&1; then "
        "echo healthy; else echo critical; fi; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_network_security": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        "&& test -x /usr/local/sbin/nutsnews-rabbitmq-network-check; then "
        "sudo -n /usr/local/sbin/nutsnews-rabbitmq-network-check "
        "--env /etc/nutsnews-rabbitmq/rabbitmq.env "
        "--topology-env /etc/nutsnews-rabbitmq/topology.env; "
        "else echo not_configured; fi"
    ),
    "rabbitmq_drift": (
        "if systemctl is-active nutsnews-rabbitmq >/dev/null 2>&1 "
        "&& test -x /usr/local/sbin/nutsnews-rabbitmq-probe; then "
        "sudo -n /usr/local/sbin/nutsnews-rabbitmq-probe drift "
        "--env /etc/nutsnews-rabbitmq/rabbitmq.env "
        "--credentials-env /etc/nutsnews-rabbitmq/topology.env "
        "--definition /etc/nutsnews-rabbitmq/worker-uplift-topology.json "
        "--metadata /var/lib/nutsnews/rabbitmq-probes/apply-metadata.json; "
        "else echo not_configured; fi"
    ),
    "restore_verification": (
        "test -s /var/lib/nutsnews/backups/last-restore-verification.json "
        "&& cat /var/lib/nutsnews/backups/last-restore-verification.json || echo not_configured"
    ),
}
RABBITMQ_PUBLIC_PORTS = (5672, 15672, 15692)
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
WORKER_SERVICE_REGEX = "|".join(WORKER_SERVICES)

PROFILES = {"baseline_apply", "cloudflare_dns_apply", "cloudflare_dns_rollback"}
CRITICAL_IF_NOT_HEALTHY: dict[str, set[str]] = {
    "baseline_apply": {
        "failed_systemd_units",
        "kernel_alignment",
        "root_disk_pressure",
        "root_inode_pressure",
        "service_ssh",
        "service_ufw",
        "service_caddy",
        "reverse_proxy_health",
        "public_endpoint_health",
        "caddy_config",
        "reboot_required",
        "secret_presence",
    },
    "cloudflare_dns_apply": {
        "failed_systemd_units",
        "root_disk_pressure",
        "root_inode_pressure",
        "service_caddy",
        "reverse_proxy_health",
        "public_endpoint_health",
        "caddy_config",
        "secret_presence",
    },
    "cloudflare_dns_rollback": {
        "failed_systemd_units",
        "root_disk_pressure",
        "root_inode_pressure",
        "secret_presence",
    },
}


def bool_arg(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def public_endpoint_health(url: str, expected_body: str, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = maintenance.redact(response.read(256).decode("utf-8", errors="replace")).strip()
            status_code = response.status
    except urllib.error.HTTPError as exc:
        body = maintenance.redact(exc.read(256).decode("utf-8", errors="replace")).strip()
        return {
            "name": "public_endpoint_health",
            "status": "critical",
            "summary": f"http_status={exc.code} body={body or 'empty'}",
        }
    except Exception as exc:
        return {
            "name": "public_endpoint_health",
            "status": "critical",
            "summary": maintenance.redact(str(exc)),
        }

    healthy = status_code == 200 and body == expected_body
    return {
        "name": "public_endpoint_health",
        "status": "healthy" if healthy else "critical",
        "summary": f"http_status={status_code} body={body or 'empty'}",
    }


def rabbitmq_network_security(evidence: dict[str, Any]) -> dict[str, Any]:
    output = maintenance.command_stdout(evidence, "rabbitmq_network_security").strip()
    if output == "not_configured":
        return {"name": "rabbitmq_network_security", "status": "not_configured", "summary": "not_configured"}
    if output.startswith("{"):
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return {"name": "rabbitmq_network_security", "status": "unknown", "summary": "network check json invalid"}
        status = "healthy" if data.get("status") == "pass" else "critical"
        failed = data.get("failed_checks") if isinstance(data.get("failed_checks"), list) else []
        return {
            "name": "rabbitmq_network_security",
            "status": status,
            "summary": "failed_checks=none" if status == "healthy" else f"failed_checks={','.join(str(item) for item in failed) or 'unknown'}",
        }
    if output:
        return {"name": "rabbitmq_network_security", "status": "unknown", "summary": output.splitlines()[-1]}
    return {"name": "rabbitmq_network_security", "status": "unknown", "summary": "unknown"}


def rabbitmq_drift(evidence: dict[str, Any]) -> dict[str, Any]:
    output = maintenance.command_stdout(evidence, "rabbitmq_drift").strip()
    if output == "not_configured":
        return {"name": "rabbitmq_drift", "status": "not_configured", "summary": "not_configured"}
    if output.startswith("{"):
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return {"name": "rabbitmq_drift", "status": "unknown", "summary": "drift json invalid"}
        status = "healthy" if data.get("status") == "pass" else "critical"
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        blockers = summary.get("high_priority_unexpected") if isinstance(summary, dict) else []
        return {
            "name": "rabbitmq_drift",
            "status": status,
            "summary": "high_priority_unexpected=none" if status == "healthy" else f"high_priority_unexpected={','.join(str(item) for item in blockers) or 'unknown'}",
        }
    if output:
        return {"name": "rabbitmq_drift", "status": "unknown", "summary": output.splitlines()[-1]}
    return {"name": "rabbitmq_drift", "status": "unknown", "summary": "unknown"}


def worker_runtime_observability(evidence: dict[str, Any]) -> dict[str, Any]:
    output = maintenance.command_stdout(evidence, "worker_runtime_observability").strip()
    if output == "not_configured":
        return {
            "name": "worker_runtime_observability",
            "status": "not_configured",
            "summary": "not_configured",
        }
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {
            "name": "worker_runtime_observability",
            "status": "unknown",
            "summary": "worker runtime status json invalid",
        }
    services = data.get("services")
    expected_services = {
        "scheduler",
        "fetcher",
        "canonicalizer",
        "enrichment",
        "approval",
        "translation",
        "persistence",
        "publication",
    }
    complete = isinstance(services, dict) and set(services) == expected_services
    unhealthy_liveness = data.get("unhealthy_liveness")
    unhealthy_metrics = data.get("unhealthy_metrics")
    unhealthy_readiness = data.get("unhealthy_readiness")
    expected_active = data.get("expected_active")
    healthy = bool(
        data.get("status") == "pass"
        and complete
        and unhealthy_liveness == []
        and unhealthy_metrics == []
        and isinstance(expected_active, bool)
        and (expected_active is False or unhealthy_readiness == [])
    )
    return {
        "name": "worker_runtime_observability",
        "status": "healthy" if healthy else "critical",
        "summary": (
            f"service_count={len(services) if isinstance(services, dict) else 0} "
            f"expected_active={str(expected_active).lower() if isinstance(expected_active, bool) else 'unknown'} "
            f"liveness_failures={len(unhealthy_liveness) if isinstance(unhealthy_liveness, list) else 'unknown'} "
            f"metrics_failures={len(unhealthy_metrics) if isinstance(unhealthy_metrics, list) else 'unknown'} "
            f"readiness_failures={len(unhealthy_readiness) if isinstance(unhealthy_readiness, list) else 'unknown'}"
        ),
    }


def derive_prometheus_query_url(remote_write_url: str) -> str:
    if not remote_write_url:
        return ""
    parsed = urllib.parse.urlparse(remote_write_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/prom/push"):
        path = path[: -len("/api/prom/push")] + "/api/prom/api/v1/query"
    elif path.endswith("/api/v1/push"):
        path = path[: -len("/api/v1/push")] + "/api/v1/query"
    elif not path.endswith("/api/v1/query"):
        path = path + "/api/v1/query"
    return urllib.parse.urlunparse(
        parsed._replace(path=path, query="", params="", fragment="")
    )


def worker_runtime_grafana_expression() -> str:
    worker_selector = (
        'job="nutsnews-worker-uplift",instance="backend.nutsnews.com",'
        'environment="production",deployment_environment="production",'
        f'host="backend.nutsnews.com",service=~"{WORKER_SERVICE_REGEX}"'
    )
    host_selector = (
        'job="nutsnews-backend-host",instance="backend.nutsnews.com",'
        'service_namespace="nutsnews",service="host",environment="production",'
        'deployment_environment="production",host="backend.nutsnews.com"'
    )
    return (
        "("
        f'(count(count by (service) (up{{{worker_selector}}} == 1)) == bool 8) + '
        f'(count(count by (service) ((time() - timestamp(up{{{worker_selector}}})) < 180)) == bool 8) + '
        "((max(nutsnews_backend_worker_uplift_deployed_identity_available"
        f'{{{host_selector}}}) or vector(0)) == bool 1) + '
        "(count(count by (worker_service) "
        "(nutsnews_backend_worker_uplift_deployed_service_info"
        f'{{{host_selector}}} == 1)) == bool 8)'
        ")"
    )


def prometheus_scalar_query(
    remote_write_url: str,
    username: str,
    password: str,
    expression: str,
    timeout: int,
) -> float | None:
    query_url = derive_prometheus_query_url(remote_write_url)
    if not query_url or not username or not password:
        return None
    body = urllib.parse.urlencode({"query": expression}).encode("utf-8")
    req = urllib.request.Request(query_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=min(max(timeout, 1), 5)) as response:
            payload = response.read(65_537)
            if len(payload) > 65_536:
                return None
            data = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return None
    data_block = data.get("data") if isinstance(data, dict) else None
    result = data_block.get("result") if isinstance(data_block, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("status") != "success"
        or not isinstance(data_block, dict)
        or data_block.get("resultType") != "vector"
        or not isinstance(result, list)
        or len(result) != 1
        or not isinstance(result[0], dict)
        or result[0].get("metric") != {}
    ):
        return None
    value = result[0].get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        parsed = float(value[1])
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def worker_runtime_grafana_observability(
    args: argparse.Namespace,
    *,
    attempts: int = 4,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    if (
        getattr(args, "profile", "") != "baseline_apply"
        or getattr(args, "phase", "") != "post"
        or not worker_runtime_enabled_for_gate()
    ):
        return {
            "name": "worker_runtime_grafana_observability",
            "status": "not_configured",
            "summary": "post-apply worker runtime Grafana gate disabled",
        }
    remote_write_url = os.environ.get("GRAFANA_CLOUD_PROMETHEUS_URL", "").strip()
    username = os.environ.get("GRAFANA_CLOUD_PROMETHEUS_USERNAME", "").strip()
    password = os.environ.get("GRAFANA_CLOUD_PROMETHEUS_PASSWORD", "").strip()
    if not remote_write_url or not username or not password:
        return {
            "name": "worker_runtime_grafana_observability",
            "status": "critical",
            "summary": "Grafana Cloud Prometheus query credentials missing",
        }
    value: float | None = None
    bounded_attempts = min(max(attempts, 1), 4)
    for attempt in range(bounded_attempts):
        value = prometheus_scalar_query(
            remote_write_url,
            username,
            password,
            worker_runtime_grafana_expression(),
            getattr(args, "timeout", 15),
        )
        if value == 4.0:
            break
        if attempt + 1 < bounded_attempts:
            sleeper(10)
    passed_checks = int(value) if value is not None and value.is_integer() else 0
    return {
        "name": "worker_runtime_grafana_observability",
        "status": "healthy" if value == 4.0 else "critical",
        "summary": (
            f"passed_contract_checks={passed_checks}/4 attempts={bounded_attempts}; "
            "requires all eight up, fresh, and exact deployed identity series"
        ),
    }


def rabbitmq_public_exposure(host: str, timeout: int) -> dict[str, Any]:
    if not rabbitmq_enabled_for_gate():
        return {"name": "rabbitmq_public_exposure", "status": "not_configured", "summary": "rabbitmq gate disabled"}
    open_ports: list[int] = []
    closed_ports: list[int] = []
    errors: dict[str, str] = {}
    connect_timeout = min(max(timeout, 1), 5)
    for port in RABBITMQ_PUBLIC_PORTS:
        try:
            with socket.create_connection((host, port), timeout=connect_timeout):
                open_ports.append(port)
        except OSError as exc:
            closed_ports.append(port)
            errors[str(port)] = exc.__class__.__name__
    status = "healthy" if not open_ports else "critical"
    return {
        "name": "rabbitmq_public_exposure",
        "status": status,
        "summary": f"open={open_ports or 'none'} closed_count={len(closed_ports)}",
        "checked_ports": list(RABBITMQ_PUBLIC_PORTS),
        "open_ports": open_ports,
        "closed_ports": closed_ports,
        "closed_reasons": errors,
    }


def collect_evidence(args: argparse.Namespace) -> dict[str, Any]:
    evidence = maintenance.collect_live(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, args.timeout)
    for name, command in REMOTE_SAFETY_COMMANDS.items():
        evidence["commands"][name] = maintenance.run_ssh_command(
            args.ssh_host,
            args.ssh_user,
            args.ssh_key,
            args.known_hosts,
            command,
            args.timeout,
        )
    return evidence


def secret_presence_checks(required_names: list[str]) -> list[dict[str, Any]]:
    missing = [name for name in required_names if not os.environ.get(name, "").strip()]
    return [
        {
            "name": "secret_presence",
            "status": "healthy" if not missing else "critical",
            "summary": "all required secret names present" if not missing else "missing required secret names",
            "checked_names": sorted(required_names),
            "missing_names": sorted(missing),
        }
    ]


def safety_checks(args: argparse.Namespace, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks = maintenance.classify_prechecks(evidence)

    caddy_output = maintenance.command_stdout(evidence, "caddy_config").strip()
    if caddy_output == "not_configured":
        caddy_status = "not_configured"
    elif "Valid configuration" in caddy_output or "configuration is valid" in caddy_output.lower():
        caddy_status = "healthy"
    elif caddy_output:
        caddy_status = "critical"
    else:
        caddy_status = "unknown"
    checks.append({"name": "caddy_config", "status": caddy_status, "summary": caddy_output.splitlines()[-1] if caddy_output else "unknown"})

    docker_output = maintenance.command_stdout(evidence, "docker_health").strip()
    if docker_output == "not_configured":
        docker_status = "not_configured"
    elif docker_output:
        docker_status = "healthy"
    else:
        docker_status = "unknown"
    checks.append({"name": "docker_health", "status": docker_status, "summary": docker_output or "unknown"})
    checks.append(worker_runtime_observability(evidence))
    checks.append(worker_runtime_grafana_observability(args))

    rabbitmq_output = maintenance.command_stdout(evidence, "rabbitmq_health").strip()
    if rabbitmq_output == "healthy":
        rabbitmq_status = "healthy"
    elif rabbitmq_output == "not_configured":
        rabbitmq_status = "not_configured"
    elif rabbitmq_output == "critical":
        rabbitmq_status = "critical"
    elif rabbitmq_output:
        rabbitmq_status = "unknown"
    else:
        rabbitmq_status = "unknown"
    checks.append({"name": "rabbitmq_health", "status": rabbitmq_status, "summary": f"rabbitmq={rabbitmq_output or 'unknown'}"})
    checks.append(rabbitmq_network_security(evidence))
    checks.append(rabbitmq_drift(evidence))

    restore_output = maintenance.command_stdout(evidence, "restore_verification").strip()
    restore_status = "not_configured"
    if restore_output.startswith("{"):
        try:
            restore_data = json.loads(restore_output)
            restore_status = str(restore_data.get("status") or "unknown")
        except json.JSONDecodeError:
            restore_status = "unknown"
    elif restore_output != "not_configured":
        restore_status = "unknown"
    checks.append(
        {
            "name": "restore_verification",
            "status": restore_status if restore_status in maintenance.VALID_STATUSES else "unknown",
            "summary": "restore drill status file" if restore_output.startswith("{") else restore_output or "unknown",
        }
    )

    checks.append(public_endpoint_health(args.public_health_url, args.expected_public_health_body, args.timeout))
    checks.append(rabbitmq_public_exposure(getattr(args, "ssh_host", os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18")), args.timeout))
    checks.extend(secret_presence_checks(args.required_secret))
    return checks


def blockers(profile: str, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    critical_names = CRITICAL_IF_NOT_HEALTHY[profile]
    result: list[dict[str, str]] = []
    for check in checks:
        name = check["name"]
        status = check["status"]
        if name not in critical_names:
            continue
        if status != "healthy":
            result.append({"check": name, "status": status})
    missing = sorted(critical_names - {check["name"] for check in checks})
    result.extend({"check": name, "status": "missing"} for name in missing)
    return result


def rabbitmq_enabled_for_gate() -> bool:
    return os.environ.get("NUTSNEWS_BACKEND_RABBITMQ_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def worker_runtime_enabled_for_gate() -> bool:
    return os.environ.get("NUTSNEWS_BACKEND_WORKER_RUNTIME_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def rabbitmq_post_apply_blockers(args: argparse.Namespace, checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    if args.profile != "baseline_apply" or args.phase != "post" or not rabbitmq_enabled_for_gate():
        return []
    by_name = {check["name"]: check for check in checks}
    result: list[dict[str, str]] = []
    for name in ("docker_health", "rabbitmq_health", "rabbitmq_network_security", "rabbitmq_drift", "rabbitmq_public_exposure"):
        status = by_name.get(name, {}).get("status", "missing")
        if status != "healthy":
            result.append({"check": name, "status": status})
    return result


def worker_runtime_post_apply_blockers(
    args: argparse.Namespace,
    checks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if args.profile != "baseline_apply" or args.phase != "post" or not worker_runtime_enabled_for_gate():
        return []
    by_name = {str(check.get("name")): check for check in checks}
    result: list[dict[str, str]] = []
    for name in (
        "worker_runtime_observability",
        "worker_runtime_grafana_observability",
    ):
        status = str(by_name.get(name, {}).get("status", "missing"))
        if status != "healthy":
            result.append({"check": name, "status": status})
    return result


def report(args: argparse.Namespace, evidence: dict[str, Any]) -> dict[str, Any]:
    checks = safety_checks(args, evidence)
    gate_blockers = blockers(args.profile, checks)
    gate_blockers.extend(rabbitmq_post_apply_blockers(args, checks))
    gate_blockers.extend(worker_runtime_post_apply_blockers(args, checks))
    enforced = args.enforce
    return {
        "version": 1,
        "generated_at_utc": maintenance.utc_now(),
        "profile": args.profile,
        "phase": args.phase,
        "enforced": enforced,
        "status": "fail" if enforced and gate_blockers else "pass",
        "change_description": args.change_description,
        "target": {"host": args.ssh_host, "user": args.ssh_user},
        "summary": maintenance.summarize_checks(checks),
        "blockers": gate_blockers,
        "checks": checks,
        "secret_handling": "presence and shape by name only; secret values are never emitted",
        "remediation": "use the fixed workflow rollback/recovery path documented for this profile",
    }


def write_summary(path: Path, gate_report: dict[str, Any]) -> None:
    lines = [
        "# Backend Deployment Safety Gate",
        "",
        f"- Profile: `{gate_report['profile']}`",
        f"- Phase: `{gate_report['phase']}`",
        f"- Enforced: `{gate_report['enforced']}`",
        f"- Status: `{gate_report['status']}`",
        f"- Change: `{gate_report['change_description']}`",
        "",
        "## Blockers",
        "",
    ]
    if gate_report["blockers"]:
        for blocker in gate_report["blockers"]:
            lines.append(f"- `{blocker['check']}` is `{blocker['status']}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Checks", "", "| Check | Status | Summary |", "| --- | --- | --- |"])
    for check in gate_report["checks"]:
        lines.append(f"| `{check['name']}` | `{check['status']}` | {check['summary']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--phase", choices=("pre", "post", "dry-run"), required=True)
    parser.add_argument("--enforce", type=bool_arg, default=True)
    parser.add_argument("--change-description", required=True)
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--public-health-url", default="https://backend.nutsnews.com/healthz")
    parser.add_argument("--expected-public-health-body", default="ok")
    parser.add_argument("--required-secret", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    gate_report = report(args, collect_evidence(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(gate_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, gate_report)
    print(json.dumps({"status": gate_report["status"], "blockers": gate_report["blockers"]}, indent=2))
    return 1 if gate_report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
