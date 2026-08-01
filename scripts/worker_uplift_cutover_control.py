#!/usr/bin/env python3
"""Run the fixed worker-uplift ownership state machine without arbitrary commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "docs" / "worker-uplift-cutover-controls.json"
DEFAULT_DECISION = ROOT / "docs" / "worker-uplift-final-cutover-decision.json"
INSTALLED_CONTRACT = Path("/etc/nutsnews-worker-uplift/cutover-controls.json")
INSTALLED_DECISION = Path("/etc/nutsnews-worker-uplift/final-cutover-decision.json")
DEFAULT_REPORT = Path("/var/lib/nutsnews/worker-uplift-cutover/last-report.json")
API_DROPIN = Path("/etc/systemd/system/nutsnews-worker-db-api.service.d/worker-uplift-cutover.conf")
PRODUCTION_OVERRIDE = Path("/opt/nutsnews-worker-uplift/cutover-production.override.yml")
BASE_COMPOSE = Path("/opt/nutsnews-worker-uplift/compose.yml")
RUNTIME_MANAGER = Path("/usr/local/sbin/nutsnews-worker-runtime")
RUNTIME_MANIFEST = Path("/etc/nutsnews-worker-uplift/services.json")
RUNTIME_REPORT_DIR = Path("/var/lib/nutsnews/worker-uplift-runtime/reports")
DB_ENV_FILE = Path("/etc/nutsnews-worker-uplift/cutover.env")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATES = {"shadow", "fenced", "cutover_active", "rollback_pending"}


class ControlError(RuntimeError):
    """A safe, value-free cutover control failure."""


@dataclass(frozen=True)
class CommandResult:
    name: str
    returncode: int
    elapsed_ms: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ControlError(f"{field} must be an absolute UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ControlError(f"{field} must be an absolute UTC timestamp") from exc
    return parsed


def require_sha256(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ControlError(f"{field} must be a lowercase SHA-256 digest")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"could not read {path.name}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"{path.name} must contain a JSON object")
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    temporary.chmod(0o640)
    temporary.replace(path)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ControlError("cutover database environment is unavailable") from exc
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ControlError("cutover database environment is invalid")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", name) or "\n" in value or "\r" in value:
            raise ControlError("cutover database environment is invalid")
        values[name] = value
    return values


def safe_state(row: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "control_id",
        "generation",
        "state",
        "active_ingestion_owner",
        "legacy_dispatch_enabled",
        "uplift_scheduler_enabled",
        "uplift_production_writes_enabled",
        "publication_write_mode",
        "candidate_sha256",
        "watermark_sha256",
        "rollback_deadline_utc",
        "observation_start_utc",
        "observation_end_utc",
        "last_transition_id",
        "updated_at_utc",
    )
    return {key: row.get(key) for key in allowed}


def validate_state(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    state = row.get("state")
    owner = row.get("active_ingestion_owner")
    legacy = row.get("legacy_dispatch_enabled")
    scheduler = row.get("uplift_scheduler_enabled")
    writes = row.get("uplift_production_writes_enabled")
    publication = row.get("publication_write_mode")
    if state not in STATES:
        errors.append("unknown control state")
    if owner not in {"legacy_shards", "worker_uplift"}:
        errors.append("unknown active ingestion owner")
    if not isinstance(legacy, bool) or not isinstance(scheduler, bool) or not isinstance(writes, bool):
        errors.append("control booleans must be explicit")
    if publication not in {"shadow_comparison", "production"}:
        errors.append("unknown publication write mode")
    if writes and owner != "worker_uplift":
        errors.append("uplift writes require worker_uplift ownership")
    if writes and legacy is not False:
        errors.append("uplift writes require legacy dispatch fenced")
    if legacy and writes:
        errors.append("legacy dispatch and uplift writes cannot overlap")
    if publication == "production" and not writes:
        errors.append("production publication requires uplift writes")
    expected = {
        "shadow": ("legacy_shards", True, True, False, "shadow_comparison"),
        "fenced": ("legacy_shards", False, False, False, "shadow_comparison"),
        "cutover_active": ("worker_uplift", False, True, True, "production"),
        "rollback_pending": ("legacy_shards", False, False, False, "shadow_comparison"),
    }
    if state in expected and (
        owner,
        legacy,
        scheduler,
        writes,
        publication,
    ) != expected[state]:
        errors.append(f"state fields do not match {state}")
    return errors


def transition_state(
    row: dict[str, Any],
    transition: str,
    *,
    candidate_sha256: str,
    watermark_sha256: str,
    rollback_deadline_utc: str,
) -> dict[str, Any]:
    current = str(row.get("state") or "")
    mapping = {
        "fence": ("shadow", "fenced"),
        "activate": ("fenced", "cutover_active"),
        "rollback-prepare": ("cutover_active", "rollback_pending"),
        "rollback-finalize": ("rollback_pending", "shadow"),
    }
    if transition not in mapping:
        raise ControlError("unsupported fixed transition")
    expected, target = mapping[transition]
    if current != expected:
        if current == target:
            return dict(row)
        raise ControlError(f"transition {transition} requires state {expected}")
    target_fields = {
        "shadow": ("legacy_shards", True, True, False, "shadow_comparison"),
        "fenced": ("legacy_shards", False, False, False, "shadow_comparison"),
        "cutover_active": ("worker_uplift", False, True, True, "production"),
        "rollback_pending": ("legacy_shards", False, False, False, "shadow_comparison"),
    }[target]
    next_row = dict(row)
    next_row.update(
        {
            "generation": int(row.get("generation", 0)) + 1,
            "state": target,
            "active_ingestion_owner": target_fields[0],
            "legacy_dispatch_enabled": target_fields[1],
            "uplift_scheduler_enabled": target_fields[2],
            "uplift_production_writes_enabled": target_fields[3],
            "publication_write_mode": target_fields[4],
            "candidate_sha256": candidate_sha256,
            "watermark_sha256": watermark_sha256,
            "rollback_deadline_utc": rollback_deadline_utc,
            "last_transition_id": transition,
            "updated_at_utc": utc_now(),
        }
    )
    errors = validate_state(next_row)
    if errors:
        raise ControlError("transition would violate single-writer invariants")
    return next_row


class CutoverDatabase:
    def __init__(self, env_file: Path = DB_ENV_FILE) -> None:
        values = read_env_file(env_file)
        required = (
            "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_HOST",
            "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PORT",
            "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_NAME",
            "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_USER",
            "NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PASSWORD",
        )
        if any(not values.get(name) for name in required):
            raise ControlError("cutover database environment is incomplete")
        self.values = values

    def connect(self):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:
            raise ControlError("PostgreSQL client is unavailable") from exc
        return psycopg2.connect(
            host=self.values["NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_HOST"],
            port=int(self.values["NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PORT"]),
            dbname=self.values["NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_NAME"],
            user=self.values["NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_USER"],
            password=self.values["NUTSNEWS_WORKER_UPLIFT_CUTOVER_DB_PASSWORD"],
            connect_timeout=5,
            options="-c statement_timeout=30000",
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def read(self) -> dict[str, Any]:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select control_id, generation, state, active_ingestion_owner,
                           legacy_dispatch_enabled, uplift_scheduler_enabled,
                           uplift_production_writes_enabled, publication_write_mode,
                           candidate_sha256, watermark_sha256,
                           to_char(rollback_deadline at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') as rollback_deadline_utc,
                           to_char(observation_start at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') as observation_start_utc,
                           to_char(observation_end at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') as observation_end_utc,
                           last_transition_id,
                           to_char(updated_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') as updated_at_utc
                    from worker_uplift_final.cutover_control
                    where control_id = 'production'
                    """
                )
                row = cursor.fetchone()
        if row is None:
            raise ControlError("cutover control production row is missing")
        result = dict(row)
        errors = validate_state(result)
        if errors:
            raise ControlError("cutover control row violates single-writer invariants")
        return result

    def transition(
        self,
        transition: str,
        *,
        candidate_sha256: str,
        watermark_sha256: str,
        rollback_deadline_utc: str,
    ) -> dict[str, Any]:
        current = self.read()
        target = transition_state(
            current,
            transition,
            candidate_sha256=candidate_sha256,
            watermark_sha256=watermark_sha256,
            rollback_deadline_utc=rollback_deadline_utc,
        )
        if int(target["generation"]) == int(current["generation"]):
            return current
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update worker_uplift_final.cutover_control
                    set generation = %s,
                        state = %s,
                        active_ingestion_owner = %s,
                        legacy_dispatch_enabled = %s,
                        uplift_scheduler_enabled = %s,
                        uplift_production_writes_enabled = %s,
                        publication_write_mode = %s,
                        candidate_sha256 = %s,
                        watermark_sha256 = %s,
                        rollback_deadline = %s::timestamptz,
                        last_transition_id = %s,
                        updated_at = now()
                    where control_id = 'production'
                      and generation = %s
                      and state = %s
                    returning control_id
                    """,
                    (
                        target["generation"],
                        target["state"],
                        target["active_ingestion_owner"],
                        target["legacy_dispatch_enabled"],
                        target["uplift_scheduler_enabled"],
                        target["uplift_production_writes_enabled"],
                        target["publication_write_mode"],
                        candidate_sha256,
                        watermark_sha256,
                        rollback_deadline_utc,
                        transition,
                        current["generation"],
                        current["state"],
                    ),
                )
                if cursor.fetchone() is None:
                    raise ControlError("cutover state compare-and-swap rejected stale evidence")
        return self.read()


def fetch_public_json(url: str) -> dict[str, Any]:
    try:
        request = Request(url, headers={"accept": "application/json", "user-agent": "nutsnews-cutover-control/1"})
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise ControlError("legacy scheduling status was not HTTP 200")
            payload = json.loads(response.read(131_072))
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError("legacy scheduling status is unavailable") from exc
    if not isinstance(payload, dict):
        raise ControlError("legacy scheduling status is invalid")
    return payload


def validate_legacy_status(payload: dict[str, Any], *, expected_enabled: bool) -> dict[str, Any]:
    effects = payload.get("disabledEffects")
    if (
        payload.get("schemaVersion") != 1
        or payload.get("enabled") is not expected_enabled
        or payload.get("configurationValid") is not True
        or payload.get("legacyProductionOwner") != "ramideltoro/nutsnews-worker"
        or not isinstance(effects, dict)
    ):
        raise ControlError("legacy scheduling status does not match the expected safe state")
    for field in (
        "failoverWakeEnabled",
        "failoverStatusEnabled",
        "failoverActionsEnabled",
        "durableObjectAlarmsEnabled",
        "dnsReadbackEnabled",
        "liveOriginReadinessEnabled",
        "failoverAlertsEnabled",
        "analyticsEventsEnabled",
    ):
        if effects.get(field) is not True:
            raise ControlError("legacy scheduling status reports a missing failover surface")
    return {
        "schemaVersion": payload["schemaVersion"],
        "state": payload.get("state"),
        "enabled": payload["enabled"],
        "configurationValid": payload["configurationValid"],
        "legacyProductionOwner": payload["legacyProductionOwner"],
        "retainedFailoverSurfaceCount": 8,
    }


def run_fixed(command: list[str], name: str, *, timeout: int = 180) -> CommandResult:
    started = time.monotonic()
    result = subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        raise ControlError(f"fixed operation failed: {name}")
    return CommandResult(name=name, returncode=result.returncode, elapsed_ms=elapsed_ms)


def atomic_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.chmod(mode)
    temporary.replace(path)


def production_api_dropin(candidate: str, watermark: str) -> str:
    return (
        "[Service]\n"
        "Environment=NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=true\n"
        "Environment=NUTSNEWS_WORKER_UPLIFT_CUTOVER_STATE=cutover-approved\n"
        "Environment=NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=true\n"
        f"Environment=NUTSNEWS_WORKER_UPLIFT_EXPECTED_CANDIDATE_SHA256={candidate}\n"
        f"Environment=NUTSNEWS_WORKER_UPLIFT_EXPECTED_WATERMARK_SHA256={watermark}\n"
    )


def production_compose_override(candidate: str, watermark: str, confirmation: str) -> str:
    return (
        "# Managed by ramideltoro/nutsnews-backend #126.\n"
        "services:\n"
        "  publication:\n"
        "    environment:\n"
        "      NUTSNEWS_PUBLICATION_WRITE_MODE: production\n"
        "      NUTSNEWS_PUBLICATION_FEATURE_FLAG: worker-uplift-publication-production\n"
        f"      NUTSNEWS_PUBLICATION_PRODUCTION_WRITE_CONFIRMATION: {confirmation}\n"
        f"      NUTSNEWS_WORKER_UPLIFT_CANDIDATE_SHA256: {candidate}\n"
        f"      NUTSNEWS_WORKER_UPLIFT_WATERMARK_SHA256: {watermark}\n"
    )


def restore_shadow_files() -> list[CommandResult]:
    if API_DROPIN.exists():
        API_DROPIN.unlink()
    if PRODUCTION_OVERRIDE.exists():
        PRODUCTION_OVERRIDE.unlink()
    commands = [
        run_fixed(["systemctl", "daemon-reload"], "reload-api-unit"),
        run_fixed(["systemctl", "restart", "nutsnews-worker-db-api.service"], "restart-api-shadow"),
        run_fixed(
            ["docker", "compose", "-f", str(BASE_COMPOSE), "up", "-d", "--no-deps", "publication"],
            "start-publication-shadow",
            timeout=300,
        ),
    ]
    return commands


def prepare_production_files(candidate: str, watermark: str, confirmation: str) -> list[CommandResult]:
    atomic_text(API_DROPIN, production_api_dropin(candidate, watermark), 0o644)
    atomic_text(PRODUCTION_OVERRIDE, production_compose_override(candidate, watermark, confirmation), 0o640)
    return [
        run_fixed(["systemctl", "daemon-reload"], "reload-api-unit"),
        run_fixed(["systemctl", "restart", "nutsnews-worker-db-api.service"], "restart-api-fenced"),
        run_fixed(
            [
                "docker",
                "compose",
                "-f",
                str(BASE_COMPOSE),
                "-f",
                str(PRODUCTION_OVERRIDE),
                "up",
                "-d",
                "--no-deps",
                "publication",
            ],
            "start-publication-production-fenced",
            timeout=300,
        ),
    ]


def stop_uplift_scheduler() -> CommandResult:
    return run_fixed(
        ["docker", "compose", "-f", str(BASE_COMPOSE), "stop", "scheduler"],
        "stop-uplift-scheduler",
        timeout=180,
    )


def start_uplift_scheduler() -> CommandResult:
    return run_fixed(
        ["docker", "compose", "-f", str(BASE_COMPOSE), "up", "-d", "--no-deps", "scheduler"],
        "start-uplift-scheduler",
        timeout=300,
    )


def validate_final_decision(
    decision: dict[str, Any],
    *,
    candidate: str,
    watermark: str,
    deadline: str,
) -> None:
    if (
        decision.get("decision") != "GO"
        or decision.get("authorized_for_execution") is not True
        or decision.get("approver_login") != "ramideltoro"
        or decision.get("tracking_issue") != "ramideltoro/nutsnews-worker#166"
        or decision.get("execution_issue") != "ramideltoro/nutsnews-worker#127"
    ):
        raise ControlError("exact #166 GO is absent")
    if decision.get("candidate_sha256") != candidate or decision.get("watermark_sha256") != watermark:
        raise ControlError("#166 decision does not match the exact candidate and watermark")
    if decision.get("rollback_deadline_utc") != deadline:
        raise ControlError("#166 decision does not match the rollback deadline")
    if decision.get("blockers"):
        raise ControlError("#166 decision still contains blockers")
    if not decision.get("control_commit"):
        raise ControlError("#166 decision does not bind the control commit")
    parse_utc(str(decision.get("approved_at_utc")), "approved_at_utc")
    parse_utc(deadline, "rollback_deadline_utc")


def simulated_initial() -> dict[str, Any]:
    return {
        "control_id": "production",
        "generation": 1,
        "state": "shadow",
        "active_ingestion_owner": "legacy_shards",
        "legacy_dispatch_enabled": True,
        "uplift_scheduler_enabled": True,
        "uplift_production_writes_enabled": False,
        "publication_write_mode": "shadow_comparison",
        "candidate_sha256": None,
        "watermark_sha256": None,
        "rollback_deadline_utc": None,
        "observation_start_utc": None,
        "observation_end_utc": None,
        "last_transition_id": "bootstrap-shadow",
        "updated_at_utc": utc_now(),
    }


def build_dry_run(contract: dict[str, Any], candidate: str, watermark: str, deadline: str) -> dict[str, Any]:
    row = simulated_initial()
    transitions = []
    for transition in ("fence", "activate", "rollback-prepare", "rollback-finalize"):
        previous = safe_state(row)
        row = transition_state(
            row,
            transition,
            candidate_sha256=candidate,
            watermark_sha256=watermark,
            rollback_deadline_utc=deadline,
        )
        transitions.append(
            {
                "transition": transition,
                "from": previous["state"],
                "to": row["state"],
                "single_writer_invariants_pass": not validate_state(row),
            }
        )
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "dry-run",
        "generated_at_utc": utc_now(),
        "mutation_performed": False,
        "candidate_sha256": candidate,
        "watermark_sha256": watermark,
        "rollback_deadline_utc": deadline,
        "transitions": transitions,
        "unchanged_failover_resources": True,
        "forbidden_operations_present": False,
        "contract_sha256": canonical_sha256(contract),
    }


def build_rehearsal(
    contract: dict[str, Any],
    candidate: str,
    watermark: str,
    deadline: str,
    *,
    injected_failure: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    row = simulated_initial()
    timeline: list[dict[str, Any]] = []
    safe_failure_states: list[dict[str, Any]] = []
    for transition in ("fence", "activate", "rollback-prepare", "rollback-finalize"):
        if injected_failure == transition:
            safe_failure_states.append(
                {
                    "injected_at": transition,
                    "state": row["state"],
                    "uplift_production_writes_enabled": row["uplift_production_writes_enabled"],
                    "legacy_dispatch_enabled": row["legacy_dispatch_enabled"],
                    "single_writer_safe": not validate_state(row),
                }
            )
            break
        row = transition_state(
            row,
            transition,
            candidate_sha256=candidate,
            watermark_sha256=watermark,
            rollback_deadline_utc=deadline,
        )
        timeline.append({"transition": transition, "state": safe_state(row)})
    elapsed_seconds = time.monotonic() - started
    complete = injected_failure is None
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "isolated-rollback-rehearsal",
        "generated_at_utc": utc_now(),
        "mutation_performed": False,
        "production_targets_reachable": False,
        "candidate_sha256": candidate,
        "watermark_sha256": watermark,
        "rollback_deadline_utc": deadline,
        "timeline": timeline,
        "injected_failure": injected_failure,
        "safe_failure_states": safe_failure_states,
        "completed_full_cycle": complete,
        "restored_state": row["state"] if complete else None,
        "recovery_elapsed_seconds": round(elapsed_seconds, 6),
        "target_recovery_seconds": 900,
        "within_target": elapsed_seconds < 900,
        "single_writer_invariants_pass": not validate_state(row),
        "dns_failover_unchanged": True,
        "contract_sha256": canonical_sha256(contract),
    }


def command_metadata(items: list[CommandResult]) -> list[dict[str, Any]]:
    return [
        {"name": item.name, "returncode": item.returncode, "elapsed_ms": item.elapsed_ms}
        for item in items
    ]


def execute_apply(
    database: CutoverDatabase,
    contract: dict[str, Any],
    decision: dict[str, Any],
    *,
    candidate: str,
    watermark: str,
    deadline: str,
    confirmation: str,
    scheduling_status_url: str,
) -> dict[str, Any]:
    validate_final_decision(decision, candidate=candidate, watermark=watermark, deadline=deadline)
    if confirmation != f"execute-worker-uplift-cutover:{candidate}":
        raise ControlError("apply typed confirmation mismatch")
    legacy_status = validate_legacy_status(fetch_public_json(scheduling_status_url), expected_enabled=False)
    before = database.read()
    commands = [stop_uplift_scheduler()]
    fenced = database.transition(
        "fence",
        candidate_sha256=candidate,
        watermark_sha256=watermark,
        rollback_deadline_utc=deadline,
    )
    commands.extend(
        prepare_production_files(
            candidate,
            watermark,
            str(contract["write_gate"]["publication_confirmation"]),
        )
    )
    active = database.transition(
        "activate",
        candidate_sha256=candidate,
        watermark_sha256=watermark,
        rollback_deadline_utc=deadline,
    )
    commands.append(start_uplift_scheduler())
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "apply",
        "generated_at_utc": utc_now(),
        "mutation_performed": True,
        "candidate_sha256": candidate,
        "watermark_sha256": watermark,
        "legacy_scheduling": legacy_status,
        "before": safe_state(before),
        "fenced": safe_state(fenced),
        "after": safe_state(active),
        "commands": command_metadata(commands),
        "single_writer_invariants_pass": not validate_state(active),
        "dns_failover_unchanged": True,
        "contract_sha256": canonical_sha256(contract),
    }


def execute_rollback_prepare(
    database: CutoverDatabase,
    contract: dict[str, Any],
    decision: dict[str, Any],
    *,
    candidate: str,
    watermark: str,
    deadline: str,
    confirmation: str,
) -> dict[str, Any]:
    validate_final_decision(decision, candidate=candidate, watermark=watermark, deadline=deadline)
    if confirmation != f"rollback-worker-uplift-cutover:{watermark}":
        raise ControlError("rollback typed confirmation mismatch")
    if datetime.now(timezone.utc) > parse_utc(deadline, "rollback_deadline_utc"):
        raise ControlError("rollback deadline has passed; forward recovery is required")
    before = database.read()
    commands = [stop_uplift_scheduler()]
    pending = database.transition(
        "rollback-prepare",
        candidate_sha256=candidate,
        watermark_sha256=watermark,
        rollback_deadline_utc=deadline,
    )
    commands.extend(restore_shadow_files())
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "rollback-prepare",
        "generated_at_utc": utc_now(),
        "mutation_performed": True,
        "before": safe_state(before),
        "after": safe_state(pending),
        "commands": command_metadata(commands),
        "requires_legacy_scheduler_enable": True,
        "single_writer_invariants_pass": not validate_state(pending),
        "contract_sha256": canonical_sha256(contract),
    }


def execute_rollback_finalize(
    database: CutoverDatabase,
    contract: dict[str, Any],
    *,
    candidate: str,
    watermark: str,
    deadline: str,
    confirmation: str,
    scheduling_status_url: str,
) -> dict[str, Any]:
    if confirmation != f"rollback-worker-uplift-cutover:{watermark}":
        raise ControlError("rollback typed confirmation mismatch")
    legacy_status = validate_legacy_status(fetch_public_json(scheduling_status_url), expected_enabled=True)
    before = database.read()
    shadow = database.transition(
        "rollback-finalize",
        candidate_sha256=candidate,
        watermark_sha256=watermark,
        rollback_deadline_utc=deadline,
    )
    command = start_uplift_scheduler()
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "rollback-finalize",
        "generated_at_utc": utc_now(),
        "mutation_performed": True,
        "legacy_scheduling": legacy_status,
        "before": safe_state(before),
        "after": safe_state(shadow),
        "commands": command_metadata([command]),
        "single_writer_invariants_pass": not validate_state(shadow),
        "dns_failover_unchanged": True,
        "contract_sha256": canonical_sha256(contract),
    }


def build_live_status(
    database: CutoverDatabase,
    contract: dict[str, Any],
    *,
    expected_legacy_enabled: bool,
    scheduling_status_url: str,
    mode: str,
) -> dict[str, Any]:
    row = database.read()
    legacy = validate_legacy_status(fetch_public_json(scheduling_status_url), expected_enabled=expected_legacy_enabled)
    state_errors = validate_state(row)
    return {
        "schema_version": 1,
        "status": "pass" if not state_errors else "fail",
        "mode": mode,
        "generated_at_utc": utc_now(),
        "mutation_performed": False,
        "database_control": safe_state(row),
        "legacy_scheduling": legacy,
        "single_writer_invariants_pass": not state_errors,
        "errors": state_errors,
        "dns_failover_unchanged": True,
        "contract_sha256": canonical_sha256(contract),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "preflight",
            "dry-run",
            "rehearse",
            "verify",
            "apply",
            "rollback-prepare",
            "rollback-finalize",
        ),
    )
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--db-env-file", type=Path, default=DB_ENV_FILE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--watermark-sha256")
    parser.add_argument("--rollback-deadline-utc")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--expected-legacy-enabled", choices=("true", "false"), default="true")
    parser.add_argument("--scheduling-status-url")
    parser.add_argument("--inject-failure", choices=("fence", "activate", "rollback-prepare", "rollback-finalize"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    contract_path = args.contract or (INSTALLED_CONTRACT if INSTALLED_CONTRACT.exists() else DEFAULT_CONTRACT)
    decision_path = args.decision or (INSTALLED_DECISION if INSTALLED_DECISION.exists() else DEFAULT_DECISION)
    try:
        contract = load_json(contract_path)
        candidate = require_sha256(args.candidate_sha256, "candidate_sha256")
        watermark = require_sha256(args.watermark_sha256, "watermark_sha256")
        deadline = str(args.rollback_deadline_utc or "")
        parse_utc(deadline, "rollback_deadline_utc")
        scheduling_status_url = args.scheduling_status_url or str(
            contract["legacy_scheduling_control"]["status_endpoint"]
        )
        if args.mode == "dry-run":
            report = build_dry_run(contract, candidate, watermark, deadline)
        elif args.mode == "rehearse":
            report = build_rehearsal(
                contract,
                candidate,
                watermark,
                deadline,
                injected_failure=args.inject_failure,
            )
        else:
            database = CutoverDatabase(args.db_env_file)
            if args.mode in {"preflight", "verify"}:
                report = build_live_status(
                    database,
                    contract,
                    expected_legacy_enabled=args.expected_legacy_enabled == "true",
                    scheduling_status_url=scheduling_status_url,
                    mode=args.mode,
                )
            elif args.mode == "apply":
                report = execute_apply(
                    database,
                    contract,
                    load_json(decision_path),
                    candidate=candidate,
                    watermark=watermark,
                    deadline=deadline,
                    confirmation=args.confirmation,
                    scheduling_status_url=scheduling_status_url,
                )
            elif args.mode == "rollback-prepare":
                report = execute_rollback_prepare(
                    database,
                    contract,
                    load_json(decision_path),
                    candidate=candidate,
                    watermark=watermark,
                    deadline=deadline,
                    confirmation=args.confirmation,
                )
            else:
                report = execute_rollback_finalize(
                    database,
                    contract,
                    candidate=candidate,
                    watermark=watermark,
                    deadline=deadline,
                    confirmation=args.confirmation,
                    scheduling_status_url=scheduling_status_url,
                )
    except ControlError as error:
        report = {
            "schema_version": 1,
            "status": "fail",
            "mode": getattr(args, "mode", "unknown"),
            "generated_at_utc": utc_now(),
            "mutation_performed": False,
            "errors": [str(error)],
        }
    write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
