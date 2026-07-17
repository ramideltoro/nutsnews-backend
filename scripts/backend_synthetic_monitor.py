#!/usr/bin/env python3
"""Run off-box public synthetic checks from GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,})")
EMAIL_RE = re.compile(r"\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\\b")
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\\s]+:)([^@\\s]+)(@)", re.IGNORECASE)


@dataclass(frozen=True)
class SyntheticCheck:
    name: str
    url: str
    expected_statuses: tuple[int, ...]
    failure_class: str
    body_contains: str | None = None
    follow_redirects: bool = True
    expected_location_prefix: str | None = None
    timeout: int = 10


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    starttls: bool
    username: str
    password: str
    sender: str
    recipients: list[str]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: str) -> str:
    redacted = TOKEN_RE.sub("<redacted-token>", value)
    redacted = URL_SECRET_RE.sub(r"\\1<redacted>\\3", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def public_checks() -> list[SyntheticCheck]:
    return [
        SyntheticCheck(
            name="frontend_www_home",
            url="https://www.nutsnews.com/",
            expected_statuses=(200,),
            failure_class="frontend_public_http",
        ),
        SyntheticCheck(
            name="frontend_apex_redirect",
            url="https://nutsnews.com/",
            expected_statuses=(308,),
            follow_redirects=False,
            expected_location_prefix="https://www.nutsnews.com/",
            failure_class="frontend_redirect",
        ),
        SyntheticCheck(
            name="backend_healthz",
            url="https://backend.nutsnews.com/healthz",
            expected_statuses=(200,),
            body_contains="ok",
            failure_class="backend_health",
        ),
        SyntheticCheck(
            name="backend_tls_known_404",
            url="https://backend.nutsnews.com/",
            expected_statuses=(404,),
            body_contains="backend application not deployed",
            failure_class="backend_tls_caddy",
        ),
        SyntheticCheck(
            name="supabase_platform_status",
            url="https://status.supabase.com/api/v2/status.json",
            expected_statuses=(200,),
            body_contains='"indicator"',
            failure_class="auth_provider_status",
        ),
    ]


def load_previous(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    previous: dict[str, str] = {}
    for check in data.get("checks", []):
        if not isinstance(check, dict):
            continue
        name = str(check.get("name", ""))
        last_success = str(check.get("last_success_at", ""))
        if name and last_success:
            previous[name] = last_success
    return previous


def opener_for(check: SyntheticCheck) -> urllib.request.OpenerDirector:
    if check.follow_redirects:
        return urllib.request.build_opener()
    return urllib.request.build_opener(NoRedirectHandler)


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return "dns"
        if isinstance(reason, TimeoutError):
            return "timeout"
        if isinstance(reason, ssl.SSLError):
            return "tls"
        return "connection"
    return "unknown"


def run_check(check: SyntheticCheck, previous_success: str | None, generated_at: str) -> dict[str, Any]:
    request = urllib.request.Request(
        check.url,
        headers={
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.8",
            "User-Agent": "NutsNewsBackendSynthetic/1.0 (+https://github.com/ramideltoro/nutsnews-backend)",
        },
        method="GET",
    )
    started = time.monotonic()
    http_status: int | None = None
    body = ""
    headers: dict[str, str] = {}
    error_class = ""
    error_detail = ""
    try:
        with opener_for(check).open(request, timeout=check.timeout) as response:
            http_status = response.getcode()
            headers = {key.lower(): value for key, value in response.headers.items()}
            body = response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        headers = {key.lower(): value for key, value in exc.headers.items()}
        body = exc.read(4096).decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive network edge cases
        error_class = classify_error(exc)
        error_detail = redact(str(exc))
    duration_ms = int((time.monotonic() - started) * 1000)

    checks = []
    checks.append(http_status in check.expected_statuses if http_status is not None else False)
    if check.body_contains is not None:
        checks.append(check.body_contains in body)
    if check.expected_location_prefix is not None:
        checks.append(str(headers.get("location", "")).startswith(check.expected_location_prefix))

    ok = bool(checks) and all(checks) and not error_class
    if ok:
        failure_class = ""
        failure_detail = ""
        last_success_at = generated_at
    else:
        failure_class = error_class or check.failure_class
        expected = ",".join(str(item) for item in check.expected_statuses)
        failure_detail = error_detail or f"expected_status={expected} observed_status={http_status}"
        if check.body_contains is not None and check.body_contains not in body:
            failure_detail += " body_match=false"
        if check.expected_location_prefix is not None and not str(headers.get("location", "")).startswith(check.expected_location_prefix):
            failure_detail += f" location={redact(str(headers.get('location', '')))}"
        last_success_at = previous_success

    return {
        "name": check.name,
        "url": redact(check.url),
        "status": "healthy" if ok else "critical",
        "failure_class": failure_class or None,
        "failure_detail": failure_detail or None,
        "http_status": http_status,
        "duration_ms": duration_ms,
        "last_success_at": last_success_at,
        "expected_statuses": list(check.expected_statuses),
        "source": "github_actions",
    }


def smtp_config_from_env() -> SmtpConfig | None:
    host = os.environ.get("NUTSNEWS_REPORT_SMTP_HOST", "").strip()
    username = os.environ.get("NUTSNEWS_REPORT_SMTP_USERNAME", "").strip()
    password = os.environ.get("NUTSNEWS_REPORT_SMTP_PASSWORD", "")
    sender = os.environ.get("NUTSNEWS_REPORT_EMAIL_FROM", "").strip()
    recipients = [item.strip() for item in os.environ.get("NUTSNEWS_REPORT_EMAIL_TO", "").split(",") if item.strip()]
    if not (host and username and password and sender and recipients):
        return None
    return SmtpConfig(
        host=host,
        port=int(os.environ.get("NUTSNEWS_REPORT_SMTP_PORT", "587") or "587"),
        starttls=(os.environ.get("NUTSNEWS_REPORT_SMTP_STARTTLS", "true").lower() != "false"),
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
    )


def send_failure_email(config: SmtpConfig, report: dict[str, Any]) -> dict[str, str]:
    failures = [check for check in report["checks"] if check["status"] != "healthy"]
    if not failures:
        return {"status": "skipped", "detail": "no failures"}

    prefix = os.environ.get("NUTSNEWS_REPORT_SUBJECT_PREFIX", "[NutsNews backend]")
    message = EmailMessage()
    message["Subject"] = f"{prefix} synthetic monitor failure"
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    lines = [
        "NutsNews backend synthetic monitoring detected failures.",
        "",
        f"Generated at: {report['generated_at_utc']}",
        f"Source: {report['source']['provider']} ({report['source']['location']})",
        f"Run URL: {report['source'].get('run_url') or 'unknown'}",
        "",
    ]
    for failure in failures:
        lines.extend(
            [
                f"- endpoint: {failure['name']}",
                f"  status: {failure.get('http_status')}",
                f"  failure_class: {failure.get('failure_class')}",
                f"  last_success_at: {failure.get('last_success_at') or 'unknown'}",
                f"  detail: {failure.get('failure_detail')}",
            ]
        )
    message.set_content(redact("\\n".join(lines)))

    with smtplib.SMTP(config.host, config.port, timeout=20) as smtp:
        if config.starttls:
            smtp.starttls()
        smtp.login(config.username, config.password)
        smtp.send_message(message)
    return {"status": "sent", "detail": f"failures={len(failures)}"}


def write_summary(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    lines = [
        "# Backend Synthetic Monitor",
        "",
        f"- status: `{report['status']}`",
        f"- healthy: `{report['summary']['healthy']}`",
        f"- critical: `{report['summary']['critical']}`",
        f"- source: `{report['source']['provider']}` / `{report['source']['location']}`",
        "",
        "| Endpoint | Status | HTTP | Failure class | Last success |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for check in report["checks"]:
        lines.append(
            f"| `{check['name']}` | `{check['status']}` | `{check.get('http_status')}` | `{check.get('failure_class') or ''}` | `{check.get('last_success_at') or ''}` |"
        )
    path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--previous-state", type=Path)
    parser.add_argument("--send-email", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = utc_now()
    previous = load_previous(args.previous_state)
    checks = [run_check(check, previous.get(check.name), generated_at) for check in public_checks()]
    critical = sum(1 for check in checks if check["status"] == "critical")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "source": {
            "provider": "github_actions",
            "location": os.environ.get("RUNNER_NAME") or os.environ.get("ImageOS") or "github-hosted-runner",
            "run_url": os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
            + "/"
            + os.environ.get("GITHUB_REPOSITORY", "ramideltoro/nutsnews-backend")
            + "/actions/runs/"
            + os.environ.get("GITHUB_RUN_ID", ""),
        },
        "status": "critical" if critical else "healthy",
        "summary": {"total": len(checks), "healthy": len(checks) - critical, "critical": critical},
        "checks": checks,
        "delivery": {"status": "skipped", "detail": "send_email=false"},
    }

    if args.send_email:
        config = smtp_config_from_env()
        if config is None:
            report["delivery"] = {"status": "not_configured", "detail": "missing SMTP secret names"}
        else:
            report["delivery"] = send_failure_email(config, report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    write_summary(args.summary, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
