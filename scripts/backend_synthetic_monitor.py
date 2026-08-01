#!/usr/bin/env python3
"""Run off-box public and protected synthetic checks from GitHub Actions."""

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
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

try:
    from scripts import backend_alert_state
except ModuleNotFoundError:  # pragma: no cover - script-path execution
    import backend_alert_state


TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_SECRET_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
DEFAULT_ADMIN_BACKEND_API_URL = "https://backend.nutsnews.com/api/app/db"


@dataclass(frozen=True)
class SyntheticCheck:
    name: str
    url: str
    expected_statuses: tuple[int, ...]
    failure_class: str
    body_contains: str | None = None
    expected_json: tuple[tuple[str, Any], ...] = ()
    expected_header_contains: tuple[tuple[str, str], ...] = ()
    follow_redirects: bool = True
    expected_location_prefix: str | None = None
    timeout: int = 10


@dataclass(frozen=True)
class AdminBackendOperation:
    name: str
    body: dict[str, Any]
    timeout: int = 15


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
    redacted = URL_SECRET_RE.sub(r"\1<redacted>\3", redacted)
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
            name="backend_readyz",
            url="https://backend.nutsnews.com/readyz",
            expected_statuses=(200,),
            expected_json=(
                ("status", "ready"),
                ("ready", True),
                ("service", "nutsnews-worker-db-api"),
                ("deploymentEnvironment", "production"),
                ("dependencies.postgresql", "ready"),
            ),
            expected_header_contains=(("cache-control", "no-store"), ("pragma", "no-cache")),
            failure_class="backend_readiness",
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


def admin_backend_operations() -> list[AdminBackendOperation]:
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return [
        AdminBackendOperation(
            name="load-admin-production-readiness",
            body={
                "providerMode": "backend_postgres_primary",
                "recentArticleLimit": 100,
                "translationSampleLimit": 60,
                "defaultLanguageCode": "en",
                "targetLanguageCodes": ["fr", "ja", "de-CH", "de", "el"],
                "articleGrowthWindowsHours": [24, 24 * 7],
            },
        ),
        AdminBackendOperation(
            name="load-admin-article-reviews",
            body={
                "providerMode": "backend_postgres_primary",
                "filters": {
                    "decision": "all",
                    "source": "",
                    "category": "",
                    "minScore": None,
                    "maxScore": None,
                    "page": 0,
                    "sort": "newest",
                },
                "pageSize": 5,
                "recentPublishedArticleLimit": 3,
                "aiDecisionVersionReportLimit": 3,
                "maxOptionRows": 100,
            },
        ),
        AdminBackendOperation(
            name="load-admin-article-engagement",
            body={"providerMode": "backend_postgres_primary", "sourceCategoryLimit": 10, "articleLimit": 5},
        ),
        AdminBackendOperation(
            name="load-admin-ai-usage",
            body={"providerMode": "backend_postgres_primary", "since": since, "limit": 20},
        ),
        AdminBackendOperation(
            name="load-admin-local-ai",
            body={"providerMode": "backend_postgres_primary", "since": since, "runLimit": 20, "reviewLimit": 10},
        ),
        AdminBackendOperation(
            name="load-admin-translation-quality",
            body={
                "providerMode": "backend_postgres_primary",
                "auditLimit": 10,
                "summaryLookupLimit": 100,
                "targetLanguageCodes": ["fr", "ja", "de-CH", "de", "el"],
            },
        ),
        AdminBackendOperation(
            name="load-admin-guardrails",
            body={
                "providerMode": "backend_postgres_primary",
                "since": since,
                "limit": 20,
                "countTables": ["articles", "article_summaries", "rss_feeds"],
            },
        ),
        AdminBackendOperation(
            name="load-admin-worker-shards",
            body={
                "providerMode": "backend_postgres_primary",
                "limit": 20,
                "shardCount": 25,
                "staleAfterMinutes": 180,
                "slowRunMs": 15000,
                "dailyWindowDays": 7,
            },
        ),
        AdminBackendOperation(
            name="load-admin-rss-feed-health",
            body={"providerMode": "backend_postgres_primary", "limit": 20, "staleAfterHours": 24},
        ),
        AdminBackendOperation(
            name="load-admin-feed-management",
            body={"providerMode": "backend_postgres_primary", "limit": 20},
        ),
        AdminBackendOperation(
            name="load-admin-audit-log",
            body={"providerMode": "backend_postgres_primary", "limit": 20},
        ),
        AdminBackendOperation(
            name="load-admin-runtime-feature-flags",
            body={"providerMode": "backend_postgres_primary", "limit": 20},
        ),
    ]


def normalize_admin_backend_base_url(value: str | None) -> str:
    base_url = (value or DEFAULT_ADMIN_BACKEND_API_URL).strip() or DEFAULT_ADMIN_BACKEND_API_URL
    base_url = base_url.rstrip("/")
    if "/api/app/db" not in base_url:
        base_url = f"{base_url}/api/app/db"
    return base_url


def load_previous(path: Path | None) -> dict[str, str]:
    report = load_previous_report(path)
    previous: dict[str, str] = {}
    for check in report.get("checks", []):
        if not isinstance(check, dict):
            continue
        name = str(check.get("name", ""))
        last_success = str(check.get("last_success_at", ""))
        if name and last_success:
            previous[name] = last_success
    return previous


def load_previous_report(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return report if isinstance(report, dict) else {}


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


def json_path_value(payload: Any, path: str) -> Any:
    current = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


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
    json_matches = True
    if check.expected_json:
        try:
            parsed_body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            parsed_body = None
        json_matches = all(
            json_path_value(parsed_body, path) == expected
            for path, expected in check.expected_json
        )
        checks.append(json_matches)
    header_matches = all(
        expected.lower() in str(headers.get(header.lower(), "")).lower()
        for header, expected in check.expected_header_contains
    )
    if check.expected_header_contains:
        checks.append(header_matches)
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
        if check.expected_json and not json_matches:
            failure_detail += " json_match=false"
        if check.expected_header_contains and not header_matches:
            failure_detail += " header_match=false"
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
        "method": "GET",
        "check_type": "public_http",
        "source": "github_actions",
    }


def missing_admin_backend_token_check(base_url: str, previous_success: str | None) -> dict[str, Any]:
    return {
        "name": "admin_backend_operations_config",
        "url": redact(base_url),
        "status": "critical",
        "failure_class": "admin_backend_configuration",
        "failure_detail": "missing NUTSNEWS_BACKEND_API_TOKEN; protected admin backend operation checks are required",
        "http_status": None,
        "duration_ms": 0,
        "last_success_at": previous_success,
        "expected_statuses": ["2xx"],
        "method": "POST",
        "check_type": "admin_backend_operation",
        "source": "github_actions",
    }


def run_admin_backend_operation(
    operation: AdminBackendOperation,
    base_url: str,
    token: str,
    previous_success: str | None,
    generated_at: str,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{operation.name}"
    encoded = json.dumps(operation.body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "NutsNewsBackendSynthetic/1.0 (+https://github.com/ramideltoro/nutsnews-backend)",
            "X-NutsNews-Db-Client": "backend-synthetic-monitor",
        },
    )
    started = time.monotonic()
    http_status: int | None = None
    raw_body = ""
    error_class = ""
    error_detail = ""
    rows: Any = None

    def payload_failure(parsed_rows: list[Any]) -> tuple[str, str] | None:
        if operation.name != "load-admin-article-reviews":
            return None
        if not parsed_rows:
            return (
                "admin_backend_invalid_shape",
                f"operation={operation.name} route={redact(url)} snapshot_row=false",
            )
        snapshot = parsed_rows[0]
        if not isinstance(snapshot, dict):
            return (
                "admin_backend_invalid_shape",
                f"operation={operation.name} route={redact(url)} snapshot_shape=false",
            )
        if not isinstance(snapshot.get("versionReportRows"), list):
            return (
                "admin_backend_invalid_shape",
                f"operation={operation.name} route={redact(url)} versionReportRows_shape=false",
            )
        if snapshot.get("versionReportError") is not None:
            return (
                "admin_backend_snapshot_error",
                f"operation={operation.name} route={redact(url)} versionReportError_present=true",
            )
        return None

    try:
        with urllib.request.urlopen(request, timeout=operation.timeout) as response:
            http_status = response.getcode()
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        exc.read(32768)
    except Exception as exc:  # pragma: no cover - defensive network edge cases
        error_class = classify_error(exc)
        error_detail = f"operation={operation.name} route={redact(url)} request_failed={redact(str(exc))}"
    duration_ms = int((time.monotonic() - started) * 1000)

    if error_class:
        ok = False
        failure_class = error_class
        failure_detail = error_detail
    elif http_status is None or http_status < 200 or http_status >= 300:
        ok = False
        failure_class = "admin_backend_http"
        failure_detail = f"operation={operation.name} route={redact(url)} expected_status=2xx observed_status={http_status}"
    else:
        try:
            parsed: Any = json.loads(raw_body or "{}")
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            ok = False
            failure_class = "admin_backend_invalid_json"
            failure_detail = f"operation={operation.name} route={redact(url)} invalid_json=true"
        else:
            rows = parsed.get("rows")
            if isinstance(rows, list):
                operation_failure = payload_failure(rows)
                if operation_failure:
                    ok = False
                    failure_class, failure_detail = operation_failure
                else:
                    ok = True
                    failure_class = ""
                    failure_detail = ""
            else:
                ok = False
                failure_class = "admin_backend_invalid_shape"
                failure_detail = f"operation={operation.name} route={redact(url)} rows_shape=false"

    return {
        "name": operation.name,
        "url": redact(url),
        "status": "healthy" if ok else "critical",
        "failure_class": failure_class or None,
        "failure_detail": failure_detail or None,
        "http_status": http_status,
        "duration_ms": duration_ms,
        "last_success_at": generated_at if ok else previous_success,
        "expected_statuses": ["2xx"],
        "method": "POST",
        "check_type": "admin_backend_operation",
        "row_count": len(rows) if isinstance(rows, list) else None,
        "source": "github_actions",
    }


def run_admin_backend_operations(previous: dict[str, str], generated_at: str) -> list[dict[str, Any]]:
    base_url = normalize_admin_backend_base_url(os.environ.get("NUTSNEWS_BACKEND_API_URL"))
    token = os.environ.get("NUTSNEWS_BACKEND_API_TOKEN", "").strip()
    if not token:
        return [missing_admin_backend_token_check(base_url, previous.get("admin_backend_operations_config"))]
    return [run_admin_backend_operation(operation, base_url, token, previous.get(operation.name), generated_at) for operation in admin_backend_operations()]


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
    notifications = report.get("alerting", {}).get("notifications", [])
    if not notifications:
        suppressed = report.get("alerting", {}).get("summary", {}).get("suppressed_count", 0)
        return {"status": "skipped", "detail": f"no unsuppressed notifications; suppressed={suppressed}"}

    prefix = os.environ.get("NUTSNEWS_REPORT_SUBJECT_PREFIX", "[NutsNews backend]")
    message = EmailMessage()
    message["Subject"] = f"{prefix} synthetic monitor alert: {len(notifications)} notification(s)"
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
    for notification in notifications:
        lines.extend(
            [
                f"- service: {notification.get('service')}",
                f"  status: {notification.get('status')}",
                f"  severity: {notification.get('severity')}",
                f"  failure_class: {notification.get('failure_class')}",
                f"  fingerprint: {notification.get('fingerprint')}",
                f"  reason: {notification.get('notification_reason')}",
                f"  message: {notification.get('message')}",
            ]
        )
    message.set_content(redact("\n".join(lines)))

    with smtplib.SMTP(config.host, config.port, timeout=20) as smtp:
        if config.starttls:
            smtp.starttls()
        smtp.login(config.username, config.password)
        smtp.send_message(message)
    return {"status": "sent", "detail": f"notifications={len(notifications)}"}


def current_alerts_from_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts = []
    for check in checks:
        if check["status"] == "healthy":
            continue
        alerts.append(
            {
                "source": "synthetic",
                "service": check["name"],
                "severity": "critical",
                "failure_class": check.get("failure_class") or "unknown",
                "message": check.get("failure_detail") or f"{check['name']} status={check.get('http_status')}",
            }
        )
    return alerts


def write_summary(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    alert_summary = report.get("alerting", {}).get("summary", {})
    lines = [
        "# Backend Synthetic Monitor",
        "",
        f"- status: `{report['status']}`",
        f"- healthy: `{report['summary']['healthy']}`",
        f"- critical: `{report['summary']['critical']}`",
        f"- source: `{report['source']['provider']}` / `{report['source']['location']}`",
        f"- active alerts: `{alert_summary.get('active_alert_count', 0)}`",
        f"- notifications: `{alert_summary.get('notification_count', 0)}`",
        f"- suppressed: `{alert_summary.get('suppressed_count', 0)}`",
        f"- recovered: `{alert_summary.get('recovered_count', 0)}`",
        f"- last sent: `{alert_summary.get('last_sent_at') or 'none'}`",
        f"- last error: `{alert_summary.get('last_error') or 'none'}`",
        "",
        "| Check | Status | HTTP | Failure class | Last success |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for check in report["checks"]:
        lines.append(
            f"| `{check['name']}` | `{check['status']}` | `{check.get('http_status')}` | `{check.get('failure_class') or ''}` | `{check.get('last_success_at') or ''}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    previous_report = load_previous_report(args.previous_state)
    previous = load_previous(args.previous_state)
    checks = [run_check(check, previous.get(check.name), generated_at) for check in public_checks()]
    checks.extend(run_admin_backend_operations(previous, generated_at))
    critical = sum(1 for check in checks if check["status"] == "critical")
    admin_backend_checks = [check for check in checks if check.get("check_type") == "admin_backend_operation"]
    admin_backend_critical = sum(1 for check in admin_backend_checks if check["status"] == "critical")
    alerting = backend_alert_state.evaluate_alerts(
        previous_report,
        current_alerts_from_checks(checks),
        generated_at,
        cooldown_seconds=60 * 60,
    )
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
        "admin_backend_operations": {
            "required": True,
            "base_url": redact(normalize_admin_backend_base_url(os.environ.get("NUTSNEWS_BACKEND_API_URL"))),
            "total": len(admin_backend_checks),
            "healthy": len(admin_backend_checks) - admin_backend_critical,
            "critical": admin_backend_critical,
        },
        "checks": checks,
        "alerting": {"summary": alerting["summary"], "notifications": alerting["notifications"], "suppressed": alerting["suppressed"]},
        "alert_state": alerting["state"],
        "delivery": {"status": "skipped", "detail": "send_email=false"},
    }

    if args.send_email:
        config = smtp_config_from_env()
        if config is None:
            report["delivery"] = {"status": "not_configured", "detail": "missing SMTP secret names"}
        else:
            report["delivery"] = send_failure_email(config, report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_summary(args.summary, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
