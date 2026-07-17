"""Alert fingerprinting, cooldown, suppression, and recovery helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any


ISO_TIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b", re.IGNORECASE)
HEX_RE = re.compile(r"\b[a-f0-9]{8,64}\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
SPACE_RE = re.compile(r"\s+")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def normalize_message(message: str) -> str:
    normalized = ISO_TIME_RE.sub("<time>", message.lower())
    normalized = HEX_RE.sub("<id>", normalized)
    normalized = NUMBER_RE.sub("<num>", normalized)
    normalized = SPACE_RE.sub(" ", normalized).strip()
    return normalized


def int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def alert_fingerprint(alert: dict[str, Any]) -> str:
    stable = "|".join(
        [
            str(alert.get("source", "")),
            str(alert.get("service", "")),
            str(alert.get("severity", "")),
            str(alert.get("failure_class", "")),
            normalize_message(str(alert.get("message", ""))),
        ]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]


def load_state(previous_report: dict[str, Any] | None) -> dict[str, Any]:
    if not previous_report:
        return {"alerts": {}}
    state = previous_report.get("alert_state", {})
    if isinstance(state, dict) and isinstance(state.get("alerts"), dict):
        return {"alerts": dict(state["alerts"])}
    return {"alerts": {}}


def evaluate_alerts(
    previous_report: dict[str, Any] | None,
    current_alerts: list[dict[str, Any]],
    now_utc: str,
    cooldown_seconds: int,
) -> dict[str, Any]:
    state = load_state(previous_report)
    alerts: dict[str, Any] = state["alerts"]
    now = parse_utc(now_utc) or datetime.now(UTC)
    current_fingerprints: set[str] = set()
    notifications: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    for alert in current_alerts:
        fingerprint = alert_fingerprint(alert)
        current_fingerprints.add(fingerprint)
        previous = alerts.get(fingerprint, {})
        last_sent = parse_utc(str(previous.get("last_sent_at") or ""))
        previous_status = str(previous.get("status") or "")
        severity = str(alert.get("severity", "warning"))
        normalized_message = normalize_message(str(alert.get("message", "")))
        should_send = False
        reason = ""

        if previous_status in {"", "recovered"}:
            should_send = True
            reason = "new"
        elif previous.get("severity") != severity:
            should_send = True
            reason = "severity_changed"
        elif last_sent is None or now - last_sent >= timedelta(seconds=cooldown_seconds):
            should_send = True
            reason = "cooldown_elapsed"

        previous_suppressed_count = 0 if previous_status in {"", "recovered"} else int_value(previous.get("suppressed_count", 0))
        first_seen_at = now_utc if previous_status in {"", "recovered"} else previous.get("first_seen_at") or now_utc
        record = {
            "fingerprint": fingerprint,
            "source": alert.get("source"),
            "service": alert.get("service"),
            "severity": severity,
            "failure_class": alert.get("failure_class"),
            "message": alert.get("message"),
            "normalized_message": normalized_message,
            "status": severity,
            "first_seen_at": first_seen_at,
            "last_seen_at": now_utc,
            "last_sent_at": now_utc if should_send else previous.get("last_sent_at"),
            "suppressed_count": previous_suppressed_count + (0 if should_send else 1),
        }
        alerts[fingerprint] = record
        event = dict(record)
        event["notification_reason"] = reason if should_send else "cooldown"
        if should_send:
            notifications.append(event)
        else:
            suppressed.append(event)

    for fingerprint, previous in list(alerts.items()):
        if fingerprint in current_fingerprints or previous.get("status") == "recovered":
            continue
        recovered = dict(previous)
        recovered.update(
            {
                "fingerprint": fingerprint,
                "status": "recovered",
                "severity": "recovered",
                "recovered_at": now_utc,
                "last_sent_at": now_utc,
                "notification_reason": "recovered",
            }
        )
        alerts[fingerprint] = recovered
        notifications.append(recovered)

    active_records = [item for item in alerts.values() if item.get("status") != "recovered"]
    last_error = current_alerts[-1].get("message") if current_alerts else None
    last_sent_values = [str(item.get("last_sent_at")) for item in alerts.values() if item.get("last_sent_at")]
    return {
        "state": {"alerts": alerts},
        "notifications": notifications,
        "suppressed": suppressed,
        "summary": {
            "active_alert_count": len(active_records),
            "suppressed_count": len(suppressed),
            "suppressed_total_count": sum(int_value(item.get("suppressed_count", 0)) for item in active_records),
            "notification_count": len(notifications),
            "recovered_count": sum(1 for item in notifications if item.get("status") == "recovered"),
            "last_sent_at": max(last_sent_values) if last_sent_values else None,
            "last_error": last_error,
        },
    }
