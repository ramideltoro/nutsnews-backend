#!/usr/bin/env python3
"""Daily OpenAI-assisted maintenance scanner for NutsNews repositories.

The robot collects deterministic, redacted evidence first, asks OpenAI for
structured issue candidates, then optionally creates one GitHub issue per
finding. It never mutates servers, fixes code, merges PRs, or closes issues.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCAN_REPOS = (
    "ramideltoro/nutsnews",
    "ramideltoro/nutsnews-backend",
    "ramideltoro/nutsnews-infra",
    "ramideltoro/nutsnews-docs",
)
ROUTING_KEYWORDS = {
    "ramideltoro/nutsnews": ("app", "frontend", "web", "api", "supabase", "worker-facing"),
    "ramideltoro/nutsnews-backend": ("backend", "server", "caddy", "alloy", "grafana", "backup", "healthz"),
    "ramideltoro/nutsnews-infra": ("vps", "gitops", "ansible", "terraform", "opentofu", "docker compose"),
    "ramideltoro/nutsnews-docs": ("docs", "documentation", "runbook", "process"),
}
ROUTING_PRIORITY = (
    "ramideltoro/nutsnews-docs",
    "ramideltoro/nutsnews-infra",
    "ramideltoro/nutsnews",
    "ramideltoro/nutsnews-backend",
)
DEFAULT_TARGET_REPO = "ramideltoro/nutsnews-backend"
DEFAULT_OPENAI_MODEL = "gpt-5.6"
DEFAULT_REASONING_EFFORT = "low"
BODY_FIELDS = (
    "Scan run label",
    "Finding fingerprint",
    "Detected repo",
    "Suggested target repo",
    "Category",
    "Severity",
    "Confidence",
    "Possible noise or false-positive reason",
    "Evidence",
    "Why this matters",
    "Suggested fix or investigation path",
    "Acceptance criteria",
    "Validation ideas",
    "Related files/log queries/checks",
    "Secret-redaction status",
)
USEFUL_LABELS = {
    "bug": "d73a4a",
    "enhancement": "a2eeef",
    "documentation": "0075ca",
    "question": "d876e3",
}

TOKEN_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|"
    r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,})"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
AUTH_HEADER_RE = re.compile(r"(?im)^(authorization|cookie|set-cookie):\s*.+$")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|SESSION|COOKIE|AUTH)[A-Z0-9_]*)\s*[:=]\s*([^\s,;]+)"
)
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?([^\s#]+)")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def scan_label(now: dt.datetime | None = None) -> str:
    return "scan+" + (now or utc_now()).strftime("%Y-%m-%dT%H-%M-%SZ")


def redact_text(value: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    redacted = AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}: <redacted>", redacted)
    redacted = TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = URL_SECRET_RE.sub(r"\1<redacted>\3", redacted)
    redacted = URL_QUERY_RE.sub(r"\1?<redacted-query>", redacted)
    redacted = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_data(item) for key, item in value.items()}
    return value


def run_command(command: list[str], cwd: Path | None = None, timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": completed.returncode,
            "stdout": redact_text(completed.stdout[-8000:]),
            "stderr": redact_text(completed.stderr[-8000:]),
        }
    except Exception as exc:  # pragma: no cover - defensive subprocess path
        return {"returncode": 255, "stdout": "", "stderr": redact_text(str(exc))}


def is_probably_text(path: Path) -> bool:
    if path.is_dir():
        return False
    if path.stat().st_size > 500_000:
        return False
    if path.suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".lockb",
        ".sqlite",
    }:
        return False
    return True


def should_skip_path(path: Path) -> bool:
    skipped = {".git", "node_modules", ".next", "dist", "build", "coverage", ".venv", "__pycache__"}
    return any(part in skipped for part in path.parts)


def collect_action_pinning(repo_path: Path) -> list[dict[str, Any]]:
    findings = []
    workflow_dir = repo_path / ".github" / "workflows"
    if not workflow_dir.exists():
        return findings
    uses_re = re.compile(r"uses:\s*([^\s#]+)")
    for workflow in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            match = uses_re.search(line)
            if not match:
                continue
            value = match.group(1).strip().strip('"').strip("'")
            if value.startswith("./") or value.startswith("docker://"):
                continue
            ref = value.rsplit("@", 1)[1] if "@" in value else ""
            if not re.fullmatch(r"[a-fA-F0-9]{40}", ref):
                findings.append(
                    {
                        "type": "workflow_action_not_sha_pinned",
                        "file": str(workflow.relative_to(repo_path)),
                        "line": line_number,
                        "value": value,
                        "message": "GitHub Action reference is not pinned to a full commit SHA.",
                    }
                )
    return findings


def collect_todos(repo_path: Path, max_items: int) -> list[dict[str, Any]]:
    items = []
    todo_re = re.compile(r"\b(TODO|FIXME|HACK)\b[:\s-]*(.+)", re.IGNORECASE)
    for path in sorted(repo_path.rglob("*")):
        if len(items) >= max_items:
            break
        if should_skip_path(path) or not is_probably_text(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            match = todo_re.search(line)
            if not match:
                continue
            body = match.group(2).strip()
            if len(body) < 12:
                continue
            items.append(
                {
                    "type": "todo_comment",
                    "file": str(path.relative_to(repo_path)),
                    "line": line_number,
                    "marker": match.group(1).upper(),
                    "text": redact_text(body[:500]),
                }
            )
            if len(items) >= max_items:
                break
    return items


def collect_repo_metadata(repo: str, repo_path: Path, max_todos: int) -> dict[str, Any]:
    if not repo_path.exists():
        return {"repo": repo, "path": str(repo_path), "status": "missing_checkout", "findings": []}

    package_manifests = []
    for name in ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements.txt", "pyproject.toml"):
        if (repo_path / name).exists():
            package_manifests.append(name)

    metadata = {
        "repo": repo,
        "path": str(repo_path),
        "status": "collected",
        "head": run_command(["git", "rev-parse", "HEAD"], cwd=repo_path)["stdout"].strip(),
        "git_status": run_command(["git", "status", "--short"], cwd=repo_path)["stdout"].splitlines(),
        "package_manifests": package_manifests,
        "findings": [],
    }
    metadata["findings"].extend(collect_action_pinning(repo_path))
    metadata["findings"].extend(collect_todos(repo_path, max_todos))
    return redact_data(metadata)


def ssh_command(host: str, user: str, key: Path, known_hosts: Path, command: str, timeout: int) -> list[str]:
    return [
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


def collect_server_evidence(args: argparse.Namespace) -> dict[str, Any]:
    if not args.ssh_key or not args.known_hosts or not args.ssh_key.exists() or not args.known_hosts.exists():
        return {"status": "not_configured", "reason": "ssh key or known_hosts not available"}
    commands = {
        "hostname": "hostname",
        "failed_units": "systemctl --failed --no-legend --no-pager || true",
        "root_disk": "df -h / | tail -n +2",
        "root_inodes": "df -i / | tail -n +2",
        "service_states": (
            "for unit in ssh ufw fail2ban caddy alloy nutsnews-backup.timer; do "
            "state=$(systemctl is-active \"$unit\" 2>/dev/null || true); "
            "printf '%s=%s\\n' \"$unit\" \"${state:-unavailable}\"; done"
        ),
        "backend_health": (
            "if command -v curl >/dev/null 2>&1 && systemctl is-active caddy >/dev/null 2>&1; then "
            "curl -fsS --connect-timeout 5 --resolve backend.nutsnews.com:443:127.0.0.1 "
            "https://backend.nutsnews.com/healthz 2>/dev/null || true; else echo unavailable; fi"
        ),
        "backup_status": (
            "if test -x /usr/local/sbin/nutsnews-backup; then "
            "sudo -n /usr/local/sbin/nutsnews-backup status 2>/dev/null || "
            "/usr/local/sbin/nutsnews-backup status 2>/dev/null || true; else echo not_configured; fi"
        ),
    }
    evidence: dict[str, Any] = {"status": "collected", "commands": {}}
    for name, command in commands.items():
        evidence["commands"][name] = run_command(
            ssh_command(args.ssh_host, args.ssh_user, args.ssh_key, args.known_hosts, command, args.timeout),
            timeout=args.timeout + 15,
        )
    return redact_data(evidence)


def collect_evidence(args: argparse.Namespace) -> dict[str, Any]:
    repos = []
    for item in args.repo_root:
        repo, sep, path = item.partition("=")
        if sep != "=":
            raise ValueError(f"--repo-root must be owner/name=path, got {item}")
        repos.append(collect_repo_metadata(repo, Path(path), args.max_todo_items))
    return {
        "generated_at_utc": utc_now().isoformat().replace("+00:00", "Z"),
        "repos": repos,
        "server": collect_server_evidence(args),
        "redaction_status": "applied before OpenAI and GitHub output",
    }


def target_repo_for(candidate: dict[str, Any]) -> str:
    explicit = str(candidate.get("target_repo") or candidate.get("suggested_target_repo") or "").strip()
    if explicit in SCAN_REPOS:
        return explicit
    text = " ".join(
        str(candidate.get(field, ""))
        for field in ("title", "category", "evidence", "suggested_fix", "why_matters", "detected_repo")
    ).lower()
    for repo in ROUTING_PRIORITY:
        keywords = ROUTING_KEYWORDS[repo]
        if any(keyword in text for keyword in keywords):
            return repo
    detected = str(candidate.get("detected_repo") or "").strip()
    return detected if detected in SCAN_REPOS else DEFAULT_TARGET_REPO


def normalize_category(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"docs", "documentation", "runbook"}:
        return "documentation"
    if normalized in {"security", "hardening", "vulnerability"}:
        return "security"
    if normalized in {"bug", "failure", "test-failure"}:
        return "bug"
    if normalized in {"dependency", "dependencies", "updates"}:
        return "dependency"
    if normalized in {"question", "investigation"}:
        return "question"
    return normalized or "maintenance"


def useful_labels_for(candidate: dict[str, Any]) -> list[str]:
    labels = []
    category = normalize_category(str(candidate.get("category", "")))
    severity = str(candidate.get("severity", "")).lower()
    confidence = str(candidate.get("confidence", "")).lower()
    if category in {"bug", "security"} or severity in {"critical", "high"}:
        labels.append("bug")
    elif category == "documentation":
        labels.append("documentation")
    elif category == "question" or confidence in {"low", "unknown"}:
        labels.append("question")
    else:
        labels.append("enhancement")
    for label in candidate.get("labels", []) if isinstance(candidate.get("labels"), list) else []:
        label_text = str(label).strip()
        if label_text in USEFUL_LABELS and label_text not in labels:
            labels.append(label_text)
    return labels


def stable_fingerprint(candidate: dict[str, Any]) -> str:
    existing = str(candidate.get("fingerprint") or "").strip()
    if existing:
        return existing
    source = json.dumps(
        {
            "repo": candidate.get("detected_repo"),
            "target": candidate.get("target_repo"),
            "category": candidate.get("category"),
            "title": candidate.get("title"),
            "evidence": candidate.get("evidence"),
        },
        sort_keys=True,
    )
    return "mnt-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None or value == "":
        return []
    return [str(value)]


def render_issue_body(candidate: dict[str, Any], run_label: str) -> str:
    fingerprint = stable_fingerprint(candidate)
    detected_repo = str(candidate.get("detected_repo") or "unknown")
    target_repo = target_repo_for(candidate)
    duplicate = str(candidate.get("possible_duplicate") or candidate.get("duplicate_of") or "").strip()
    lines = [
        "## Scan run label",
        run_label,
        "",
        "## Finding fingerprint",
        fingerprint,
        "",
        "## Detected repo",
        detected_repo,
        "",
        "## Suggested target repo",
        target_repo,
        "",
        "## Category",
        normalize_category(str(candidate.get("category") or "maintenance")),
        "",
        "## Severity",
        str(candidate.get("severity") or "medium"),
        "",
        "## Confidence",
        str(candidate.get("confidence") or "unknown"),
        "",
        "## Possible noise or false-positive reason",
        str(candidate.get("possible_noise") or candidate.get("possible_noise_or_false_positive_reason") or "none stated"),
        "",
        "## Evidence",
        str(candidate.get("evidence") or "No evidence supplied by model."),
        "",
        "## Why this matters",
        str(candidate.get("why_matters") or "Needs triage."),
        "",
        "## Suggested fix or investigation path",
        str(candidate.get("suggested_fix") or candidate.get("suggested_fix_or_investigation_path") or "Investigate the cited evidence."),
        "",
        "## Acceptance criteria",
    ]
    criteria = list_value(candidate.get("acceptance_criteria")) or ["Finding is triaged and either fixed, documented as accepted risk, or closed as noise."]
    lines.extend(f"- {item}" for item in criteria)
    lines.extend(["", "## Validation ideas"])
    validation = list_value(candidate.get("validation_ideas")) or ["Run the relevant repo validation after any fix."]
    lines.extend(f"- {item}" for item in validation)
    lines.extend(["", "## Related files/log queries/checks"])
    related = list_value(candidate.get("related_files") or candidate.get("related_files_log_queries_checks")) or ["See evidence section."]
    lines.extend(f"- {item}" for item in related)
    lines.extend(["", "## Secret-redaction status", str(candidate.get("secret_redaction_status") or "redacted before issue creation")])
    if duplicate:
        lines.extend(["", "## Possible duplicate", duplicate])
    lines.extend(["", "<!-- maintenance-robot-generated -->"])
    return redact_text("\n".join(lines) + "\n")


def normalize_candidate(candidate: dict[str, Any], run_label: str) -> dict[str, Any]:
    normalized = dict(candidate)
    normalized["fingerprint"] = stable_fingerprint(normalized)
    normalized["detected_repo"] = str(normalized.get("detected_repo") or normalized.get("repo") or DEFAULT_TARGET_REPO)
    normalized["target_repo"] = target_repo_for(normalized)
    normalized["category"] = normalize_category(str(normalized.get("category") or "maintenance"))
    normalized["severity"] = str(normalized.get("severity") or "medium").lower()
    normalized["confidence"] = str(normalized.get("confidence") or "unknown").lower()
    normalized["title"] = str(normalized.get("title") or f"Maintenance finding: {normalized['category']}").strip()[:240]
    normalized["labels"] = [run_label, *useful_labels_for(normalized)]
    normalized["body"] = render_issue_body(normalized, run_label)
    return redact_data(normalized)


def issue_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "fingerprint": {"type": "string"},
                        "detected_repo": {"type": "string"},
                        "target_repo": {"type": "string"},
                        "category": {"type": "string"},
                        "severity": {"type": "string"},
                        "confidence": {"type": "string"},
                        "possible_noise": {"type": "string"},
                        "evidence": {"type": "string"},
                        "why_matters": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "validation_ideas": {"type": "array", "items": {"type": "string"}},
                        "related_files": {"type": "array", "items": {"type": "string"}},
                        "secret_redaction_status": {"type": "string"},
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "possible_duplicate": {"type": "string"},
                    },
                    "required": [
                        "title",
                        "fingerprint",
                        "detected_repo",
                        "target_repo",
                        "category",
                        "severity",
                        "confidence",
                        "possible_noise",
                        "evidence",
                        "why_matters",
                        "suggested_fix",
                        "acceptance_criteria",
                        "validation_ideas",
                        "related_files",
                        "secret_redaction_status",
                        "labels",
                        "possible_duplicate",
                    ],
                },
            }
        },
        "required": ["findings"],
    }


def openai_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    if os.environ.get("OPENAI_ORG_ID"):
        headers["OpenAI-Organization"] = os.environ["OPENAI_ORG_ID"]
    if os.environ.get("OPENAI_PROJECT"):
        headers["OpenAI-Project"] = os.environ["OPENAI_PROJECT"]
    return headers


def extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for output in response.get("output", []) if isinstance(response.get("output"), list) else []:
        for content in output.get("content", []) if isinstance(output, dict) else []:
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def parse_openai_findings(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    data = json.loads(stripped)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return [item for item in data["findings"] if isinstance(item, dict)]
    raise ValueError("OpenAI response did not contain a findings array")


def call_openai(evidence: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    prompt = (
        "You are the NutsNews maintenance triage robot. Convert the redacted evidence into GitHub issue candidates. "
        "Create one candidate per finding. Do not suppress low-confidence or noisy findings; mark uncertainty in possible_noise. "
        "Route app/frontend/API findings to ramideltoro/nutsnews, backend server/platform findings to "
        "ramideltoro/nutsnews-backend, GitOps/Ansible/VPS findings to ramideltoro/nutsnews-infra, and docs/runbook/process "
        "findings to ramideltoro/nutsnews-docs. If evidence indicates no actionable finding, return an empty findings array."
    )
    payload = {
        "model": args.openai_model,
        "store": False,
        "reasoning": {"effort": args.reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "nutsnews_maintenance_findings",
                "schema": issue_schema(),
                "strict": True,
            }
        },
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(evidence, sort_keys=True)}]},
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers=openai_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.openai_timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = redact_text(exc.read().decode("utf-8", errors="replace")[:2000])
        raise RuntimeError(f"OpenAI API HTTP {exc.code}: {detail}") from exc
    data = json.loads(raw)
    return parse_openai_findings(extract_output_text(data))


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def github_request(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers=github_headers(token),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = redact_text(exc.read().decode("utf-8", errors="replace")[:2000])
        raise RuntimeError(f"GitHub API HTTP {exc.code} for {method} {path}: {detail}") from exc


def ensure_label(repo: str, label: str, token: str, color: str = "5319e7") -> dict[str, Any]:
    encoded = urllib.parse.quote(label, safe="")
    try:
        return github_request("GET", f"/repos/{repo}/labels/{encoded}", token)
    except RuntimeError:
        return github_request(
            "POST",
            f"/repos/{repo}/labels",
            token,
            {"name": label, "color": color, "description": "NutsNews maintenance robot scan label"},
        )


def find_possible_duplicate(repo: str, fingerprint: str, token: str) -> str:
    query = urllib.parse.quote(f'repo:{repo} "{fingerprint}" in:body state:open')
    try:
        result = github_request("GET", f"/search/issues?q={query}&per_page=1", token)
    except RuntimeError:
        return ""
    items = result.get("items", []) if isinstance(result, dict) else []
    if items:
        number = items[0].get("number")
        return f"possible duplicate of #{number}" if number else ""
    return ""


def create_issue(repo: str, candidate: dict[str, Any], token: str) -> dict[str, Any]:
    labels = list(dict.fromkeys(str(label) for label in candidate["labels"]))
    for label in labels:
        ensure_label(repo, label, token, USEFUL_LABELS.get(label, "5319e7"))
    payload = {"title": candidate["title"], "body": candidate["body"], "labels": labels}
    try:
        return github_request("POST", f"/repos/{repo}/issues", token, payload)
    except RuntimeError:
        payload["labels"] = [candidate["labels"][0]]
        return github_request("POST", f"/repos/{repo}/issues", token, payload)


def failure_candidate(run_label: str, message: str, evidence: str) -> dict[str, Any]:
    return normalize_candidate(
        {
            "title": "Maintenance robot failed to complete a scan step",
            "fingerprint": "maintenance-robot-failure-" + hashlib.sha256(message.encode("utf-8")).hexdigest()[:12],
            "detected_repo": DEFAULT_TARGET_REPO,
            "target_repo": DEFAULT_TARGET_REPO,
            "category": "bug",
            "severity": "high",
            "confidence": "high",
            "possible_noise": "Failure may be transient if the provider or API was temporarily unavailable.",
            "evidence": evidence,
            "why_matters": "The maintenance robot may have missed findings that should become GitHub issues.",
            "suggested_fix": "Inspect the workflow artifact and provider/API status, then rerun the scan.",
            "acceptance_criteria": ["The robot completes a dry-run and create-mode scan without this failure."],
            "validation_ideas": ["Rerun Backend OpenAI Maintenance Robot in dry-run mode."],
            "related_files": [".github/workflows/backend-openai-maintenance.yml", "scripts/openai_maintenance_robot.py"],
            "secret_redaction_status": "failure evidence redacted before issue creation",
            "labels": ["bug"],
        },
        run_label,
    )


def create_issues(candidates: list[dict[str, Any]], args: argparse.Namespace, run_label: str) -> list[dict[str, Any]]:
    token = os.environ.get("NUTSNEWS_MAINTENANCE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    results = []
    if not token:
        return [
            {
                "status": "failed",
                "target_repo": candidate["target_repo"],
                "fingerprint": candidate["fingerprint"],
                "error": "NUTSNEWS_MAINTENANCE_GITHUB_TOKEN/GITHUB_TOKEN is not configured",
            }
            for candidate in candidates
        ]

    for candidate in candidates:
        target = args.test_target_repo if args.mode == "test" else candidate["target_repo"]
        candidate = dict(candidate)
        candidate["target_repo"] = target
        duplicate = find_possible_duplicate(target, candidate["fingerprint"], token)
        if duplicate and "Possible duplicate" not in candidate["body"]:
            candidate["body"] = candidate["body"] + f"\n## Possible duplicate\n{duplicate}\n"
        if args.mode == "test" and "maintenance-robot-test" not in candidate["labels"]:
            candidate["labels"].append("maintenance-robot-test")
        try:
            issue = create_issue(target, candidate, token)
            results.append(
                {
                    "status": "created",
                    "target_repo": target,
                    "number": issue.get("number"),
                    "url": issue.get("html_url"),
                    "fingerprint": candidate["fingerprint"],
                }
            )
        except Exception as exc:
            results.append(
                {
                    "status": "failed",
                    "target_repo": target,
                    "fingerprint": candidate["fingerprint"],
                    "error": redact_text(str(exc)),
                }
            )
    failures = [result for result in results if result["status"] == "failed"]
    if failures and args.mode in {"create", "test"}:
        failure = failure_candidate(run_label, "GitHub issue creation failure", json.dumps(failures, indent=2))
        try:
            issue = create_issue(DEFAULT_TARGET_REPO, failure, token)
            results.append(
                {
                    "status": "created_failure_report",
                    "target_repo": DEFAULT_TARGET_REPO,
                    "number": issue.get("number"),
                    "url": issue.get("html_url"),
                    "fingerprint": failure["fingerprint"],
                }
            )
        except Exception as exc:
            results.append({"status": "failed_failure_report", "target_repo": DEFAULT_TARGET_REPO, "error": redact_text(str(exc))})
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "test", "create"), default="dry-run")
    parser.add_argument("--scan-label", default="")
    parser.add_argument("--repo-root", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))
    parser.add_argument("--reasoning-effort", default=os.environ.get("OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT))
    parser.add_argument("--openai-timeout", type=int, default=120)
    parser.add_argument("--max-todo-items", type=int, default=20)
    parser.add_argument("--test-target-repo", default=DEFAULT_TARGET_REPO)
    parser.add_argument("--ssh-host", default=os.environ.get("NUTSNEWS_BACKEND_HOST", "65.75.201.18"))
    parser.add_argument("--ssh-user", default=os.environ.get("NUTSNEWS_BACKEND_ANSIBLE_USER", "rami") or "rami")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--openai-response-fixture", type=Path)
    return parser.parse_args(argv)


def write_summary(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Backend OpenAI Maintenance Robot",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Scan run label: `{report['scan_label']}`",
        f"- OpenAI model: `{report['openai']['model']}`",
        f"- OpenAI status: `{report['openai']['status']}`",
        f"- Finding count: `{len(report['findings'])}`",
        f"- Issue result count: `{len(report['issue_results'])}`",
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("- None")
    for finding in report["findings"]:
        lines.append(f"- `{finding['target_repo']}` `{finding['severity']}` `{finding['confidence']}` {finding['title']}")
    lines.extend(["", "## Issue Results", ""])
    if not report["issue_results"]:
        lines.append("- None")
    for result in report["issue_results"]:
        lines.append(f"- `{result['status']}` `{result.get('target_repo', '')}` {result.get('url') or result.get('error', '')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.repo_root:
        args.repo_root = [f"{repo}=." for repo in SCAN_REPOS if repo == DEFAULT_TARGET_REPO]
    run_label = args.scan_label or scan_label()
    evidence = collect_evidence(args)
    openai_status = "not_attempted"
    openai_error = None
    raw_candidates: list[dict[str, Any]] = []

    try:
        if args.openai_response_fixture:
            raw_candidates = parse_openai_findings(args.openai_response_fixture.read_text(encoding="utf-8"))
            openai_status = "fixture"
        else:
            raw_candidates = call_openai(evidence, args)
            openai_status = "success"
    except Exception as exc:
        openai_status = "failed"
        openai_error = redact_text(str(exc))
        raw_candidates = [failure_candidate(run_label, "OpenAI issue-candidate generation failed", openai_error)]

    candidates = [normalize_candidate(candidate, run_label) for candidate in raw_candidates]
    issue_results = []
    if args.mode == "dry-run":
        issue_results = [
            {
                "status": "planned",
                "target_repo": candidate["target_repo"],
                "fingerprint": candidate["fingerprint"],
                "title": candidate["title"],
            }
            for candidate in candidates
        ]
    else:
        issue_results = create_issues(candidates, args, run_label)

    report = {
        "schema_version": 1,
        "generated_at_utc": utc_now().isoformat().replace("+00:00", "Z"),
        "mode": args.mode,
        "scan_label": run_label,
        "openai": {
            "status": openai_status,
            "model": args.openai_model,
            "reasoning_effort": args.reasoning_effort,
            "error": openai_error,
            "guidance_checked": "2026-07-17 resolver: gpt-5.6 alias routes to gpt-5.6-sol; Responses API recommended",
        },
        "evidence": evidence,
        "findings": candidates,
        "issue_results": issue_results,
        "redaction_status": "evidence, OpenAI errors, and issue bodies redacted",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact_data(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, report)
    print(json.dumps({"mode": args.mode, "scan_label": run_label, "findings": len(candidates), "openai_status": openai_status}, indent=2))
    return 0 if not any(result["status"].startswith("failed") for result in issue_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
