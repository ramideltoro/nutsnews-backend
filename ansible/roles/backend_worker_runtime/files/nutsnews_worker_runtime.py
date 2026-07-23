#!/usr/bin/env python3
"""Fixed-command worker-uplift runtime manager.

The script intentionally exposes a closed action set. It validates the
source-controlled manifest before mutating Docker Compose state and writes
redacted JSON reports for protected workflow artifacts.
"""

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
from urllib import error, parse, request


IMAGE_RE = re.compile(r"^(?P<repo>ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*)@sha256:(?P<digest>[0-9a-f]{64})$")
SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
SECRET_KEY_RE = re.compile(r"(PASSWORD|PASS|TOKEN|SECRET|PRIVATE|KEY|COOKIE)", re.IGNORECASE)
TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)

READ_ONLY_ACTIONS = {"check", "status", "logs", "queue-inspect", "dlq-inspect"}
MUTATING_ACTIONS = {"deploy", "promote", "restart", "scale", "rollback", "dlq-replay", "drain", "reconciliation", "smoke"}
ALL_ACTIONS = sorted(READ_ONLY_ACTIONS | MUTATING_ACTIONS)


class RuntimeErrorWithReport(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    value = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    value = TOKEN_RE.sub("<redacted-token>", value)
    value = URL_SECRET_RE.sub(r"\1<redacted>\3", value)
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeErrorWithReport(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeErrorWithReport(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeErrorWithReport("manifest root must be a JSON object")
    return data


def service_image_parts(image: str) -> tuple[str, str]:
    match = IMAGE_RE.match(image)
    if not match:
        raise ValueError("image must be a lower-case GHCR digest reference: ghcr.io/...@sha256:<64 hex>")
    return match.group("repo"), f"sha256:{match.group('digest')}"


def repository_allowed(repository: str, allowed: list[str]) -> bool:
    return any(repository == item.rstrip("/") or repository.startswith(item) for item in allowed)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if manifest.get("tracking_issue") != 85:
        errors.append("tracking_issue must be 85")
    production_writes_enabled = manifest.get("production_writes_enabled") is True
    cutover_state = manifest.get("cutover_state")
    if manifest.get("mode") != "shadow" and not production_writes_enabled:
        errors.append("runtime mode must default to shadow before protected cutover")
    if cutover_state not in {"shadow", "cutover-approved"}:
        errors.append("cutover_state must be shadow or cutover-approved")
    if production_writes_enabled and cutover_state != "cutover-approved":
        errors.append("production writes require cutover_state=cutover-approved")
    backend_api = manifest.get("backend_api", {})
    if production_writes_enabled and not backend_api.get("writes_enabled"):
        errors.append("production writes require backend_api.writes_enabled=true")

    allowed_repos = manifest.get("allowed_image_repositories", [])
    if not isinstance(allowed_repos, list) or not allowed_repos:
        errors.append("allowed_image_repositories must be a non-empty list")
        allowed_repos = []
    allowed_sources = set(manifest.get("allowed_source_repositories", []))
    allowed_stages = set(manifest.get("allowed_stages", []))
    max_replicas = int(manifest.get("max_replicas_per_service", 0) or 0)
    if max_replicas < 1:
        errors.append("max_replicas_per_service must be at least 1")

    services = manifest.get("services", [])
    if not isinstance(services, list):
        errors.append("services must be a list")
        services = []
    seen_names: set[str] = set()
    for service in services:
        if not isinstance(service, dict):
            errors.append("service entries must be objects")
            continue
        name = str(service.get("name") or "")
        if not SERVICE_RE.match(name):
            errors.append(f"service {name or '<missing>'} has invalid name")
        if name in seen_names:
            errors.append(f"duplicate service name: {name}")
        seen_names.add(name)
        stage = str(service.get("stage") or "")
        if stage not in allowed_stages:
            errors.append(f"service {name} has unsupported stage: {stage}")
        try:
            repository, digest = service_image_parts(str(service.get("image") or ""))
        except ValueError as exc:
            errors.append(f"service {name} image invalid: {exc}")
            repository = ""
            digest = ""
        if repository and not repository_allowed(repository, allowed_repos):
            errors.append(f"service {name} uses untrusted image repository: {repository}")
        if ":" in str(service.get("image") or "").split("@", 1)[0]:
            errors.append(f"service {name} image must not include a mutable tag")
        if service.get("runtime_mode", "shadow") != "shadow":
            errors.append(f"service {name} runtime_mode must remain shadow")
        replicas = int(service.get("replicas", 0) or 0)
        if replicas < 0 or replicas > max_replicas:
            errors.append(f"service {name} replicas must be between 0 and {max_replicas}")
        resources = service.get("resources", {})
        if not isinstance(resources, dict) or not resources.get("memory") or not resources.get("cpus"):
            errors.append(f"service {name} must declare memory and CPU limits")
        if not service.get("healthcheck"):
            errors.append(f"service {name} must declare a healthcheck")
        provenance = service.get("provenance", {})
        if not isinstance(provenance, dict):
            errors.append(f"service {name} provenance must be an object")
            provenance = {}
        if provenance.get("required") is not True:
            errors.append(f"service {name} provenance.required must be true")
        if provenance.get("signed") is not True:
            errors.append(f"service {name} provenance.signed must be true")
        if digest and provenance.get("subject_digest") != digest:
            errors.append(f"service {name} provenance subject_digest must match image digest")
        if provenance.get("source_repository") not in allowed_sources:
            errors.append(f"service {name} provenance source_repository is not allow-listed")
        env = service.get("env", {})
        if not isinstance(env, dict):
            errors.append(f"service {name} env must be an object")
            env = {}
        for key, value in env.items():
            if SECRET_KEY_RE.search(str(key)) and value not in {"", None} and not str(key).endswith("_FILE"):
                errors.append(f"service {name} env key {key} looks secret-bearing; use secret_files or *_FILE")
        for secret in service.get("secret_files", []):
            if not isinstance(secret, dict):
                errors.append(f"service {name} secret file entries must be objects")
                continue
            if not SERVICE_RE.match(str(secret.get("name") or "")):
                errors.append(f"service {name} secret file name is invalid")
            if not str(secret.get("env_key") or "").endswith("_FILE"):
                errors.append(f"service {name} secret file {secret.get('name')} must expose an *_FILE env key")
            if secret.get("value") not in {"", None}:
                errors.append(f"service {name} secret file {secret.get('name')} must not store values in manifest")
            if not str(secret.get("path") or "").startswith("/run/secrets/"):
                errors.append(f"service {name} secret file {secret.get('name')} must mount under /run/secrets")
            if not str(secret.get("host_path") or "").startswith("/etc/nutsnews-worker-uplift/services/"):
                errors.append(f"service {name} secret file {secret.get('name')} must use a root-owned service host_path")
    return errors


def service_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {service["name"]: service for service in manifest.get("services", []) if isinstance(service, dict) and service.get("name")}


def require_service(manifest: dict[str, Any], name: str | None) -> dict[str, Any]:
    if not name:
        raise RuntimeErrorWithReport("service_name is required for this action")
    services = service_map(manifest)
    if name not in services:
        raise RuntimeErrorWithReport(f"unknown service_name: {name}")
    return services[name]


def run_command(argv: list[str], timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    return {
        "argv": argv[:3] + ["<args-redacted>"] if argv and argv[0] == "docker" else argv,
        "returncode": completed.returncode,
        "stdout": redact(completed.stdout),
        "stderr": redact(completed.stderr),
    }


def compose_base(args: argparse.Namespace) -> list[str]:
    return ["docker", "compose", "-f", str(args.compose), "--project-name", args.project]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def rabbitmq_get_json(args: argparse.Namespace, queue: str) -> dict[str, Any]:
    env = read_env(args.rabbitmq_env)
    user = env.get("RABBITMQ_DEFAULT_USER", "")
    password = env.get("RABBITMQ_DEFAULT_PASS", "")
    vhost = env.get("RABBITMQ_DEFAULT_VHOST", args.vhost)
    if not user or not password:
        return {"status": "not_configured", "queue": queue, "summary": "RabbitMQ admin env not readable"}
    path = f"/api/queues/{parse.quote(vhost, safe='')}/{parse.quote(queue, safe='')}"
    url = f"http://127.0.0.1:15672{path}"
    req = request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return {"status": "critical", "queue": queue, "http_status": exc.code, "summary": "RabbitMQ queue API failed"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "unknown", "queue": queue, "summary": f"RabbitMQ queue API unavailable: {type(exc).__name__}"}
    keep = ("name", "vhost", "state", "messages", "messages_ready", "messages_unacknowledged", "consumers", "consumer_capacity")
    return {"status": "healthy", "queue": queue, "metrics": {key: data.get(key) for key in keep if key in data}}


def declared_queues(service: dict[str, Any], kind: str) -> list[str]:
    queues = service.get("queues", {})
    if not isinstance(queues, dict):
        return []
    if kind == "main":
        value = queues.get("main")
        return [value] if isinstance(value, str) else []
    if kind == "retry":
        value = queues.get("retry", [])
        return [item for item in value if isinstance(item, str)]
    if kind == "dlq":
        value = queues.get("dlq")
        return [value] if isinstance(value, str) else []
    return []


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o640)


def build_report(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    report: dict[str, Any] = {
        "status": "pass" if not errors else "fail",
        "action": args.action,
        "service_name": args.service_name,
        "generated_at_utc": utc_now(),
        "tracking_issue": 85,
        "mode": manifest.get("mode"),
        "production_writes_enabled": manifest.get("production_writes_enabled"),
        "errors": errors,
        "commands": [],
    }
    if errors:
        return report

    if args.action == "check":
        return report

    if args.action in MUTATING_ACTIONS and not args.confirm_action:
        report["status"] = "fail"
        report["errors"].append("mutating action requires --confirm-action")
        return report

    if args.action in {"deploy", "promote", "restart", "scale", "rollback", "logs", "queue-inspect", "dlq-inspect", "dlq-replay", "drain", "reconciliation", "smoke"}:
        service = require_service(manifest, args.service_name)
    else:
        service = {}

    if args.action == "status":
        if not manifest.get("services"):
            report["summary"] = "worker runtime framework is installed; no services are configured"
        elif not args.compose.exists():
            report["status"] = "fail"
            report["errors"].append(f"compose file not found: {args.compose}")
        else:
            command = compose_base(args) + ["ps", "--format", "json"]
            report["commands"].append(run_command(command))
    elif args.action == "logs":
        command = compose_base(args) + ["logs", "--no-color", "--tail", str(args.tail), service["name"]]
        report["commands"].append(run_command(command))
    elif args.action == "deploy":
        report["commands"].append(run_command(compose_base(args) + ["pull", service["name"]], timeout=300))
        report["commands"].append(
            run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}={service.get('replicas', 1)}", service["name"]], timeout=300)
        )
    elif args.action == "promote":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["summary"] = "Promotion requires a later backend API protected cutover state and service manifest change."
        if not args.dry_run:
            report["errors"].append("promote requires cutover_state=cutover-approved and production_writes_enabled=true")
    elif args.action == "restart":
        report["commands"].append(run_command(compose_base(args) + ["restart", service["name"]], timeout=180))
    elif args.action == "scale":
        replicas = args.replicas if args.replicas is not None else int(service.get("replicas", 1))
        max_replicas = int(manifest.get("max_replicas_per_service", 1))
        if replicas < 0 or replicas > max_replicas:
            report["status"] = "fail"
            report["errors"].append(f"replicas must be between 0 and {max_replicas}")
        else:
            report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}={replicas}", service["name"]], timeout=300))
    elif args.action == "rollback":
        if not service.get("rollback"):
            report["status"] = "fail"
            report["errors"].append("service rollback metadata is required before rollback")
        else:
            report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", service["name"]], timeout=300))
    elif args.action == "drain":
        report["commands"].append(run_command(compose_base(args) + ["up", "-d", "--no-deps", "--scale", f"{service['name']}=0", service["name"]], timeout=300))
    elif args.action in {"queue-inspect", "dlq-inspect"}:
        kind = "dlq" if args.action == "dlq-inspect" else args.queue_kind
        queues = declared_queues(service, kind)
        report["queues"] = [rabbitmq_get_json(args, queue) for queue in queues]
    elif args.action == "dlq-replay":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["queues"] = declared_queues(service, "dlq")
        report["summary"] = "DLQ replay is framework-gated; service-specific replay requires a later approved replayer image and idempotency proof."
        if not args.dry_run:
            report["errors"].append("dlq-replay currently fails closed unless --dry-run is set")
    elif args.action == "reconciliation":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["summary"] = "Reconciliation command is reserved for stage outbox/watermark services; no legacy checkout is used."
        if not args.dry_run:
            report["errors"].append("reconciliation requires a later approved service-specific reconciler")
    elif args.action == "smoke":
        report["status"] = "dry_run" if args.dry_run else "blocked"
        report["summary"] = "Runtime smoke validates service container health once a service image supplies its smoke contract."
        if not args.dry_run:
            report["errors"].append("smoke requires a later approved service-specific smoke contract")

    if report["commands"] and any(item["returncode"] != 0 for item in report["commands"]):
        report["status"] = "fail"
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage worker-uplift runtime services through fixed commands.")
    parser.add_argument("action", choices=ALL_ACTIONS)
    parser.add_argument("--manifest", type=Path, default=Path("/etc/nutsnews-worker-uplift/services.json"))
    parser.add_argument("--compose", type=Path, default=Path("/opt/nutsnews-worker-uplift/compose.yml"))
    parser.add_argument("--project", default="nutsnews-worker-uplift")
    parser.add_argument("--service-name")
    parser.add_argument("--replicas", type=int)
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--queue-kind", choices=("main", "retry", "dlq"), default="main")
    parser.add_argument("--rabbitmq-env", type=Path, default=Path("/etc/nutsnews-rabbitmq/rabbitmq.env"))
    parser.add_argument("--vhost", default="nutsnews-worker-uplift")
    parser.add_argument("--confirm-action", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = load_json(args.manifest)
        report = build_report(args, manifest)
    except RuntimeErrorWithReport as exc:
        report = {"status": "fail", "action": getattr(args, "action", "unknown"), "generated_at_utc": utc_now(), "errors": [str(exc)], **exc.report}
    except subprocess.TimeoutExpired as exc:
        report = {"status": "fail", "action": args.action, "generated_at_utc": utc_now(), "errors": [f"command timed out: {exc.cmd}"]}

    write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") in {"pass", "healthy", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
