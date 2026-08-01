#!/usr/bin/env python3
"""Run bounded, self-recovering backend observability failure drills.

Only the fixed ``translation`` shadow worker is stopped by this hook. The
remaining drills are telemetry fixtures: they never publish RabbitMQ messages,
alter queues, pause the sync relay, change readiness, or touch application data.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


TARGET = "backend.nutsnews.com"
DRILLS = (
    "worker-unavailable",
    "rabbitmq-zero-consumer",
    "rabbitmq-growing-dlq",
    "postgres-relay-lag",
    "backend-readiness-failed",
)
TELEMETRY_ONLY_DRILLS = frozenset(DRILLS) - {"worker-unavailable"}
DRILL_ID_PATTERN = re.compile(r"^nnobs-[0-9]{10,20}-[a-f0-9]{8}$")
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
FIXED_DURATION_SECONDS = 900
RECOVERY_DELAY_SECONDS = 1080
WORKER_SERVICE = "translation"
WORKER_STAGE = "translation"
WORKER_LIVENESS_URL = "http://127.0.0.1:18086/live"
RECOVERY_UNIT_PREFIX = "nutsnews-observability-drill-recovery-"
SCRIPT_PATH = Path("/usr/local/sbin/nutsnews-observability-failure-drill")


@dataclass(frozen=True)
class Paths:
    state_dir: Path = Path("/var/lib/nutsnews/observability-drills")
    state: Path = Path("/var/lib/nutsnews/observability-drills/state.json")
    lock: Path = Path("/var/lib/nutsnews/observability-drills/operation.lock")
    metrics: Path = Path("/var/lib/nutsnews/metrics/observability-failure-drills.prom")
    manifest: Path = Path("/etc/nutsnews-worker-uplift/services.json")
    compose: Path = Path("/opt/nutsnews-worker-uplift/compose.yml")


DEFAULT_PATHS = Paths()


class DrillFailure(RuntimeError):
    """A failure identified by a bounded check name."""

    def __init__(self, check: str):
        super().__init__(check)
        self.check = check


class CommandRunner:
    """Run fixed argv commands without a shell or inherited secret values."""

    _environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    def run(self, argv: Sequence[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._environment,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def render_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc)
    return normalized if render_timestamp(normalized) == value else None


def check(name: str, passed: bool) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail"}


def fail(name: str, checks: list[dict[str, str]]) -> None:
    checks.append(check(name, False))
    raise DrillFailure(name)


def inactive_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "safe_metadata_only": True,
        "drill": None,
        "drill_id": None,
        "recovery_required": False,
        "recovered": True,
        "injected_at_utc": None,
        "recovery_deadline_utc": None,
        "duration_seconds": FIXED_DURATION_SECONDS,
        "recovery_unit": None,
    }


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise DrillFailure("state_valid")
    required = {
        "schema_version",
        "safe_metadata_only",
        "drill",
        "drill_id",
        "recovery_required",
        "recovered",
        "injected_at_utc",
        "recovery_deadline_utc",
        "duration_seconds",
        "recovery_unit",
    }
    if set(state) != required:
        raise DrillFailure("state_valid")
    if state.get("schema_version") != 1 or state.get("safe_metadata_only") is not True:
        raise DrillFailure("state_valid")
    if state.get("duration_seconds") != FIXED_DURATION_SECONDS:
        raise DrillFailure("state_valid")
    if not isinstance(state.get("recovery_required"), bool) or not isinstance(state.get("recovered"), bool):
        raise DrillFailure("state_valid")

    active = state.get("recovery_required") is True
    if active:
        drill = state.get("drill")
        drill_id = state.get("drill_id")
        expected_unit = recovery_unit(str(drill_id)) if isinstance(drill_id, str) else ""
        injected_at = parse_timestamp(state.get("injected_at_utc"))
        recovery_deadline = parse_timestamp(state.get("recovery_deadline_utc"))
        if (
            drill not in DRILLS
            or not isinstance(drill_id, str)
            or DRILL_ID_PATTERN.fullmatch(drill_id) is None
            or state.get("recovered") is not False
            or injected_at is None
            or recovery_deadline is None
            or recovery_deadline - injected_at != timedelta(seconds=RECOVERY_DELAY_SECONDS)
            or state.get("recovery_unit") != expected_unit
        ):
            raise DrillFailure("state_valid")
    else:
        if state.get("recovered") is not True or state.get("recovery_unit") is not None:
            raise DrillFailure("state_valid")
        drill = state.get("drill")
        drill_id = state.get("drill_id")
        injected_at = parse_timestamp(state.get("injected_at_utc"))
        recovery_deadline = parse_timestamp(state.get("recovery_deadline_utc"))
        pristine = (
            drill is None
            and drill_id is None
            and state.get("injected_at_utc") is None
            and state.get("recovery_deadline_utc") is None
        )
        recovered = (
            drill in DRILLS
            and isinstance(drill_id, str)
            and DRILL_ID_PATTERN.fullmatch(drill_id) is not None
            and injected_at is not None
            and recovery_deadline is not None
            and recovery_deadline - injected_at == timedelta(seconds=RECOVERY_DELAY_SECONDS)
        )
        if not pristine and not recovered:
            raise DrillFailure("state_valid")
    return state


def read_state(paths: Paths) -> dict[str, Any]:
    if not paths.state.exists():
        return inactive_state()
    if paths.state.is_symlink() or not paths.state.is_file():
        raise DrillFailure("state_valid")
    try:
        return validate_state(json.loads(paths.state.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        raise DrillFailure("state_valid") from None


def ensure_directory(path: Path, mode: int) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise DrillFailure("storage_safe")
        return
    path.mkdir(parents=True, mode=mode)
    os.chmod(path, mode)


def atomic_write(path: Path, content: str, mode: int) -> None:
    ensure_directory(path.parent, 0o750)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DrillFailure("storage_safe")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def write_state(paths: Paths, state: dict[str, Any]) -> None:
    validated = validate_state(state)
    atomic_write(paths.state, json.dumps(validated, indent=2, sort_keys=True) + "\n", 0o640)


def render_metrics(active_drill: str | None) -> str:
    if active_drill is not None and active_drill not in DRILLS:
        raise DrillFailure("fixture_series_valid")
    lines = [
        "# HELP nutsnews_observability_failure_drill_active Whether a bounded production observability failure drill is active.",
        "# TYPE nutsnews_observability_failure_drill_active gauge",
    ]
    for drill in DRILLS:
        value = 1 if drill == active_drill else 0
        lines.append(
            "nutsnews_observability_failure_drill_active"
            f'{{drill="{drill}"}} {value}'
        )
    return "\n".join(lines) + "\n"


def write_metrics(paths: Paths, active_drill: str | None) -> None:
    atomic_write(paths.metrics, render_metrics(active_drill), 0o644)


def metric_states(paths: Paths) -> dict[str, int] | None:
    if not paths.metrics.exists() or paths.metrics.is_symlink() or not paths.metrics.is_file():
        return None
    try:
        rendered = paths.metrics.read_text(encoding="utf-8")
    except OSError:
        return None
    for active_drill in (None, *DRILLS):
        if rendered == render_metrics(active_drill):
            return {candidate: int(candidate == active_drill) for candidate in DRILLS}
    return None


def metrics_match(paths: Paths, active_drill: str | None) -> bool:
    observed = metric_states(paths)
    expected = {drill: int(drill == active_drill) for drill in DRILLS}
    return observed == expected


def recovery_unit(drill_id: str) -> str:
    return f"{RECOVERY_UNIT_PREFIX}{drill_id}"


def compose_base(paths: Paths) -> list[str]:
    return [
        "/usr/bin/docker",
        "compose",
        "-f",
        str(paths.compose),
        "-p",
        "nutsnews-worker-uplift",
    ]


def load_shadow_manifest(paths: Paths, checks: list[dict[str, str]]) -> dict[str, Any]:
    if (
        paths.manifest.is_symlink()
        or not paths.manifest.is_file()
        or paths.compose.is_symlink()
        or not paths.compose.is_file()
    ):
        fail("shadow_manifest", checks)
    try:
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("shadow_manifest", checks)
    services = manifest.get("services") if isinstance(manifest, dict) else None
    translations = [
        service
        for service in services or []
        if isinstance(service, dict) and service.get("name") == WORKER_SERVICE
    ]
    backend_api = manifest.get("backend_api") if isinstance(manifest, dict) else None
    service = translations[0] if len(translations) == 1 else {}
    postgres = service.get("postgres") if isinstance(service, dict) else None
    environment = service.get("env") if isinstance(service, dict) else None
    safe = (
        manifest.get("schema_version") == 1
        and manifest.get("generated_by") == "backend_worker_runtime"
        and manifest.get("mode") == "shadow"
        and manifest.get("cutover_state") == "shadow"
        and manifest.get("production_writes_enabled") is False
        and isinstance(backend_api, dict)
        and backend_api.get("writes_enabled") is False
        and len(translations) == 1
        and service.get("stage") == WORKER_STAGE
        and service.get("runtime_mode") == "shadow"
        and isinstance(postgres, dict)
        and postgres.get("production_write_path") is False
        and isinstance(environment, dict)
        and environment.get("NUTSNEWS_TRANSLATION_SHADOW_MODE") == "true"
    )
    if not safe:
        fail("shadow_manifest", checks)
    checks.append(check("shadow_manifest", True))
    return manifest


def worker_running(paths: Paths, runner: CommandRunner) -> bool:
    result = runner.run(
        compose_base(paths) + ["ps", "--status", "running", "--services", WORKER_SERVICE],
        timeout=30,
    )
    services = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return result.returncode == 0 and services == [WORKER_SERVICE]


def liveness_healthy(timeout_seconds: float = 3.0) -> bool:
    request = urllib.request.Request(WORKER_LIVENESS_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return False
            payload = response.read(65_537)
            if len(payload) > 65_536:
                return False
            body = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return False
    return bool(
        isinstance(body, dict)
        and body.get("probe") == "liveness"
        and body.get("status") == "ok"
    )


def wait_for_liveness(
    expected: bool,
    probe: Callable[[], bool],
    *,
    attempts: int = 30,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    for attempt in range(attempts):
        if probe() is expected:
            return True
        if attempt + 1 < attempts:
            sleeper(2)
    return False


def preflight(
    drill: str,
    paths: Paths,
    runner: CommandRunner,
    probe: Callable[[], bool],
    checks: list[dict[str, str]],
) -> None:
    state = read_state(paths)
    if state["recovery_required"] is True:
        fail("single_active_drill", checks)
    checks.append(check("single_active_drill", True))
    if not metrics_match(paths, None):
        fail("fixture_series_clear", checks)
    checks.append(check("fixture_series_clear", True))
    if drill == "worker-unavailable":
        load_shadow_manifest(paths, checks)
        if not worker_running(paths, runner):
            fail("translation_running", checks)
        checks.append(check("translation_running", True))
        if not probe():
            fail("translation_liveness", checks)
        checks.append(check("translation_liveness", True))


def schedule_recovery(
    drill: str,
    drill_id: str,
    runner: CommandRunner,
) -> str:
    unit = recovery_unit(drill_id)
    result = runner.run(
        [
            "/usr/bin/systemd-run",
            "--quiet",
            f"--unit={unit}",
            f"--on-active={RECOVERY_DELAY_SECONDS}s",
            "--collect",
            "--timer-property=AccuracySec=1s",
            "--property=Type=oneshot",
            str(SCRIPT_PATH),
            "--action",
            "recover",
            "--drill",
            drill,
            "--drill-id",
            drill_id,
            "--duration-seconds",
            str(FIXED_DURATION_SECONDS),
            "--confirm-target",
            TARGET,
            "--confirm-drill",
            drill,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise DrillFailure("recovery_scheduled")
    return unit


def stop_recovery_timer(unit: str | None, runner: CommandRunner) -> bool:
    if not unit or not unit.startswith(RECOVERY_UNIT_PREFIX):
        return False
    result = runner.run(["/usr/bin/systemctl", "stop", f"{unit}.timer"], timeout=30)
    return result.returncode == 0


def active_state(drill: str, drill_id: str, now: datetime, unit: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "safe_metadata_only": True,
        "drill": drill,
        "drill_id": drill_id,
        "recovery_required": True,
        "recovered": False,
        "injected_at_utc": render_timestamp(now),
        "recovery_deadline_utc": render_timestamp(now + timedelta(seconds=RECOVERY_DELAY_SECONDS)),
        "duration_seconds": FIXED_DURATION_SECONDS,
        "recovery_unit": unit,
    }


def recovered_state(state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    result["recovery_required"] = False
    result["recovered"] = True
    result["recovery_unit"] = None
    return result


def recover_exact(
    state: dict[str, Any],
    paths: Paths,
    runner: CommandRunner,
    probe: Callable[[], bool],
    checks: list[dict[str, str]],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    drill = str(state["drill"])
    if drill == "worker-unavailable":
        if paths.compose.is_symlink() or not paths.compose.is_file():
            fail("translation_restored", checks)
        result = runner.run(
            compose_base(paths)
            + ["up", "-d", "--no-deps", "--pull", "never", WORKER_SERVICE],
            timeout=300,
        )
        if result.returncode != 0 or not worker_running(paths, runner):
            fail("translation_restored", checks)
        checks.append(check("translation_restored", True))
        if not wait_for_liveness(True, probe, sleeper=sleeper):
            fail("translation_liveness_restored", checks)
        checks.append(check("translation_liveness_restored", True))

    # The worker fixture is deliberately not cleared until worker liveness is
    # restored. Telemetry-only fixtures reach this point without touching their
    # represented production component.
    write_metrics(paths, None)
    if not metrics_match(paths, None):
        fail("fixture_series_clear", checks)
    checks.append(check("fixture_series_clear", True))
    write_state(paths, recovered_state(state))
    timer_stopped = stop_recovery_timer(str(state.get("recovery_unit") or ""), runner)
    checks.append({"name": "recovery_timer_stopped", "status": "pass" if timer_stopped else "not_applicable"})
    return read_state(paths)


def inject(
    drill: str,
    drill_id: str,
    paths: Paths,
    runner: CommandRunner,
    probe: Callable[[], bool],
    now: datetime,
    checks: list[dict[str, str]],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    preflight(drill, paths, runner, probe, checks)
    try:
        unit = schedule_recovery(drill, drill_id, runner)
    except DrillFailure:
        checks.append(check("recovery_scheduled", False))
        raise
    checks.append(check("recovery_scheduled", True))

    state = active_state(drill, drill_id, now, unit)
    try:
        write_state(paths, state)
    except Exception:
        stop_recovery_timer(unit, runner)
        raise
    try:
        if drill == "worker-unavailable":
            result = runner.run(
                compose_base(paths) + ["stop", "-t", "30", WORKER_SERVICE],
                timeout=120,
            )
            if result.returncode != 0 or worker_running(paths, runner):
                fail("translation_stopped", checks)
            checks.append(check("translation_stopped", True))
            if not wait_for_liveness(False, probe, attempts=10, sleeper=sleeper):
                fail("translation_liveness_failed", checks)
            checks.append(check("translation_liveness_failed", True))

        write_metrics(paths, drill)
        if not metrics_match(paths, drill):
            fail("fixture_series_active", checks)
        checks.append(check("fixture_series_active", True))
        return state
    except Exception:
        recovery_checks: list[dict[str, str]] = []
        try:
            recover_exact(state, paths, runner, probe, recovery_checks, sleeper)
        except Exception:
            pass
        raise


def status_checks(
    requested_drill: str,
    requested_id: str,
    state: dict[str, Any],
    paths: Paths,
    runner: CommandRunner,
    probe: Callable[[], bool],
    checks: list[dict[str, str]],
) -> None:
    active = state["recovery_required"] is True
    if state.get("drill") is not None and (
        state["drill"] != requested_drill or state["drill_id"] != requested_id
    ):
        fail("drill_identity_matches", checks)
    checks.append(check("drill_identity_matches", True))
    expected_drill = str(state["drill"]) if active else None
    if not metrics_match(paths, expected_drill):
        fail("fixture_series_active" if active else "fixture_series_clear", checks)
    checks.append(check("fixture_series_active" if active else "fixture_series_clear", True))
    if requested_drill == "worker-unavailable" and active:
        load_shadow_manifest(paths, checks)
        if worker_running(paths, runner):
            fail("translation_stopped", checks)
        checks.append(check("translation_stopped", True))
        if probe():
            fail("translation_liveness_failed", checks)
        checks.append(check("translation_liveness_failed", True))


def operation_lock(paths: Paths):
    ensure_directory(paths.state_dir, 0o750)
    if paths.lock.is_symlink() or (paths.lock.exists() and not paths.lock.is_file()):
        raise DrillFailure("storage_safe")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(paths.lock, flags, 0o640)
    except OSError:
        raise DrillFailure("storage_safe") from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DrillFailure("storage_safe")
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    os.fchmod(handle.fileno(), 0o640)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def base_report(action: str, drill: str | None, drill_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "safe_metadata_only": True,
        "action": action,
        "drill": drill,
        "drill_id": drill_id,
        "status": "pass",
        "dry_run": action == "plan",
        "recovery_scheduled": False,
        "recovery_required": False,
        "recovered": False,
        "injected_at_utc": None,
        "recovery_deadline_utc": None,
        "duration_seconds": FIXED_DURATION_SECONDS,
        "checks": [],
    }


def apply_state_to_report(report: dict[str, Any], state: dict[str, Any]) -> None:
    report["recovery_scheduled"] = state["recovery_required"] is True
    report["recovery_required"] = state["recovery_required"] is True
    report["recovered"] = state["recovered"] is True
    report["injected_at_utc"] = state["injected_at_utc"]
    report["recovery_deadline_utc"] = state["recovery_deadline_utc"]


def validate_cli(args: argparse.Namespace) -> None:
    if args.duration_seconds != FIXED_DURATION_SECONDS:
        raise DrillFailure("duration_fixed")
    if args.action == "watchdog":
        if args.execute or args.drill or args.drill_id or args.confirm_target or args.confirm_drill:
            raise DrillFailure("watchdog_internal_only")
        return
    if args.drill not in DRILLS:
        raise DrillFailure("drill_allowed")
    if not isinstance(args.drill_id, str) or DRILL_ID_PATTERN.fullmatch(args.drill_id) is None:
        raise DrillFailure("drill_id_bounded")
    if args.confirm_target != TARGET or args.confirm_drill != args.drill:
        raise DrillFailure("confirmation")
    if args.action == "inject" and not args.execute:
        raise DrillFailure("execute_confirmation")
    if args.action != "inject" and args.execute:
        raise DrillFailure("execute_scope")


def require_root(action: str) -> None:
    if action in {"inject", "recover", "watchdog"} and os.geteuid() != 0:
        raise DrillFailure("root_required")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "inject", "status", "recover", "watchdog"), default="plan")
    parser.add_argument("--drill", choices=DRILLS)
    parser.add_argument("--drill-id")
    parser.add_argument("--duration-seconds", type=int, default=FIXED_DURATION_SECONDS)
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--confirm-drill", default="")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def run(
    args: argparse.Namespace,
    *,
    paths: Paths = DEFAULT_PATHS,
    runner: CommandRunner | None = None,
    probe: Callable[[], bool] = liveness_healthy,
    now_fn: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    validate_cli(args)
    require_root(args.action)
    runner = runner or CommandRunner()
    report = base_report(args.action, args.drill, args.drill_id)
    checks: list[dict[str, str]] = report["checks"]

    if args.action == "plan":
        preflight(str(args.drill), paths, runner, probe, checks)
        return report

    if args.action == "status":
        state = read_state(paths)
        status_checks(str(args.drill), str(args.drill_id), state, paths, runner, probe, checks)
        apply_state_to_report(report, state)
        return report

    lock = operation_lock(paths)
    try:
        if args.action == "inject":
            state = inject(
                str(args.drill),
                str(args.drill_id),
                paths,
                runner,
                probe,
                now_fn(),
                checks,
                sleeper,
            )
            apply_state_to_report(report, state)
            return report

        state = read_state(paths)
        if args.action == "recover":
            if state["recovery_required"] is not True:
                if state.get("drill") not in {None, args.drill} or state.get("drill_id") not in {None, args.drill_id}:
                    fail("drill_identity_matches", checks)
                write_metrics(paths, None)
                checks.append(check("fixture_series_clear", metrics_match(paths, None)))
                apply_state_to_report(report, state)
                return report
            if state["drill"] != args.drill or state["drill_id"] != args.drill_id:
                fail("drill_identity_matches", checks)
            checks.append(check("drill_identity_matches", True))
            state = recover_exact(state, paths, runner, probe, checks, sleeper)
            apply_state_to_report(report, state)
            return report

        # The watchdog has no caller-controlled target. It only acts on a
        # strictly validated, expired persistent state record.
        report["drill"] = state.get("drill")
        report["drill_id"] = state.get("drill_id")
        if state["recovery_required"] is not True:
            checks.append({"name": "recovery_deadline_expired", "status": "not_applicable"})
            apply_state_to_report(report, state)
            return report
        deadline = parse_timestamp(state["recovery_deadline_utc"])
        if deadline is None:
            fail("state_valid", checks)
        if now_fn() < deadline:
            checks.append({"name": "recovery_deadline_expired", "status": "not_applicable"})
            apply_state_to_report(report, state)
            return report
        checks.append(check("recovery_deadline_expired", True))
        state = recover_exact(state, paths, runner, probe, checks, sleeper)
        apply_state_to_report(report, state)
        return report
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv)
        report = run(args)
    except DrillFailure as exc:
        action = getattr(args, "action", "plan")
        drill = getattr(args, "drill", None)
        drill_id = getattr(args, "drill_id", None)
        report = base_report(action, drill, drill_id)
        report["status"] = "fail"
        report["checks"] = [check(exc.check, False)]
    except Exception:
        action = getattr(args, "action", "plan")
        drill = getattr(args, "drill", None)
        drill_id = getattr(args, "drill_id", None)
        report = base_report(action, drill, drill_id)
        report["status"] = "fail"
        report["checks"] = [check("internal_failure", False)]
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
