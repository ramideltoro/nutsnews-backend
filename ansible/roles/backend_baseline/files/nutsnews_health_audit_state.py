#!/usr/bin/python3
"""Produce and durably merge bounded backend health-audit state.

The GitHub workflow uses ``event`` locally to reduce the full report to safe
metadata. The installed root-owned copy accepts that metadata on stdin through
the fixed ``write`` command and is the only writer for the metrics state file.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


STATE_PATH = Path("/var/lib/nutsnews/health-audit/last-run.json")
LOCK_PATH = Path("/var/lib/nutsnews/health-audit/.last-run.lock")
SCHEMA_VERSION = 1
EXPECTED_INTERVAL_SECONDS = 24 * 60 * 60
MAX_EVENT_BYTES = 2 * 1024
MAX_STATE_BYTES = 4 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_CRITICAL_CHECKS = 256
MAX_CONSECUTIVE_FAILURES = 10_000
MAX_EVENT_AGE = timedelta(hours=6)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MIN_AUDIT_TIME = datetime(2025, 1, 1, tzinfo=UTC)
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "safe_metadata_only",
        "source",
        "available",
        "conclusion",
        "last_run_at_utc",
        "critical_checks",
        "expected_interval_seconds",
    }
)
STATE_FIELDS = EVENT_FIELDS | {"last_success_at_utc", "consecutive_failures"}
CONCLUSIONS = frozenset({"success", "failure"})
DELIVERY_STATUSES = frozenset({"sent", "skipped", "not_configured", "error"})


class StateValidationError(ValueError):
    """Raised when untrusted report, event, or prior state is not safe to use."""


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _reject_json_constant(_value: str) -> None:
    raise StateValidationError("non-finite JSON value")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateValidationError("duplicate JSON key")
        result[key] = value
    return result


def parse_json_object(payload: bytes, *, limit: int, context: str) -> dict[str, Any]:
    if not payload:
        raise StateValidationError(f"{context} is empty")
    if len(payload) > limit:
        raise StateValidationError(f"{context} exceeds the size limit")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateValidationError(f"{context} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise StateValidationError(f"{context} must be a JSON object")
    return value


def read_bounded(path: Path, *, limit: int, context: str, reject_links: bool = False) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if reject_links and stat.S_ISLNK(metadata.st_mode):
        raise StateValidationError(f"{context} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise StateValidationError(f"{context} must be a regular file")
    if metadata.st_size > limit:
        raise StateValidationError(f"{context} exceeds the size limit")
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise StateValidationError(f"{context} exceeds the size limit")
    return payload


def parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise StateValidationError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise StateValidationError(f"{field} must be a valid UTC timestamp") from exc
    if parsed < MIN_AUDIT_TIME:
        raise StateValidationError(f"{field} predates the supported audit window")
    return parsed


def require_exact_fields(value: dict[str, Any], expected: frozenset[str], *, context: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise StateValidationError(f"{context} does not match the closed schema")


def require_bounded_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise StateValidationError(f"{field} is outside its integer bounds")
    return value


def validate_event(event: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    require_exact_fields(event, EVENT_FIELDS, context="event")
    if type(event.get("schema_version")) is not int or event.get("schema_version") != SCHEMA_VERSION:
        raise StateValidationError("unsupported event schema")
    if event.get("safe_metadata_only") is not True:
        raise StateValidationError("event is not marked safe metadata only")
    if event.get("source") != "github_actions":
        raise StateValidationError("event source is not allowed")
    if event.get("available") is not True:
        raise StateValidationError("event availability must be true")

    conclusion = event.get("conclusion")
    if conclusion not in CONCLUSIONS:
        raise StateValidationError("event conclusion is not allowed")
    critical_checks = require_bounded_int(
        event.get("critical_checks"),
        field="critical_checks",
        minimum=0,
        maximum=MAX_CRITICAL_CHECKS,
    )
    if conclusion == "success" and critical_checks != 0:
        raise StateValidationError("a successful event cannot contain critical checks")
    interval = require_bounded_int(
        event.get("expected_interval_seconds"),
        field="expected_interval_seconds",
        minimum=EXPECTED_INTERVAL_SECONDS,
        maximum=EXPECTED_INTERVAL_SECONDS,
    )

    current_time = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    run_at = parse_timestamp(event.get("last_run_at_utc"), field="last_run_at_utc")
    if run_at > current_time + MAX_FUTURE_SKEW:
        raise StateValidationError("event timestamp is too far in the future")
    if run_at < current_time - MAX_EVENT_AGE:
        raise StateValidationError("event timestamp is stale")

    return {
        "schema_version": SCHEMA_VERSION,
        "safe_metadata_only": True,
        "source": "github_actions",
        "available": True,
        "conclusion": conclusion,
        "last_run_at_utc": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "critical_checks": critical_checks,
        "expected_interval_seconds": interval,
    }


def validate_state(state_value: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    require_exact_fields(state_value, STATE_FIELDS, context="existing state")
    event_projection = {field: state_value[field] for field in EVENT_FIELDS}

    # Existing state may be old because its purpose is to make missed schedules
    # visible. Validate shape and future skew without imposing event freshness.
    validation_now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    run_at = parse_timestamp(event_projection["last_run_at_utc"], field="last_run_at_utc")
    if run_at > validation_now + MAX_FUTURE_SKEW:
        raise StateValidationError("existing state timestamp is too far in the future")
    event_projection["last_run_at_utc"] = run_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    require_exact_fields(event_projection, EVENT_FIELDS, context="existing state event")
    if (
        type(event_projection.get("schema_version")) is not int
        or event_projection.get("schema_version") != SCHEMA_VERSION
    ):
        raise StateValidationError("unsupported existing state schema")
    if event_projection.get("safe_metadata_only") is not True:
        raise StateValidationError("existing state is not marked safe metadata only")
    if event_projection.get("source") != "github_actions" or event_projection.get("available") is not True:
        raise StateValidationError("existing state provenance is invalid")
    conclusion = event_projection.get("conclusion")
    if conclusion not in CONCLUSIONS:
        raise StateValidationError("existing state conclusion is not allowed")
    critical_checks = require_bounded_int(
        event_projection.get("critical_checks"),
        field="critical_checks",
        minimum=0,
        maximum=MAX_CRITICAL_CHECKS,
    )
    require_bounded_int(
        event_projection.get("expected_interval_seconds"),
        field="expected_interval_seconds",
        minimum=EXPECTED_INTERVAL_SECONDS,
        maximum=EXPECTED_INTERVAL_SECONDS,
    )
    failures = require_bounded_int(
        state_value.get("consecutive_failures"),
        field="consecutive_failures",
        minimum=0,
        maximum=MAX_CONSECUTIVE_FAILURES,
    )

    last_success_raw = state_value.get("last_success_at_utc")
    last_success_at: datetime | None
    if last_success_raw is None:
        last_success_at = None
    else:
        last_success_at = parse_timestamp(last_success_raw, field="last_success_at_utc")
        if last_success_at > run_at:
            raise StateValidationError("last success cannot postdate the last run")

    if conclusion == "success":
        if critical_checks != 0 or failures != 0 or last_success_at != run_at:
            raise StateValidationError("successful existing state is inconsistent")
    elif failures < 1:
        raise StateValidationError("failed existing state must count a failure")

    return {
        **event_projection,
        "last_success_at_utc": (
            last_success_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_success_at else None
        ),
        "consecutive_failures": failures,
    }


def report_to_event(report: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if type(report.get("version")) is not int or report.get("version") != 1:
        raise StateValidationError("unsupported report version")
    summary = report.get("summary")
    delivery = report.get("delivery")
    if not isinstance(summary, dict) or not isinstance(delivery, dict):
        raise StateValidationError("report summary or delivery is invalid")
    critical_checks = require_bounded_int(
        summary.get("critical"),
        field="report critical checks",
        minimum=0,
        maximum=MAX_CRITICAL_CHECKS,
    )
    delivery_status = delivery.get("status")
    if delivery_status not in DELIVERY_STATUSES:
        raise StateValidationError("report delivery status is invalid")
    last_error = report.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        raise StateValidationError("report last error is invalid")

    expected_conclusion = (
        "success"
        if critical_checks == 0 and last_error is None and delivery_status != "error"
        else "failure"
    )
    if report.get("conclusion") != expected_conclusion:
        raise StateValidationError("report conclusion is inconsistent")

    return validate_event(
        {
            "schema_version": SCHEMA_VERSION,
            "safe_metadata_only": True,
            "source": "github_actions",
            "available": True,
            "conclusion": expected_conclusion,
            "last_run_at_utc": report.get("last_report_run_at"),
            "critical_checks": critical_checks,
            "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
        },
        now=now,
    )


def merge_event(
    event_value: dict[str, Any],
    previous_value: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    event = validate_event(event_value, now=now)
    previous = validate_state(previous_value, now=now) if previous_value is not None else None
    event_time = parse_timestamp(event["last_run_at_utc"], field="last_run_at_utc")

    if previous is not None:
        previous_time = parse_timestamp(previous["last_run_at_utc"], field="last_run_at_utc")
        if event_time < previous_time:
            raise StateValidationError("event is older than existing state")
        if event_time == previous_time:
            replay_matches = (
                event["conclusion"] == previous["conclusion"]
                and event["critical_checks"] == previous["critical_checks"]
                and event["expected_interval_seconds"] == previous["expected_interval_seconds"]
            )
            if not replay_matches:
                raise StateValidationError("event conflicts with existing state at the same timestamp")
            return previous, False

    if event["conclusion"] == "success":
        last_success = event["last_run_at_utc"]
        consecutive_failures = 0
    else:
        last_success = previous.get("last_success_at_utc") if previous else None
        consecutive_failures = (previous.get("consecutive_failures", 0) if previous else 0) + 1
        if consecutive_failures > MAX_CONSECUTIVE_FAILURES:
            raise StateValidationError("consecutive failure count would exceed its bound")

    merged = {
        **event,
        "last_success_at_utc": last_success,
        "consecutive_failures": consecutive_failures,
    }
    return validate_state(merged, now=now), True


def _durable_replace(path: Path, value: dict[str, Any], *, mode: int) -> None:
    parent = path.parent
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise StateValidationError("output parent must be a real directory")
    try:
        target_metadata = path.lstat()
    except FileNotFoundError:
        target_metadata = None
    if target_metadata is not None and (
        stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(target_metadata.st_mode)
    ):
        raise StateValidationError("output target must be a regular file")

    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise StateValidationError("serialized state exceeds its size limit")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_existing_state(path: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    try:
        payload = read_bounded(path, limit=MAX_STATE_BYTES, context="existing state", reject_links=True)
    except FileNotFoundError:
        return None
    value = parse_json_object(payload, limit=MAX_STATE_BYTES, context="existing state")
    return validate_state(value, now=now)


def write_state(
    event_value: dict[str, Any],
    *,
    state_path: Path = STATE_PATH,
    lock_path: Path = LOCK_PATH,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    parent = state_path.parent
    if lock_path.parent != parent:
        raise StateValidationError("lock and state must share a directory")
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise StateValidationError("state parent must be a real directory")

    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_nlink != 1
            or lock_metadata.st_uid != os.geteuid()
        ):
            raise StateValidationError("lock must be a singly linked file owned by the writer")
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "r+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            previous = load_existing_state(state_path, now=now)
            merged, changed = merge_event(event_value, previous, now=now)
            if changed:
                _durable_replace(state_path, merged, mode=0o644)
            return merged, changed
    except Exception:
        # os.fdopen owns lock_fd after it succeeds. If it fails, close the raw fd.
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise


def produce_event(report_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    report_payload = read_bounded(report_path, limit=MAX_REPORT_BYTES, context="report")
    report = parse_json_object(report_payload, limit=MAX_REPORT_BYTES, context="report")
    return report_to_event(report, now=now)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    event_parser = subparsers.add_parser("event", help="create a sanitized event from a report")
    event_parser.add_argument("--report", required=True)
    subparsers.add_parser("write", help="merge a sanitized stdin event into the fixed host state")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "event":
            if os.geteuid() == 0:
                raise StateValidationError("event production refuses elevated execution")
            event = produce_event(Path(args.report))
            print(json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 0

        payload = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        event = parse_json_object(payload, limit=MAX_EVENT_BYTES, context="event")
        state, changed = write_state(event)
        action = "updated" if changed else "unchanged"
        print(f"health-audit state {action}: conclusion={state['conclusion']}")
        return 0
    except (OSError, StateValidationError) as exc:
        print(f"health-audit state rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
