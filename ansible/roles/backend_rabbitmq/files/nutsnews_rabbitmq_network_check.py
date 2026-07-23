#!/usr/bin/env python3
"""Verify RabbitMQ network exposure and management access posture.

This is a read-only host check. It inspects listeners, UFW state, Docker port
publishing, local loopback access, anonymous management access, RabbitMQ users,
and topology credential uniqueness without printing secret values.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_PORTS = {
    "amqp": 5672,
    "management": 15672,
    "prometheus": 15692,
}
LOOPBACK_MANAGEMENT_URL = "http://127.0.0.1:15672"
LOOPBACK_PROMETHEUS_URL = "http://127.0.0.1:15692/metrics"
BREAK_GLASS_PREFIX = "RABBITMQ_BREAK_GLASS_ADMIN_"


def run_command(argv: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def split_host_port(value: str) -> tuple[str, int | None]:
    local = value.strip()
    if local.startswith("["):
        end = local.rfind("]:")
        if end != -1:
            host = local[1:end]
            port_text = local[end + 2 :]
            return host, int(port_text) if port_text.isdigit() else None
    host, separator, port_text = local.rpartition(":")
    if not separator or not port_text.isdigit():
        return local, None
    return host or "*", int(port_text)


def parse_ss_listeners(output: str, ports: set[int]) -> dict[int, list[str]]:
    listeners: dict[int, list[str]] = {port: [] for port in ports}
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "LISTEN" or len(parts) < 4:
            continue
        host, port = split_host_port(parts[3])
        if port in listeners:
            listeners[port].append(host)
    return {port: sorted(set(hosts)) for port, hosts in listeners.items()}


def is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").split("%", 1)[0]
    if normalized == "localhost":
        return True
    if normalized in {"", "*", "0.0.0.0", "::"}:
        return False
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def check_host_listeners(ports: dict[str, int]) -> dict[str, Any]:
    completed = run_command(["ss", "-ltnH"])
    if completed.returncode != 0:
        return {
            "name": "host_listeners",
            "status": "fail",
            "summary": "ss listener inspection failed",
            "stderr": completed.stderr.strip()[:500],
        }
    by_port = parse_ss_listeners(completed.stdout, set(ports.values()))
    missing = [name for name, port in ports.items() if not by_port.get(port)]
    public = {
        name: [host for host in by_port.get(port, []) if not is_loopback_host(host)]
        for name, port in ports.items()
    }
    public = {name: hosts for name, hosts in public.items() if hosts}
    status = "pass" if not missing and not public else "fail"
    return {
        "name": "host_listeners",
        "status": status,
        "summary": "RabbitMQ host listeners are loopback-only" if status == "pass" else "RabbitMQ host listener drift detected",
        "expected": {name: f"127.0.0.1:{port}" for name, port in ports.items()},
        "observed": {name: by_port.get(port, []) for name, port in ports.items()},
        "missing": missing,
        "non_loopback": public,
    }


def check_ufw(ports: dict[str, int]) -> dict[str, Any]:
    completed = run_command(["ufw", "status", "verbose"])
    output = completed.stdout
    if completed.returncode != 0:
        return {
            "name": "ufw_rabbitmq_rules",
            "status": "fail",
            "summary": "ufw status inspection failed",
            "stderr": completed.stderr.strip()[:500],
        }
    active = bool(re.search(r"^Status:\s+active$", output, re.MULTILINE))
    default_deny = "Default: deny (incoming)" in output
    allow_rules: list[str] = []
    for line in output.splitlines():
        for port in ports.values():
            if re.search(rf"(^|\s){port}(?:/tcp)?(?:\s|\(v6\)).*\bALLOW\b", line):
                allow_rules.append(line.strip())
    status = "pass" if active and default_deny and not allow_rules else "fail"
    return {
        "name": "ufw_rabbitmq_rules",
        "status": status,
        "summary": "UFW denies inbound traffic and has no RabbitMQ allow rules" if status == "pass" else "RabbitMQ firewall drift detected",
        "active": active,
        "default_deny_incoming": default_deny,
        "rabbitmq_allow_rules": allow_rules,
    }


def parse_docker_ports(output: str) -> dict[str, list[dict[str, str]]]:
    data = json.loads(output or "{}")
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for key, bindings in data.items():
        if not isinstance(bindings, list):
            continue
        safe_bindings: list[dict[str, str]] = []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            safe_bindings.append(
                {
                    "HostIp": str(binding.get("HostIp", "")),
                    "HostPort": str(binding.get("HostPort", "")),
                }
            )
        result[str(key)] = safe_bindings
    return result


def check_docker_publish(container_name: str, ports: dict[str, int]) -> dict[str, Any]:
    completed = run_command(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_name])
    if completed.returncode != 0:
        return {
            "name": "docker_private_publish",
            "status": "fail",
            "summary": "Docker published-port inspection failed",
            "stderr": completed.stderr.strip()[:500],
        }
    published = parse_docker_ports(completed.stdout.strip())
    missing: list[str] = []
    non_loopback: dict[str, list[dict[str, str]]] = {}
    mismatched_ports: dict[str, list[dict[str, str]]] = {}
    for name, host_port in ports.items():
        container_key = f"{host_port}/tcp"
        bindings = published.get(container_key, [])
        if not bindings:
            missing.append(name)
            continue
        bad_hosts = [binding for binding in bindings if not is_loopback_host(binding.get("HostIp", ""))]
        bad_ports = [binding for binding in bindings if binding.get("HostPort") != str(host_port)]
        if bad_hosts:
            non_loopback[name] = bad_hosts
        if bad_ports:
            mismatched_ports[name] = bad_ports
    status = "pass" if not missing and not non_loopback and not mismatched_ports else "fail"
    return {
        "name": "docker_private_publish",
        "status": status,
        "summary": "Docker publishes RabbitMQ ports only to loopback" if status == "pass" else "Docker RabbitMQ port publishing drift detected",
        "published": {f"{port}/tcp": published.get(f"{port}/tcp", []) for port in ports.values()},
        "missing": missing,
        "non_loopback": non_loopback,
        "mismatched_ports": mismatched_ports,
    }


def parse_docker_networks(output: str) -> dict[str, dict[str, str]]:
    data = json.loads(output or "{}")
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for name, detail in data.items():
        if not isinstance(detail, dict):
            continue
        result[str(name)] = {
            "IPAddress": str(detail.get("IPAddress", "")),
            "Gateway": str(detail.get("Gateway", "")),
        }
    return result


def check_docker_networks(container_name: str) -> dict[str, Any]:
    completed = run_command(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_name])
    if completed.returncode != 0:
        return {
            "name": "docker_private_network",
            "status": "fail",
            "summary": "Docker network inspection failed",
            "stderr": completed.stderr.strip()[:500],
        }
    networks = parse_docker_networks(completed.stdout.strip())
    usable_private_networks = []
    for name, detail in networks.items():
        ip_address = detail.get("IPAddress", "")
        if name == "host" or not ip_address:
            continue
        try:
            private = ipaddress.ip_address(ip_address).is_private
        except ValueError:
            private = False
        if private:
            usable_private_networks.append(name)
    status = "pass" if usable_private_networks else "fail"
    return {
        "name": "docker_private_network",
        "status": status,
        "summary": "RabbitMQ is attached to a private Docker network for colocated service containers"
        if status == "pass"
        else "RabbitMQ private Docker network was not detected",
        "network_names": sorted(networks),
        "private_network_names": sorted(usable_private_networks),
    }


def check_loopback_tcp(ports: dict[str, int], timeout: float) -> dict[str, Any]:
    reachable: list[str] = []
    failures: dict[str, str] = {}
    for name, port in ports.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                reachable.append(name)
        except OSError as exc:
            failures[name] = exc.__class__.__name__
    status = "pass" if len(reachable) == len(ports) else "fail"
    return {
        "name": "loopback_connectivity",
        "status": status,
        "summary": "RabbitMQ loopback ports are reachable from the host" if status == "pass" else "RabbitMQ loopback connectivity failed",
        "reachable": sorted(reachable),
        "failures": failures,
    }


def check_prometheus(url: str, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(16384).decode("utf-8", errors="replace")
            status_code = response.status
    except Exception as exc:
        return {
            "name": "prometheus_loopback",
            "status": "fail",
            "summary": f"RabbitMQ Prometheus endpoint was not reachable: {exc.__class__.__name__}",
        }
    has_metrics = status_code == 200 and ("rabbitmq_" in body or "erlang_" in body)
    return {
        "name": "prometheus_loopback",
        "status": "pass" if has_metrics else "fail",
        "summary": "RabbitMQ Prometheus metrics are reachable over loopback" if has_metrics else f"RabbitMQ Prometheus endpoint returned HTTP {status_code} without expected metrics",
        "http_status": status_code,
    }


def check_anonymous_management(base_url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/overview", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except Exception as exc:
        return {
            "name": "anonymous_management_access",
            "status": "fail",
            "summary": f"anonymous management request failed before authorization check: {exc.__class__.__name__}",
        }
    denied = status_code in {401, 403}
    return {
        "name": "anonymous_management_access",
        "status": "pass" if denied else "fail",
        "summary": "RabbitMQ management API denies anonymous requests" if denied else f"RabbitMQ management API accepted anonymous request with HTTP {status_code}",
        "http_status": status_code,
    }


def parse_rabbitmq_users(output: str) -> list[str]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        users: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("listing users") or stripped.lower().startswith("user"):
                continue
            users.append(stripped.split()[0])
        return users
    if not isinstance(data, list):
        return []
    users = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("user") or item.get("name")
        if name:
            users.append(str(name))
    return users


def check_guest_user(container_name: str) -> dict[str, Any]:
    completed = run_command(["docker", "exec", container_name, "rabbitmqctl", "list_users", "--formatter", "json"], timeout=20)
    if completed.returncode != 0:
        return {
            "name": "guest_user_absent",
            "status": "fail",
            "summary": "RabbitMQ user inspection failed",
            "stderr": completed.stderr.strip()[:500],
        }
    users = parse_rabbitmq_users(completed.stdout)
    status = "pass" if "guest" not in users else "fail"
    return {
        "name": "guest_user_absent",
        "status": status,
        "summary": "RabbitMQ default guest user is absent" if status == "pass" else "RabbitMQ default guest user is still present",
        "user_count": len(users),
        "guest_present": "guest" in users,
    }


def duplicate_env_key_groups(entries: list[tuple[str, str]]) -> list[list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for key, value in entries:
        grouped[value].append(key)
    return sorted(sorted(keys) for value, keys in grouped.items() if value and len(keys) > 1)


def check_topology_credentials(env_path: Path, admin_env_path: Path | None) -> dict[str, Any]:
    try:
        values = parse_env(env_path)
        admin_values = parse_env(admin_env_path) if admin_env_path else {}
    except OSError as exc:
        return {
            "name": "topology_credential_separation",
            "status": "fail",
            "summary": f"RabbitMQ topology credential env could not be read: {exc.__class__.__name__}",
        }
    service_usernames = [
        (key, value)
        for key, value in values.items()
        if key.endswith("_USERNAME") and not key.startswith(BREAK_GLASS_PREFIX)
    ]
    service_passwords = [
        (key, value)
        for key, value in values.items()
        if key.endswith("_PASSWORD") and not key.startswith(BREAK_GLASS_PREFIX)
    ]
    blank_keys = sorted(key for key, value in service_usernames + service_passwords if not value)
    duplicate_usernames = duplicate_env_key_groups(service_usernames)
    duplicate_passwords = duplicate_env_key_groups(service_passwords)
    admin_username = admin_values.get("RABBITMQ_DEFAULT_USER", "")
    admin_password = admin_values.get("RABBITMQ_DEFAULT_PASS", "")
    service_uses_admin_username = sorted(key for key, value in service_usernames if value == admin_username)
    service_uses_admin_password = sorted(key for key, value in service_passwords if value == admin_password)
    service_uses_guest = sorted(key for key, value in service_usernames if value == "guest")
    problems = [
        *blank_keys,
        *service_uses_guest,
        *service_uses_admin_username,
        *service_uses_admin_password,
        *(key for group in duplicate_usernames for key in group),
        *(key for group in duplicate_passwords for key in group),
    ]
    status = "pass" if not problems else "fail"
    return {
        "name": "topology_credential_separation",
        "status": status,
        "summary": "RabbitMQ service identities have distinct non-admin credentials" if status == "pass" else "RabbitMQ service identity credential separation drift detected",
        "service_username_count": len(service_usernames),
        "service_password_count": len(service_passwords),
        "blank_keys": blank_keys,
        "duplicate_username_key_groups": duplicate_usernames,
        "duplicate_password_key_groups": duplicate_passwords,
        "service_username_reuses_admin_key_names": service_uses_admin_username,
        "service_password_reuses_admin_key_names": service_uses_admin_password,
        "service_username_uses_guest_key_names": service_uses_guest,
    }


def check_tls_posture(listener_check: dict[str, Any], docker_check: dict[str, Any]) -> dict[str, Any]:
    listener_non_loopback = listener_check.get("non_loopback", {})
    docker_non_loopback = docker_check.get("non_loopback", {})
    tls_required = bool(listener_non_loopback or docker_non_loopback)
    status = "fail" if tls_required else "pass"
    return {
        "name": "tls_boundary_posture",
        "status": status,
        "summary": "No host-trust-boundary RabbitMQ connection exists; TLS is not required for loopback-only exposure"
        if status == "pass"
        else "RabbitMQ has a non-loopback exposure and must use TLS with reviewed certificate rotation",
        "tls_required": tls_required,
        "listener_non_loopback": listener_non_loopback,
        "docker_non_loopback": docker_non_loopback,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    ports = {
        "amqp": args.amqp_port,
        "management": args.management_port,
        "prometheus": args.prometheus_port,
    }
    checks: list[dict[str, Any]] = []
    listener_check = check_host_listeners(ports)
    docker_check = check_docker_publish(args.container_name, ports)
    docker_network_check = check_docker_networks(args.container_name)
    checks.extend(
        [
            listener_check,
            docker_check,
            docker_network_check,
            check_ufw(ports),
            check_loopback_tcp(ports, args.connect_timeout),
            check_prometheus(args.prometheus_url, args.timeout_seconds),
            check_anonymous_management(args.management_url, args.timeout_seconds),
            check_guest_user(args.container_name),
            check_topology_credentials(args.topology_env, args.env),
            check_tls_posture(listener_check, docker_check),
        ]
    )
    failed = [check for check in checks if check["status"] != "pass"]
    return {
        "status": "fail" if failed else "pass",
        "ports": ports,
        "container": args.container_name,
        "checks": checks,
        "failed_checks": [check["name"] for check in failed],
        "secret_handling": "credentials are inspected only for presence/uniqueness and secret values are never emitted",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-name", default="nutsnews-rabbitmq")
    parser.add_argument("--env", type=Path, default=Path("/etc/nutsnews-rabbitmq/rabbitmq.env"))
    parser.add_argument("--topology-env", type=Path, default=Path("/etc/nutsnews-rabbitmq/topology.env"))
    parser.add_argument("--management-url", default=LOOPBACK_MANAGEMENT_URL)
    parser.add_argument("--prometheus-url", default=LOOPBACK_PROMETHEUS_URL)
    parser.add_argument("--amqp-port", type=int, default=DEFAULT_PORTS["amqp"])
    parser.add_argument("--management-port", type=int, default=DEFAULT_PORTS["management"])
    parser.add_argument("--prometheus-port", type=int, default=DEFAULT_PORTS["prometheus"])
    parser.add_argument("--connect-timeout", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    try:
        report = build_report(parse_args())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        report = {
            "status": "fail",
            "failed_checks": ["unexpected_error"],
            "error": exc.__class__.__name__,
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
