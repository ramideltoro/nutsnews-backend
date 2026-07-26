#!/usr/bin/env python3
"""Build a read-only worker-uplift shadow soak and capacity report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "worker-uplift-shadow-soak-capacity-report.json"
DEFAULT_SMOKE_REPORT = Path("/var/lib/nutsnews/worker-uplift-runtime/reports/last-smoke.json")
DEFAULT_RUNTIME_STATUS_REPORT = Path("/var/lib/nutsnews/worker-uplift-runtime/reports/last-status.json")
STAGES = ("scheduler", "fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication")
WORKER_STAGES = STAGES[1:]
STAGE_SCHEMAS = {
    "scheduler": "worker_uplift_scheduler",
    "fetcher": "worker_uplift_fetcher",
    "canonicalizer": "worker_uplift_canonicalizer",
    "enrichment": "worker_uplift_enrichment",
    "approval": "worker_uplift_approval",
    "translation": "worker_uplift_translation",
    "persistence": "worker_uplift_persistence",
    "publication": "worker_uplift_publication",
}
QUEUE_METRIC_KEYS = ("messages_ready", "messages_unacknowledged", "messages", "consumers")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_manifest_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def run_psql(db_url: str, query: str) -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return None, "psql_not_installed"
    except subprocess.TimeoutExpired:
        return None, "query_timeout"
    except subprocess.CalledProcessError:
        return None, "query_failed"
    return completed.stdout.strip(), None


def parse_key_values(output: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (output or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def parse_int(values: dict[str, str], key: str) -> int:
    try:
        return int(values.get(key, "0") or "0")
    except ValueError:
        return 0


def parse_float(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def sql_events_union() -> str:
    selects: list[str] = []
    for schema in STAGE_SCHEMAS.values():
        selects.extend(
            [
                f"select received_at as ts from {schema}.inbox",
                f"select created_at as ts from {schema}.outbox",
                f"select started_at as ts from {schema}.attempts",
            ]
        )
    selects.extend(
        [
            "select created_at as ts from worker_uplift_scheduler.feed_schedules",
            "select acquired_at as ts from worker_uplift_scheduler.feed_leases",
            "select fetched_at as ts from worker_uplift_fetcher.fetch_versions",
            "select created_at as ts from worker_uplift_canonicalizer.article_identities",
            "select created_at as ts from worker_uplift_enrichment.enrichment_records",
            "select reviewed_at as ts from worker_uplift_approval.approval_decisions",
            "select translated_at as ts from worker_uplift_translation.translation_records",
            "select created_at as ts from worker_uplift_persistence.write_requests",
            "select created_at as ts from worker_uplift_final.article_shadow_aggregates",
            "select created_at as ts from worker_uplift_final.api_command_receipts",
            "select updated_at as ts from worker_uplift_final.stage_health_projections",
            "select checked_at as ts from worker_uplift_publication.publication_readiness",
            "select decided_at as ts from worker_uplift_publication.publication_decisions",
        ]
    )
    return "\nunion all\n".join(selects)


def observation_window_query() -> str:
    events = sql_events_union()
    return f"""
with events as (
  {events}
)
select 'observed_event_count=' || count(*)::text from events
union all
select 'observed_window_start_utc=' || coalesce(to_char(min(ts) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '') from events
union all
select 'observed_window_end_utc=' || coalesce(to_char(max(ts) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '') from events
union all
select 'observed_window_hours=' || coalesce(round((extract(epoch from (max(ts) - min(ts))) / 3600.0)::numeric, 2)::text, '0') from events;
"""


def stage_pressure_query() -> str:
    selects: list[str] = []
    for stage, schema in STAGE_SCHEMAS.items():
        selects.extend(
            [
                f"select '{stage}_inbox_received=' || count(*)::text from {schema}.inbox where status = 'received';",
                f"select '{stage}_inbox_processing=' || count(*)::text from {schema}.inbox where status = 'processing';",
                f"select '{stage}_inbox_failed_or_parked=' || count(*)::text from {schema}.inbox where status in ('failed', 'parked');",
                f"select '{stage}_outbox_pending=' || count(*)::text from {schema}.outbox where status = 'pending';",
                f"select '{stage}_outbox_retrying=' || count(*)::text from {schema}.outbox where status = 'retrying';",
                f"select '{stage}_outbox_dead_lettered=' || count(*)::text from {schema}.outbox where status = 'dead_lettered';",
                f"select '{stage}_oldest_unconfirmed_outbox_age_seconds=' || coalesce(extract(epoch from (now() - min(created_at)))::bigint, 0)::text from {schema}.outbox where status in ('pending', 'published', 'retrying') and confirmed_at is null;",
                f"select '{stage}_attempts_failed=' || count(*)::text from {schema}.attempts where status = 'failed';",
                f"select '{stage}_attempts_retry_scheduled=' || count(*)::text from {schema}.attempts where status = 'retry_scheduled';",
                f"select '{stage}_attempts_dead_lettered=' || count(*)::text from {schema}.attempts where status = 'dead_lettered';",
            ]
        )
    return "\n".join(selects)


def final_shadow_query() -> str:
    return """
select 'final_shadow_aggregates=' || count(*)::text from worker_uplift_final.article_shadow_aggregates;
select 'ready_final_shadow_aggregates=' || count(*)::text from worker_uplift_final.article_shadow_aggregates where publication_status in ('ready', 'shadow_only');
select 'published_final_shadow_aggregates=' || count(*)::text from worker_uplift_final.article_shadow_aggregates where publication_status = 'published';
select 'api_shadow_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where provider_mode = 'backend_postgres_shadow';
select 'api_primary_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where provider_mode = 'backend_postgres_primary';
select 'failed_api_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where status = 'rejected';
select 'non_shadow_api_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where shadow_only is false;
select 'stage_health_rows=' || count(*)::text from worker_uplift_final.stage_health_projections;
select 'active_ingestion_owner_legacy_shards=' || count(*)::text from worker_uplift_final.stage_health_projections where active_ingestion_owner = 'legacy_shards';
select 'active_ingestion_owner_worker_uplift=' || count(*)::text from worker_uplift_final.stage_health_projections where active_ingestion_owner = 'worker_uplift';
select 'stage_health_retry_count=' || coalesce(sum(retry_count), 0)::text from worker_uplift_final.stage_health_projections;
select 'stage_health_dlq_count=' || coalesce(sum(dlq_count), 0)::text from worker_uplift_final.stage_health_projections;
select 'stage_health_max_queue_age_seconds=' || coalesce(max(queue_age_seconds), 0)::text from worker_uplift_final.stage_health_projections;
select 'failed_persistence_write_requests=' || count(*)::text from worker_uplift_persistence.write_requests where status = 'failed';
select 'retrying_persistence_write_requests=' || count(*)::text from worker_uplift_persistence.write_requests where status = 'retrying';
"""


def ai_cost_query() -> str:
    return """
select 'approval_local_ai_records=' || count(*)::text from worker_uplift_approval.approval_decisions where ai_provider = 'local_ai';
select 'approval_openai_records=' || count(*)::text from worker_uplift_approval.approval_decisions where lower(coalesce(ai_provider, '') || ' ' || coalesce(ai_model, '')) like '%openai%';
select 'approval_other_provider_records=' || count(*)::text from worker_uplift_approval.approval_decisions where ai_provider is not null and ai_provider not in ('local_ai');
select 'approval_null_provider_records=' || count(*)::text from worker_uplift_approval.approval_decisions where ai_provider is null;
select 'translation_local_ai_records=' || count(*)::text from worker_uplift_translation.translation_records where ai_provider = 'local_ai';
select 'translation_openai_records=' || count(*)::text from worker_uplift_translation.translation_records where lower(coalesce(ai_provider, '') || ' ' || coalesce(ai_model, '')) like '%openai%';
select 'translation_other_provider_records=' || count(*)::text from worker_uplift_translation.translation_records where ai_provider is not null and ai_provider not in ('local_ai');
select 'translation_null_provider_records=' || count(*)::text from worker_uplift_translation.translation_records where ai_provider is null;
select 'approval_qwen_model_records=' || count(*)::text from worker_uplift_approval.approval_decisions where lower(coalesce(ai_model, '')) like '%qwen%';
select 'translation_qwen_model_records=' || count(*)::text from worker_uplift_translation.translation_records where lower(coalesce(ai_model, '')) like '%qwen%';
"""


QUERY_CATALOG = {
    "observation_window": observation_window_query,
    "stage_error_and_retry_pressure": stage_pressure_query,
    "runtime_guardrails": final_shadow_query,
    "ai_cost_and_qwen_saturation": ai_cost_query,
}


def run_report_queries(db_url: str) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    sections: dict[str, dict[str, str]] = {}
    query_errors: list[dict[str, Any]] = []
    for check_id, query_builder in QUERY_CATALOG.items():
        query = query_builder()
        output, error = run_psql(db_url, query)
        if error:
            query_errors.append({"id": check_id, "status": "fail", "error": error, "query_sha256": sha256_text(query)})
            sections[check_id] = {}
        else:
            sections[check_id] = parse_key_values(output)
    return sections, query_errors


def int_value(value: Any) -> int:
    try:
        if value in {"", None}:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def smoke_summary(smoke_report: dict[str, Any] | None) -> dict[str, Any]:
    if not smoke_report:
        return {"status": "missing", "reason": "smoke report not found"}
    smoke = smoke_report.get("smoke", {})
    if not isinstance(smoke, dict):
        return {"status": "invalid", "reason": "smoke section missing"}
    queues_after = smoke.get("queues_after", {})
    queue_metrics: dict[str, list[dict[str, Any]]] = {}
    if isinstance(queues_after, dict):
        for stage, queue_list in queues_after.items():
            if not isinstance(queue_list, list):
                continue
            queue_metrics[str(stage)] = []
            for queue in queue_list:
                if not isinstance(queue, dict):
                    continue
                metrics = queue.get("metrics", {})
                if not isinstance(metrics, dict):
                    metrics = {}
                item = {
                    "queue": queue.get("queue"),
                    "status": queue.get("status"),
                }
                for key in QUEUE_METRIC_KEYS:
                    item[key] = int_value(metrics.get(key))
                queue_metrics[str(stage)].append(item)
    guardrails = smoke.get("guardrails", {})
    production_writes_enabled: dict[str, bool] = {}
    if isinstance(guardrails, dict):
        for stage, value in guardrails.items():
            if isinstance(value, dict):
                production_writes_enabled[str(stage)] = value.get("production_writes_enabled") is True
    return {
        "status": smoke_report.get("status"),
        "generated_at_utc": smoke_report.get("generated_at_utc"),
        "contract": smoke.get("contract"),
        "trigger": smoke.get("trigger"),
        "missing_consumers": smoke.get("missing_consumers", []),
        "dlq_growth": smoke.get("dlq_growth", {}),
        "legacy_ingestion_endpoints_invoked": smoke.get("legacy_ingestion_endpoints_invoked"),
        "queue_metrics_after": queue_metrics,
        "guardrail_production_writes_enabled": production_writes_enabled,
        "versions": smoke.get("versions", {}),
        "db_checks": smoke.get("db_checks", {}) if isinstance(smoke.get("db_checks"), dict) else {},
        "idempotency": smoke.get("idempotency", {}) if isinstance(smoke.get("idempotency"), dict) else {},
    }


def docker_ps_items(stdout: str) -> list[dict[str, Any]]:
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    if isinstance(decoded, dict):
        return [decoded]
    items: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        try:
            decoded_line = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded_line, dict):
            items.append(decoded_line)
    return items


def runtime_status_summary(runtime_report: dict[str, Any] | None) -> dict[str, Any]:
    if not runtime_report:
        return {"status": "missing", "reason": "runtime status report not found", "services": {}}
    services: dict[str, Any] = {}
    command_failures: list[dict[str, Any]] = []
    for command in runtime_report.get("commands", []):
        if not isinstance(command, dict):
            continue
        if int_value(command.get("returncode")) != 0:
            command_failures.append({
                "returncode": int_value(command.get("returncode")),
                "argv": command.get("argv", []),
            })
        for item in docker_ps_items(str(command.get("stdout", ""))):
            service = str(item.get("Service") or item.get("Name") or "")
            if not service:
                continue
            services[service] = {
                "name": item.get("Name"),
                "service": service,
                "state": item.get("State") or item.get("Status"),
                "health": item.get("Health"),
                "exit_code": item.get("ExitCode"),
                "image": item.get("Image"),
            }
    return {
        "status": runtime_report.get("status"),
        "generated_at_utc": runtime_report.get("generated_at_utc"),
        "mode": runtime_report.get("mode"),
        "production_writes_enabled": runtime_report.get("production_writes_enabled"),
        "command_failures": command_failures,
        "services": services,
    }


def run_local_command(argv: list[str], timeout: int = 10) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, ""
    return completed.returncode, completed.stdout.strip()


def systemd_status(unit: str) -> str:
    returncode, stdout = run_local_command(["systemctl", "is-active", unit], timeout=5)
    if returncode is None:
        return "unknown"
    return stdout.splitlines()[0] if stdout else "unknown"


def collect_host_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "collected_at_utc": utc_now(),
        "host_metrics_available": True,
        "systemd_available": True,
    }
    try:
        loadavg = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        load_1m = float(loadavg[0])
    except (OSError, ValueError, IndexError):
        load_1m = None
        snapshot["host_metrics_available"] = False
    cpus = os.cpu_count() or 0
    if load_1m is not None and cpus:
        snapshot["load_1m"] = round(load_1m, 3)
        snapshot["cpu_count"] = cpus
        snapshot["load_per_vcpu"] = round(load_1m / cpus, 3)
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        values: dict[str, int] = {}
        for line in meminfo:
            key, _, rest = line.partition(":")
            amount = rest.strip().split()[0]
            values[key] = int(amount)
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total > 0:
            snapshot["memory_total_kib"] = total
            snapshot["memory_available_kib"] = available
            snapshot["memory_used_percent"] = round(((total - available) / total) * 100, 2)
        else:
            snapshot["host_metrics_available"] = False
    except (OSError, ValueError, IndexError):
        snapshot["host_metrics_available"] = False
    try:
        disk = shutil.disk_usage("/")
        snapshot["root_disk_total_bytes"] = disk.total
        snapshot["root_disk_free_bytes"] = disk.free
        snapshot["root_disk_used_percent"] = round(((disk.total - disk.free) / disk.total) * 100, 2)
    except OSError:
        snapshot["host_metrics_available"] = False

    returncode, stdout = run_local_command(["systemctl", "--failed", "--no-legend", "--no-pager"], timeout=8)
    if returncode is None:
        snapshot["systemd_available"] = False
        snapshot["failed_systemd_units"] = None
    else:
        failed_units = [line.split()[0] for line in stdout.splitlines() if line.strip()]
        snapshot["failed_systemd_units"] = failed_units
    snapshot["service_states"] = {
        "docker": systemd_status("docker"),
        "rabbitmq-server": systemd_status("rabbitmq-server"),
        "alloy": systemd_status("alloy"),
        "caddy": systemd_status("caddy"),
    }
    return snapshot


def observation_hours(values: dict[str, str]) -> float:
    direct = parse_float(values.get("observed_window_hours"))
    if direct is not None:
        return direct
    start = parse_utc(values.get("observed_window_start_utc"))
    end = parse_utc(values.get("observed_window_end_utc"))
    if not start or not end:
        return 0.0
    return max((end - start).total_seconds() / 3600.0, 0.0)


def smoke_queue_rows(smoke: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    queue_metrics = smoke.get("queue_metrics_after", {})
    if not isinstance(queue_metrics, dict):
        return rows
    for stage, queue_list in queue_metrics.items():
        if not isinstance(queue_list, list):
            continue
        for item in queue_list:
            if isinstance(item, dict):
                rows.append({"stage": stage, **item})
    return rows


def evaluate_observation_window(values: dict[str, str], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    min_hours = float(manifest.get("observation_window_policy", {}).get("minimum_hours", 48) or 48)
    hours = observation_hours(values)
    details = {
        "minimum_hours": min_hours,
        "observed_hours": round(hours, 2),
        "observed_window_start_utc": values.get("observed_window_start_utc", ""),
        "observed_window_end_utc": values.get("observed_window_end_utc", ""),
        "observed_event_count": parse_int(values, "observed_event_count"),
    }
    reasons: list[str] = []
    if parse_int(values, "observed_event_count") <= 0:
        reasons.append("no_shadow_events_observed")
        return "fail", reasons, details
    if hours < min_hours:
        reasons.append("observation_window_below_required_hours")
        return "insufficient_window", reasons, details
    return "pass", reasons, details


def evaluate_runtime_guardrails(values: dict[str, str], smoke: dict[str, Any], runtime: dict[str, Any], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    policy = manifest.get("observation_window_policy", {})
    budgets = manifest.get("approved_budgets", {})
    if policy.get("writes_allowed") is not False:
        reasons.append("manifest_writes_allowed_not_false")
    if policy.get("production_cutover_authorized") is not False:
        reasons.append("manifest_cutover_authorized_not_false")
    if smoke.get("status") != "pass":
        reasons.append("latest_smoke_not_pass")
    if smoke.get("missing_consumers"):
        reasons.append("latest_smoke_missing_consumers")
    if smoke.get("legacy_ingestion_endpoints_invoked") is not False:
        reasons.append("legacy_ingestion_invocation_not_false")
    if any(smoke.get("guardrail_production_writes_enabled", {}).values()):
        reasons.append("smoke_guardrail_production_writes_enabled")
    if runtime.get("status") != "pass":
        reasons.append("runtime_status_not_pass")
    if runtime.get("production_writes_enabled") is not False:
        reasons.append("runtime_production_writes_enabled_not_false")
    if runtime.get("command_failures"):
        reasons.append("runtime_status_command_failed")
    services = runtime.get("services", {})
    if not isinstance(services, dict) or not services:
        reasons.append("runtime_services_missing")
        services = {}
    for stage in STAGES:
        service = services.get(stage)
        if not isinstance(service, dict):
            reasons.append(f"{stage}_runtime_service_missing")
            continue
        state = str(service.get("state") or "").lower()
        health = str(service.get("health") or "").lower()
        if "running" not in state:
            reasons.append(f"{stage}_runtime_not_running")
        if health and health not in {"healthy", "running"}:
            reasons.append(f"{stage}_runtime_health_not_healthy")
    if parse_int(values, "active_ingestion_owner_worker_uplift") > int(budgets.get("max_worker_uplift_active_owner_rows", 0) or 0):
        reasons.append("worker_uplift_marked_active_owner_before_cutover")
    if parse_int(values, "api_primary_receipts") > 0:
        reasons.append("primary_api_receipts_before_cutover")
    if parse_int(values, "non_shadow_api_receipts") > 0:
        reasons.append("non_shadow_api_receipts_before_cutover")
    if parse_int(values, "published_final_shadow_aggregates") > 0:
        reasons.append("published_final_shadow_aggregates_before_cutover")
    if parse_int(values, "failed_api_receipts") > int(budgets.get("max_failed_shadow_api_requests", 0) or 0):
        reasons.append("failed_shadow_api_receipts_nonzero")
    details = {
        "values": values,
        "smoke_status": smoke.get("status"),
        "smoke_generated_at_utc": smoke.get("generated_at_utc"),
        "runtime_status": runtime.get("status"),
        "runtime_generated_at_utc": runtime.get("generated_at_utc"),
        "runtime_services": services,
    }
    return ("pass" if not reasons else "fail"), reasons, details


def evaluate_queue_headroom(smoke: dict[str, Any], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    budgets = manifest.get("approved_budgets", {})
    max_ready = int(budgets.get("max_queue_messages_ready", 100) or 100)
    max_unacked = int(budgets.get("max_queue_messages_unacknowledged", 10) or 10)
    max_missing = int(budgets.get("max_missing_consumers", 0) or 0)
    max_dlq_growth = int(budgets.get("max_dlq_growth_per_smoke", 0) or 0)
    rows = smoke_queue_rows(smoke)
    if not rows:
        reasons.append("latest_smoke_queue_metrics_missing")
    for row in rows:
        ready = int_value(row.get("messages_ready") or row.get("messages"))
        unacked = int_value(row.get("messages_unacknowledged"))
        consumers = int_value(row.get("consumers"))
        if ready > max_ready:
            reasons.append(f"{row.get('stage')}_queue_ready_above_budget")
        if unacked > max_unacked:
            reasons.append(f"{row.get('stage')}_queue_unacked_above_budget")
        if consumers < 1:
            reasons.append(f"{row.get('stage')}_queue_consumer_missing")
    missing = smoke.get("missing_consumers", [])
    if isinstance(missing, list) and len(missing) > max_missing:
        reasons.append("missing_consumers_above_budget")
    dlq_growth = smoke.get("dlq_growth", {})
    if isinstance(dlq_growth, dict):
        for queue, value in dlq_growth.items():
            if int_value(value) > max_dlq_growth:
                reasons.append(f"{queue}_dlq_growth_above_budget")
    elif dlq_growth:
        reasons.append("dlq_growth_invalid")
    return ("pass" if not reasons else "fail"), reasons, {"queues_after": rows, "dlq_growth": dlq_growth}


def evaluate_stage_pressure(values: dict[str, str], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    budgets = manifest.get("approved_budgets", {})
    max_age = int(budgets.get("max_stage_pending_outbox_age_seconds", 900) or 900)
    pressure: dict[str, dict[str, int]] = {}
    for stage in STAGES:
        stage_values = {
            "inbox_received": parse_int(values, f"{stage}_inbox_received"),
            "inbox_processing": parse_int(values, f"{stage}_inbox_processing"),
            "inbox_failed_or_parked": parse_int(values, f"{stage}_inbox_failed_or_parked"),
            "outbox_pending": parse_int(values, f"{stage}_outbox_pending"),
            "outbox_retrying": parse_int(values, f"{stage}_outbox_retrying"),
            "outbox_dead_lettered": parse_int(values, f"{stage}_outbox_dead_lettered"),
            "oldest_unconfirmed_outbox_age_seconds": parse_int(values, f"{stage}_oldest_unconfirmed_outbox_age_seconds"),
            "attempts_failed": parse_int(values, f"{stage}_attempts_failed"),
            "attempts_retry_scheduled": parse_int(values, f"{stage}_attempts_retry_scheduled"),
            "attempts_dead_lettered": parse_int(values, f"{stage}_attempts_dead_lettered"),
        }
        pressure[stage] = stage_values
        if stage_values["oldest_unconfirmed_outbox_age_seconds"] > max_age:
            reasons.append(f"{stage}_oldest_unconfirmed_outbox_age_above_budget")
        if stage_values["outbox_retrying"] > 0:
            reasons.append(f"{stage}_outbox_retrying_nonzero")
        if stage_values["outbox_dead_lettered"] > 0:
            reasons.append(f"{stage}_outbox_dead_lettered_nonzero")
    return ("pass" if not reasons else "fail"), reasons, {"stage_pressure": pressure}


def evaluate_ai_cost(values: dict[str, str], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    budgets = manifest.get("approved_budgets", {})
    max_openai = int(budgets.get("max_openai_shadow_records", 0) or 0)
    openai_records = parse_int(values, "approval_openai_records") + parse_int(values, "translation_openai_records")
    if openai_records > max_openai:
        reasons.append("openai_shadow_records_above_budget")
    tuned = manifest.get("tuned_runtime_values", {})
    approval = tuned.get("approval", {}) if isinstance(tuned, dict) else {}
    if approval.get("openai_fallback_enabled") is not False:
        reasons.append("approval_openai_fallback_not_disabled_in_manifest")
    try:
        openai_budget = float(approval.get("openai_fallback_budget_usd", 1))
    except (TypeError, ValueError):
        openai_budget = 1.0
    if openai_budget != 0:
        reasons.append("approval_openai_fallback_budget_not_zero")
    details = {
        "values": values,
        "openai_shadow_records": openai_records,
        "qwen_model_records": parse_int(values, "approval_qwen_model_records") + parse_int(values, "translation_qwen_model_records"),
    }
    return ("pass" if not reasons else "fail"), reasons, details


def evaluate_host(snapshot: dict[str, Any], manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    budgets = manifest.get("approved_budgets", {})
    if snapshot.get("host_metrics_available") is not True:
        reasons.append("host_metrics_unavailable")
    if snapshot.get("systemd_available") is not True:
        reasons.append("systemd_status_unavailable")
    root_disk_used = parse_float(snapshot.get("root_disk_used_percent"))
    memory_used = parse_float(snapshot.get("memory_used_percent"))
    load_per_vcpu = parse_float(snapshot.get("load_per_vcpu"))
    if root_disk_used is None:
        reasons.append("root_disk_usage_missing")
    elif root_disk_used > float(budgets.get("max_root_disk_used_percent", 80) or 80):
        reasons.append("root_disk_usage_above_budget")
    if memory_used is None:
        reasons.append("memory_usage_missing")
    elif memory_used > float(budgets.get("max_memory_used_percent", 80) or 80):
        reasons.append("memory_usage_above_budget")
    if load_per_vcpu is None:
        reasons.append("load_per_vcpu_missing")
    elif load_per_vcpu > float(budgets.get("max_load_per_vcpu", 1.5) or 1.5):
        reasons.append("load_per_vcpu_above_budget")
    failed_units = snapshot.get("failed_systemd_units")
    if not isinstance(failed_units, list):
        reasons.append("failed_systemd_units_unavailable")
    elif len(failed_units) > int(budgets.get("max_failed_systemd_units", 0) or 0):
        reasons.append("failed_systemd_units_nonzero")
    service_states = snapshot.get("service_states", {})
    if not isinstance(service_states, dict):
        reasons.append("service_states_unavailable")
    else:
        for service in ("docker", "alloy"):
            if service_states.get(service) != "active":
                reasons.append(f"{service}_systemd_not_active")
        if service_states.get("rabbitmq-server") not in {"active", "unknown", "inactive"}:
            reasons.append("rabbitmq_systemd_failed")
    return ("pass" if not reasons else "fail"), reasons, snapshot


def evaluate_source_controlled_tuning(manifest: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    tuned = manifest.get("tuned_runtime_values", {})
    if not isinstance(tuned, dict):
        tuned = {}
        reasons.append("tuned_runtime_values_missing")
    for stage in STAGES:
        if stage not in tuned:
            reasons.append(f"{stage}_tuned_values_missing")
    if not manifest.get("exit_triggers"):
        reasons.append("exit_triggers_missing")
    if not manifest.get("guardrails"):
        reasons.append("guardrails_missing")
    return ("pass" if not reasons else "fail"), reasons, {"tuned_runtime_values": tuned, "exit_triggers": manifest.get("exit_triggers", [])}


def build_checks(
    manifest: dict[str, Any],
    sections: dict[str, dict[str, str]],
    smoke: dict[str, Any],
    runtime: dict[str, Any],
    host: dict[str, Any],
    query_errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    query_error_by_id = {item["id"]: item for item in query_errors}
    checks: list[dict[str, Any]] = []
    for section in manifest.get("required_sections", []):
        check_id = str(section.get("id", ""))
        if check_id in query_error_by_id:
            checks.append({**section, **query_error_by_id[check_id]})
            continue
        if check_id == "observation_window":
            status, reasons, details = evaluate_observation_window(sections.get(check_id, {}), manifest)
        elif check_id == "runtime_guardrails":
            status, reasons, details = evaluate_runtime_guardrails(sections.get(check_id, {}), smoke, runtime, manifest)
        elif check_id == "queue_and_dlq_headroom":
            status, reasons, details = evaluate_queue_headroom(smoke, manifest)
        elif check_id == "stage_error_and_retry_pressure":
            status, reasons, details = evaluate_stage_pressure(sections.get(check_id, {}), manifest)
        elif check_id == "ai_cost_and_qwen_saturation":
            status, reasons, details = evaluate_ai_cost(sections.get(check_id, {}), manifest)
        elif check_id == "host_and_telemetry_headroom":
            status, reasons, details = evaluate_host(host, manifest)
        elif check_id == "source_controlled_tuning":
            status, reasons, details = evaluate_source_controlled_tuning(manifest)
        else:
            status = "fail"
            reasons = ["unknown_required_section"]
            details = {}
        checks.append({**section, "status": status, "reasons": reasons, "details": details})
    return checks


def write_report(report: dict[str, Any], output: str) -> None:
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--db-url-env", default="NUTSNEWS_WORKER_UPLIFT_SOAK_DB_URL")
    parser.add_argument("--smoke-report", default=str(DEFAULT_SMOKE_REPORT))
    parser.add_argument("--runtime-status-report", default=str(DEFAULT_RUNTIME_STATUS_REPORT))
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--require-window", action="store_true")
    parser.add_argument("--min-window-hours", type=float)
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    if args.min_window_hours is not None:
        manifest.setdefault("observation_window_policy", {})["minimum_hours"] = args.min_window_hours
    db_url = os.environ.get(args.db_url_env, "").strip()
    smoke = smoke_summary(load_optional_json(Path(args.smoke_report)))
    runtime = runtime_status_summary(load_optional_json(Path(args.runtime_status_report)))
    checked_at = utc_now()

    report: dict[str, Any] = {
        "status": "pass",
        "checked_at_utc": checked_at,
        "report_id": manifest.get("report_id"),
        "manifest": safe_manifest_path(manifest_path),
        "manifest_version": manifest.get("version"),
        "tracking_issue": manifest.get("tracking_issue"),
        "safe_metadata_only": True,
        "writes_performed": False,
        "production_cutover_authorized": False,
        "db_url_env": args.db_url_env,
        "db_url_present": bool(db_url),
        "smoke_report_present": Path(args.smoke_report).exists(),
        "runtime_status_report_present": Path(args.runtime_status_report).exists(),
        "require_window": args.require_window,
        "observation_window_policy": manifest.get("observation_window_policy", {}),
        "approved_budgets": manifest.get("approved_budgets", {}),
        "smoke": smoke,
        "runtime_status": runtime,
        "host_snapshot": {},
        "checks": [],
        "errors": [],
    }

    if args.offline:
        report["status"] = "skipped"
        report["reason"] = "offline mode"
        report["checks"] = [
            {**section, "status": "skipped_with_reason", "reason": "offline mode"}
            for section in manifest.get("required_sections", [])
        ]
        write_report(report, args.output)
        return 0

    if not db_url:
        report["status"] = "blocked"
        report["reason"] = f"missing database URL env {args.db_url_env}"
        report["errors"].append("missing_worker_uplift_soak_db_url")
        write_report(report, args.output)
        return 1 if args.enforce else 0

    host = collect_host_snapshot()
    sections, query_errors = run_report_queries(db_url)
    checks = build_checks(manifest, sections, smoke, runtime, host, query_errors)
    failed = [check["id"] for check in checks if check.get("status") == "fail"]
    insufficient = [check["id"] for check in checks if check.get("status") == "insufficient_window"]
    report["checks"] = checks
    report["host_snapshot"] = host
    report["failed_checks"] = failed
    report["insufficient_window_checks"] = insufficient
    if failed:
        report["status"] = "fail"
        report["errors"].append("worker_uplift_soak_checks_failed")
    elif insufficient:
        report["status"] = "insufficient_window"
        report["errors"].append("worker_uplift_soak_window_incomplete")
    else:
        report["status"] = "pass"
    write_report(report, args.output)
    if args.enforce and report["status"] == "fail":
        return 1
    if args.enforce and args.require_window and report["status"] == "insufficient_window":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
