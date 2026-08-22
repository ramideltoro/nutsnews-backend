#!/usr/bin/env python3
"""Write low-cardinality NutsNews backend metrics for Alloy textfile scraping."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_OUTPUT = Path("/var/lib/nutsnews/metrics/nutsnews.prom")
BACKUP_STATE_DIR = Path("/var/lib/nutsnews/backups")
RABBITMQ_RECOVERY_STATE_DIR = Path("/var/lib/nutsnews/rabbitmq-recovery")
POSTGRES_STATE_DIR = Path("/var/lib/nutsnews/postgres")
POSTGRES_REPLICATION_HEALTH_PATH = POSTGRES_STATE_DIR / "replication-health.json"
SUPABASE_SYNC_RELAY_STATE_PATH = Path("/var/lib/nutsnews/supabase-sync-relay/last-run.json")
HEALTH_AUDIT_STATE_PATH = Path(
    os.environ.get(
        "NUTSNEWS_HEALTH_AUDIT_STATE_PATH",
        "/var/lib/nutsnews/health-audit/last-run.json",
    )
)
WORKER_UPLIFT_RUNTIME_MANIFEST_PATH = Path(
    os.environ.get(
        "NUTSNEWS_WORKER_UPLIFT_RUNTIME_MANIFEST_PATH",
        "/etc/nutsnews-worker-uplift/services.json",
    )
)
POSTGRES_METRICS_DATABASE = os.environ.get("NUTSNEWS_METRICS_POSTGRES_DATABASE", "nutsnews_primary_shadow")
PUBLIC_FEED_STATUS_URL = os.environ.get(
    "NUTSNEWS_PUBLIC_FEED_STATUS_URL",
    "https://nutsnews-worker-0.nutsnews.workers.dev/public-feed-snapshot/status",
)
try:
    BACKUP_STALE_AFTER_HOURS = int(os.environ.get("NUTSNEWS_BACKUP_STALE_AFTER_HOURS", "30"))
except ValueError:
    BACKUP_STALE_AFTER_HOURS = 30
if not 1 <= BACKUP_STALE_AFTER_HOURS <= 7 * 24:
    BACKUP_STALE_AFTER_HOURS = 30
BACKUP_STALE_AFTER_SECONDS = BACKUP_STALE_AFTER_HOURS * 60 * 60
SAFE_POSTGRES_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_TRANSLATION_LANGUAGES = ("fr", "ja", "de-CH", "de", "el")
WORKER_UPLIFT_STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
WORKER_SERVICE_VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
WORKER_BUILD_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
WORKER_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DOCKER_STATS_TIMEOUT_SECONDS = 5
DOCKER_STATS_SERVICES = ("rabbitmq", *WORKER_UPLIFT_STAGES)
DOCKER_STATS_CONTAINER_ALLOWLIST = {
    "nutsnews-rabbitmq": "rabbitmq",
    "nutsnews-worker-uplift-scheduler-1": "scheduler",
    "nutsnews-worker-uplift-scheduler-2": "scheduler",
    "nutsnews-worker-uplift-scheduler-3": "scheduler",
    "nutsnews-worker-uplift-fetcher-1": "fetcher",
    "nutsnews-worker-uplift-fetcher-2": "fetcher",
    "nutsnews-worker-uplift-fetcher-3": "fetcher",
    "nutsnews-worker-uplift-canonicalizer-1": "canonicalizer",
    "nutsnews-worker-uplift-canonicalizer-2": "canonicalizer",
    "nutsnews-worker-uplift-canonicalizer-3": "canonicalizer",
    "nutsnews-worker-uplift-enrichment-1": "enrichment",
    "nutsnews-worker-uplift-enrichment-2": "enrichment",
    "nutsnews-worker-uplift-enrichment-3": "enrichment",
    "nutsnews-worker-uplift-approval-1": "approval",
    "nutsnews-worker-uplift-approval-2": "approval",
    "nutsnews-worker-uplift-approval-3": "approval",
    "nutsnews-worker-uplift-translation-1": "translation",
    "nutsnews-worker-uplift-translation-2": "translation",
    "nutsnews-worker-uplift-translation-3": "translation",
    "nutsnews-worker-uplift-persistence-1": "persistence",
    "nutsnews-worker-uplift-persistence-2": "persistence",
    "nutsnews-worker-uplift-persistence-3": "persistence",
    "nutsnews-worker-uplift-publication-1": "publication",
    "nutsnews-worker-uplift-publication-2": "publication",
    "nutsnews-worker-uplift-publication-3": "publication",
}
DOCKER_BYTE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
DOCKER_BYTE_VALUE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)\s*$",
    re.IGNORECASE,
)
DOCKER_PERCENT_VALUE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*$")
RABBITMQ_RECOVERY_STATUS_FILES = {
    "definition_export": "last-definition-export.json",
    "clean_rebuild_drill": "last-clean-rebuild-drill.json",
    "stopped_volume_restore_drill": "last-stopped-volume-restore-drill.json",
    "scheduled_check": "last-scheduled-check.json",
}
SEMANTIC_STATUS_SERVICE_UNITS = frozenset(
    {
        "nutsnews-newrelic-job-metrics.service",
        "nutsnews-postgres-replication-health.service",
    }
)
SERVICES = (
    "ssh",
    "ufw",
    "fail2ban",
    "caddy",
    "docker",
    "postgresql",
    "alloy",
    "sysstat",
    "nutsnews-backup.timer",
    "nutsnews-backup-verify.timer",
    "nutsnews-restore-drill.timer",
    "nutsnews-ops-dashboard-collect.timer",
    "nutsnews-rabbitmq-canary.timer",
    "nutsnews-worker-db-api",
    "nutsnews-supabase-sync-relay.timer",
)
STATUS_VALUE = {
    "healthy": 1,
    "warning": 0,
    "critical": 0,
    "unknown": 0,
    "not_configured": 0,
}
REPLICATION_LAG_STATUSES = {"healthy", "lagging", "inactive", "unknown", "not_configured"}
PUBLIC_FEED_SNAPSHOT_STATUSES = {"hit", "miss", "empty", "unbound", "disabled", "error", "unknown"}
HEALTH_AUDIT_CONCLUSIONS = {"success", "failure", "cancelled", "unknown"}
WORKER_UPLIFT_CONTROL_STATE_TUPLES = {
    "shadow": ("legacy_shards", True, True, False, "shadow_comparison"),
    "fenced": ("legacy_shards", False, False, False, "shadow_comparison"),
    "cutover_active": ("worker_uplift", False, True, True, "production"),
    "rollback_pending": ("legacy_shards", False, False, False, "shadow_comparison"),
}

WORKER_UPLIFT_CONTROL_STATE_QUERY = """
select json_build_object(
  'control_id', control_id,
  'generation', generation,
  'state', state,
  'active_ingestion_owner', active_ingestion_owner,
  'legacy_dispatch_enabled', legacy_dispatch_enabled,
  'uplift_scheduler_enabled', uplift_scheduler_enabled,
  'uplift_production_writes_enabled', uplift_production_writes_enabled,
  'publication_write_mode', publication_write_mode
)::text
from worker_uplift_final.cutover_control
where control_id = 'production'
"""

LEGACY_WORKER_METRICS_QUERY = """
select json_build_object(
  'last_run_at', max(coalesce(run_completed_at, run_started_at)),
  'last_success_at', max(coalesce(run_completed_at, run_started_at)) filter (where success is true),
  'last_run_success', (array_agg(success order by run_started_at desc nulls last, id desc))[1],
  'last_scheduled_run_at', max(coalesce(run_completed_at, run_started_at)) filter (
    where run_source = 'scheduled'
  ),
  'last_scheduled_success_at', max(coalesce(run_completed_at, run_started_at)) filter (
    where success is true and run_source = 'scheduled'
  ),
  'last_scheduled_run_success', (
    array_agg(success order by run_started_at desc nulls last, id desc)
    filter (where run_source = 'scheduled')
  )[1],
  'runs_24h', count(*) filter (where run_started_at >= now() - interval '24 hours'),
  'successful_runs_24h', count(*) filter (
    where success is true and run_started_at >= now() - interval '24 hours'
  ),
  'scheduled_runs_24h', count(*) filter (
    where run_source = 'scheduled' and run_started_at >= now() - interval '24 hours'
  ),
  'successful_scheduled_runs_24h', count(*) filter (
    where success is true
      and run_source = 'scheduled'
      and run_started_at >= now() - interval '24 hours'
  )
)::text
from public.worker_runs
"""

FEED_HEALTH_METRICS_QUERY = """
with active_feeds as (
  select url
  from public.rss_feeds
  where is_active is true
), latest_health as (
  select distinct on (feed_url)
         feed_url, last_checked_at, last_success_at, consecutive_failure_count,
         total_fetch_count, total_success_count, total_article_count, total_image_count,
         total_accepted_count
  from public.feed_health
  order by feed_url, updated_at desc nulls last, last_checked_at desc nulls last, id desc
), classified as (
  select
    a.url,
    h.last_checked_at,
    h.last_success_at,
    case
      when h.feed_url is null then 'untracked'
      when coalesce(h.consecutive_failure_count, 0) >= 3 then 'failed'
      when h.last_checked_at is null or h.last_checked_at < now() - interval '24 hours' then 'stale'
      when coalesce(h.total_fetch_count, 0) >= 5
       and coalesce(h.total_success_count, 0)::numeric / nullif(h.total_fetch_count, 0) < 0.70
        then 'warning'
      when coalesce(h.total_article_count, 0) >= 20
       and coalesce(h.total_image_count, 0)::numeric / nullif(h.total_article_count, 0) < 0.10
        then 'warning'
      when coalesce(h.total_fetch_count, 0) >= 5 and coalesce(h.total_accepted_count, 0) = 0
        then 'warning'
      else 'healthy'
    end as health_status
  from active_feeds a
  left join latest_health h on h.feed_url = a.url
)
select json_build_object(
  'active_count', count(*),
  'healthy_count', count(*) filter (where health_status = 'healthy'),
  'warning_count', count(*) filter (where health_status = 'warning'),
  'failed_count', count(*) filter (where health_status = 'failed'),
  'stale_count', count(*) filter (where health_status = 'stale'),
  'untracked_count', count(*) filter (where health_status = 'untracked'),
  'unhealthy_count', count(*) filter (where health_status <> 'healthy'),
  'oldest_checked_at', min(last_checked_at),
  'latest_checked_at', max(last_checked_at),
  'oldest_success_at', min(last_success_at),
  'latest_success_at', max(last_success_at)
)::text
from classified
"""

CONTENT_COVERAGE_METRICS_QUERY = """
with recent as (
  select original_url, image_url, published_on_site_at
  from public.articles
  where status = 'published'
  order by published_on_site_at desc nulls last, created_at desc nulls last, id desc
  limit 60
), snapshot as (
  select original_url, image_url, published_on_site_at
  from public.public_feed_snapshot
)
select json_build_object(
  'snapshot_rows', (select count(*) from snapshot),
  'latest_published_at', (select max(published_on_site_at) from snapshot),
  'recent_sample_rows', (select count(*) from recent),
  'recent_image_rows', (
    select count(*) from recent where image_url is not null and btrim(image_url) <> ''
  ),
  'recent_translated_pairs', (
    select count(distinct (recent.original_url, summaries.language_code))
    from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code in ('fr', 'ja', 'de-CH', 'de', 'el')
  ),
  'translated_fr', (
    select count(distinct recent.original_url) from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code = 'fr'
  ),
  'translated_ja', (
    select count(distinct recent.original_url) from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code = 'ja'
  ),
  'translated_de_ch', (
    select count(distinct recent.original_url) from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code = 'de-CH'
  ),
  'translated_de', (
    select count(distinct recent.original_url) from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code = 'de'
  ),
  'translated_el', (
    select count(distinct recent.original_url) from recent
    join public.article_summaries summaries on summaries.original_url = recent.original_url
    where summaries.language_code = 'el'
  )
)::text
"""

AI_USAGE_METRICS_QUERY = """
select json_build_object(
  'runs_24h', count(*),
  'last_run_at', max(coalesce(run_completed_at, run_started_at)),
  'local_calls_24h', coalesce(sum(local_ai_call_count), 0),
  'openai_calls_24h', coalesce(sum(openai_call_count), 0),
  'local_tokens_24h', coalesce(sum(local_ai_total_tokens), 0),
  'openai_tokens_24h', coalesce(sum(openai_total_tokens), 0),
  'openai_estimated_cost_usd_24h', coalesce(sum(estimated_openai_cost_usd), 0),
  'cost_protection_events_24h', count(*) filter (where cost_protection_limit_reached is true),
  'spike_warning_events_24h', count(*) filter (where spike_warning_triggered is true)
)::text
from public.ai_usage_runs
where run_started_at >= now() - interval '24 hours'
"""

DATABASE_GROWTH_METRICS_QUERY = """
select json_build_object(
  'database_size_bytes', pg_database_size(current_database()),
  'articles_rows', (select count(*) from public.articles),
  'article_summaries_rows', (select count(*) from public.article_summaries),
  'worker_runs_rows', (select count(*) from public.worker_runs),
  'ai_usage_runs_rows', (select count(*) from public.ai_usage_runs)
)::text
"""

WORKER_UPLIFT_OUTBOX_METRICS_QUERY = """
with backlog as (
  select 'scheduler' as stage, min(created_at) as oldest_at, count(*) as pending_count
  from worker_uplift_scheduler.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'fetcher', min(created_at), count(*)
  from worker_uplift_fetcher.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'canonicalizer', min(created_at), count(*)
  from worker_uplift_canonicalizer.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'enrichment', min(created_at), count(*)
  from worker_uplift_enrichment.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'approval', min(created_at), count(*)
  from worker_uplift_approval.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'translation', min(created_at), count(*)
  from worker_uplift_translation.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'persistence', min(created_at), count(*)
  from worker_uplift_persistence.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
  union all
  select 'publication', min(created_at), count(*)
  from worker_uplift_publication.outbox
  where status in ('pending', 'published', 'retrying') and confirmed_at is null
)
select json_object_agg(
  stage,
  json_build_object(
    'oldest_age_seconds', coalesce(
      greatest(0, floor(extract(epoch from now() - oldest_at)))::bigint,
      0
    ),
    'pending_count', pending_count
  )
)::text
from backlog
"""


def run(command: list[str], timeout: int = 8) -> str:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def shell(command: str, timeout: int = 8) -> str:
    return run(["sh", "-lc", command], timeout=timeout)


def failed_systemd_unit_names(output: str) -> list[str]:
    stripped = output.strip()
    if not stripped:
        return []
    if stripped.isdigit():
        return [f"unknown-failed-unit-{index}" for index in range(int(stripped))]
    names: list[str] = []
    for line in stripped.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] in {"●", "*"} and len(fields) > 1:
            names.append(fields[1])
        else:
            names.append(fields[0].lstrip("●"))
    return names


def actionable_failed_systemd_unit_names(output: str) -> list[str]:
    return sorted(
        unit for unit in failed_systemd_unit_names(output) if unit not in SEMANTIC_STATUS_SERVICE_UNITS
    )


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if labels:
        rendered = ",".join(f'{key}="{label(raw)}"' for key, raw in sorted(labels.items()))
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def docker_percent(value: Any) -> float:
    match = DOCKER_PERCENT_VALUE.fullmatch(str(value or ""))
    if match is None:
        raise ValueError("invalid Docker percentage")
    parsed = float(match.group(1))
    if not math.isfinite(parsed):
        raise ValueError("non-finite Docker percentage")
    return parsed


def docker_bytes(value: Any) -> int:
    match = DOCKER_BYTE_VALUE.fullmatch(str(value or ""))
    if match is None:
        raise ValueError("invalid Docker byte value")
    parsed = float(match.group(1)) * DOCKER_BYTE_UNITS[match.group(2).lower()]
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("invalid Docker byte value")
    return int(round(parsed))


def docker_pair(value: Any) -> tuple[int, int]:
    parts = str(value or "").split("/", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("invalid Docker IO pair")
    return docker_bytes(parts[0]), docker_bytes(parts[1])


def docker_pids(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit():
        raise ValueError("invalid Docker PID count")
    return int(raw)


def parse_docker_stats(output: str) -> list[dict[str, int | float | str]]:
    """Parse only exact approved container names; raw identities never become labels."""
    rows: list[dict[str, int | float | str]] = []
    seen_containers: set[str] = set()
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parsed = json.loads(raw_line)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("Name"), str):
            raise ValueError("invalid Docker stats row")
        container_name = parsed["Name"]
        service = DOCKER_STATS_CONTAINER_ALLOWLIST.get(container_name)
        if service is None:
            continue
        if container_name in seen_containers:
            raise ValueError("duplicate Docker stats row")
        seen_containers.add(container_name)
        memory_used, memory_limit = docker_pair(parsed.get("MemUsage"))
        network_receive, network_transmit = docker_pair(parsed.get("NetIO"))
        block_read, block_write = docker_pair(parsed.get("BlockIO"))
        # Parse Docker's percentage too so a malformed snapshot fails closed,
        # while the emitted aggregate is derived from the byte totals below.
        docker_percent(parsed.get("MemPerc"))
        rows.append(
            {
                "service": service,
                "cpu_percent": docker_percent(parsed.get("CPUPerc")),
                "memory_used_bytes": memory_used,
                "memory_limit_bytes": memory_limit,
                "network_receive_bytes": network_receive,
                "network_transmit_bytes": network_transmit,
                "block_read_bytes": block_read,
                "block_write_bytes": block_write,
                "pids": docker_pids(parsed.get("PIDs")),
            }
        )
    return rows


def aggregate_docker_stats(
    rows: list[dict[str, int | float | str]],
) -> dict[str, dict[str, int | float]]:
    aggregates: dict[str, dict[str, int | float]] = {}
    for row in rows:
        service = str(row["service"])
        aggregate = aggregates.setdefault(
            service,
            {
                "container_count": 0,
                "cpu_percent": 0.0,
                "memory_used_bytes": 0,
                "memory_limit_bytes": 0,
                "network_receive_bytes": 0,
                "network_transmit_bytes": 0,
                "block_read_bytes": 0,
                "block_write_bytes": 0,
                "pids": 0,
            },
        )
        aggregate["container_count"] += 1
        for key in (
            "cpu_percent",
            "memory_used_bytes",
            "memory_limit_bytes",
            "network_receive_bytes",
            "network_transmit_bytes",
            "block_read_bytes",
            "block_write_bytes",
            "pids",
        ):
            aggregate[key] += row[key]  # type: ignore[operator]
    return aggregates


def docker_stats_headers() -> list[str]:
    return [
        "# HELP nutsnews_docker_stats_collection_available Whether the bounded Docker stats command and parser completed successfully.",
        "# TYPE nutsnews_docker_stats_collection_available gauge",
        "# HELP nutsnews_docker_approved_services Fixed number of approved RabbitMQ and NutsNews worker services.",
        "# TYPE nutsnews_docker_approved_services gauge",
        "# HELP nutsnews_docker_available_services Approved services represented by at least one running container, or -1 when collection is unavailable.",
        "# TYPE nutsnews_docker_available_services gauge",
        "# HELP nutsnews_docker_stats_available Whether Docker stats are available for an approved service.",
        "# TYPE nutsnews_docker_stats_available gauge",
        "# HELP nutsnews_docker_container_count Running approved containers aggregated by bounded service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_count gauge",
        "# HELP nutsnews_docker_container_cpu_percent Aggregate current CPU use for running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_cpu_percent gauge",
        "# HELP nutsnews_docker_container_memory_used_bytes Aggregate current memory use for running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_memory_used_bytes gauge",
        "# HELP nutsnews_docker_container_memory_limit_bytes Aggregate memory limit for running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_memory_limit_bytes gauge",
        "# HELP nutsnews_docker_container_memory_used_percent Aggregate memory use as a percentage of aggregate limits, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_memory_used_percent gauge",
        "# HELP nutsnews_docker_container_network_receive_bytes Aggregate network bytes received by running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_network_receive_bytes gauge",
        "# HELP nutsnews_docker_container_network_transmit_bytes Aggregate network bytes transmitted by running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_network_transmit_bytes gauge",
        "# HELP nutsnews_docker_container_block_read_bytes Aggregate block bytes read by running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_block_read_bytes gauge",
        "# HELP nutsnews_docker_container_block_write_bytes Aggregate block bytes written by running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_block_write_bytes gauge",
        "# HELP nutsnews_docker_container_pids Aggregate current PID count for running containers of an approved service, or -1 when unavailable.",
        "# TYPE nutsnews_docker_container_pids gauge",
        "# HELP nutsnews_docker_aggregate_container_count Running containers across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_container_count gauge",
        "# HELP nutsnews_docker_aggregate_cpu_percent Aggregate current CPU use across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_cpu_percent gauge",
        "# HELP nutsnews_docker_aggregate_memory_used_bytes Aggregate current memory use across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_memory_used_bytes gauge",
        "# HELP nutsnews_docker_aggregate_memory_limit_bytes Aggregate memory limits across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_memory_limit_bytes gauge",
        "# HELP nutsnews_docker_aggregate_memory_used_percent Aggregate memory use as a percentage of all approved-service limits, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_memory_used_percent gauge",
        "# HELP nutsnews_docker_aggregate_network_receive_bytes Aggregate network bytes received across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_network_receive_bytes gauge",
        "# HELP nutsnews_docker_aggregate_network_transmit_bytes Aggregate network bytes transmitted across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_network_transmit_bytes gauge",
        "# HELP nutsnews_docker_aggregate_block_read_bytes Aggregate block bytes read across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_block_read_bytes gauge",
        "# HELP nutsnews_docker_aggregate_block_write_bytes Aggregate block bytes written across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_block_write_bytes gauge",
        "# HELP nutsnews_docker_aggregate_pids Aggregate current PID count across all approved services, or -1 when unavailable.",
        "# TYPE nutsnews_docker_aggregate_pids gauge",
    ]


def render_docker_stats(
    rows: list[dict[str, int | float | str]] | None,
) -> list[str]:
    collection_available = rows is not None
    aggregates = aggregate_docker_stats(rows or [])
    lines = [
        *docker_stats_headers(),
        metric("nutsnews_docker_stats_collection_available", 1 if collection_available else 0),
        metric("nutsnews_docker_approved_services", len(DOCKER_STATS_SERVICES)),
        metric(
            "nutsnews_docker_available_services",
            len(aggregates) if collection_available else -1,
        ),
    ]
    aggregate_total = aggregate_docker_stats(
        [
            {**row, "service": "approved"}
            for row in (rows or [])
        ]
    ).get("approved")

    for service in DOCKER_STATS_SERVICES:
        values = aggregates.get(service)
        available = collection_available and values is not None
        labels = {"service": service}
        memory_limit = values["memory_limit_bytes"] if values is not None else 0
        memory_percent = (
            round(100 * values["memory_used_bytes"] / memory_limit, 6)
            if values is not None and memory_limit > 0
            else -1
        )
        lines.extend(
            [
                metric("nutsnews_docker_stats_available", 1 if available else 0, labels),
                metric(
                    "nutsnews_docker_container_count",
                    values["container_count"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_cpu_percent",
                    values["cpu_percent"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_memory_used_bytes",
                    values["memory_used_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_memory_limit_bytes",
                    values["memory_limit_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_memory_used_percent",
                    memory_percent,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_network_receive_bytes",
                    values["network_receive_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_network_transmit_bytes",
                    values["network_transmit_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_block_read_bytes",
                    values["block_read_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_block_write_bytes",
                    values["block_write_bytes"] if available and values is not None else -1,
                    labels,
                ),
                metric(
                    "nutsnews_docker_container_pids",
                    values["pids"] if available and values is not None else -1,
                    labels,
                ),
            ]
        )

    aggregate_memory_limit = (
        aggregate_total["memory_limit_bytes"] if aggregate_total is not None else 0
    )
    aggregate_memory_percent = (
        round(
            100 * aggregate_total["memory_used_bytes"] / aggregate_memory_limit,
            6,
        )
        if aggregate_total is not None and aggregate_memory_limit > 0
        else 0
        if collection_available
        else -1
    )
    aggregate_values = {
        "container_count": 0,
        "cpu_percent": 0,
        "memory_used_bytes": 0,
        "memory_limit_bytes": 0,
        "memory_used_percent": aggregate_memory_percent,
        "network_receive_bytes": 0,
        "network_transmit_bytes": 0,
        "block_read_bytes": 0,
        "block_write_bytes": 0,
        "pids": 0,
        **(aggregate_total or {}),
    }
    for suffix in (
        "container_count",
        "cpu_percent",
        "memory_used_bytes",
        "memory_limit_bytes",
        "memory_used_percent",
        "network_receive_bytes",
        "network_transmit_bytes",
        "block_read_bytes",
        "block_write_bytes",
        "pids",
    ):
        lines.append(
            metric(
                f"nutsnews_docker_aggregate_{suffix}",
                aggregate_values[suffix] if collection_available else -1,
            )
        )
    return lines


def docker_stats_unavailable_metric_lines() -> list[str]:
    return render_docker_stats(None)


def docker_stats_metric_lines() -> list[str]:
    try:
        completed = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=DOCKER_STATS_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            return docker_stats_unavailable_metric_lines()
        rows = parse_docker_stats(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return docker_stats_unavailable_metric_lines()
    return render_docker_stats(rows)


CONTROL_STATE_UNSET = object()


def validated_worker_uplift_control_state(row: Any) -> dict[str, Any] | None:
    """Validate one production control row without inferring missing values."""
    if not isinstance(row, dict):
        return None
    state = row.get("state")
    generation = row.get("generation")
    if (
        row.get("control_id") != "production"
        or type(generation) is not int
        or generation <= 0
        or not isinstance(state, str)
        or state not in WORKER_UPLIFT_CONTROL_STATE_TUPLES
    ):
        return None
    boolean_fields = (
        "legacy_dispatch_enabled",
        "uplift_scheduler_enabled",
        "uplift_production_writes_enabled",
    )
    if any(type(row.get(field)) is not bool for field in boolean_fields):
        return None
    observed = (
        row.get("active_ingestion_owner"),
        row.get("legacy_dispatch_enabled"),
        row.get("uplift_scheduler_enabled"),
        row.get("uplift_production_writes_enabled"),
        row.get("publication_write_mode"),
    )
    if observed != WORKER_UPLIFT_CONTROL_STATE_TUPLES[state]:
        return None
    return {
        "generation": generation,
        "state": state,
        "active_ingestion_owner": observed[0],
        "legacy_dispatch_enabled": observed[1],
        "uplift_scheduler_enabled": observed[2],
        "uplift_production_writes_enabled": observed[3],
        "publication_write_mode": observed[4],
    }


def worker_uplift_ownership_metric_lines(
    control_row: Any = CONTROL_STATE_UNSET,
) -> list[str]:
    """Project ownership only from the authoritative production cutover row."""
    if control_row is CONTROL_STATE_UNSET:
        control_row = postgres_json_query(WORKER_UPLIFT_CONTROL_STATE_QUERY)
    control = validated_worker_uplift_control_state(control_row)
    available = control is not None
    if control is None:
        generation = 0
        state = "unknown"
        ingestion_owner = "unknown"
        legacy_dispatch_enabled = 0
        uplift_scheduler_enabled = 0
        production_writes_enabled = 0
        publication_write_mode = "unknown"
        expected_active = 0
        mode = "unknown"
        write_gate = "unknown"
    else:
        generation = int(control["generation"])
        state = str(control["state"])
        ingestion_owner = str(control["active_ingestion_owner"])
        legacy_dispatch_enabled = int(control["legacy_dispatch_enabled"])
        uplift_scheduler_enabled = int(control["uplift_scheduler_enabled"])
        production_writes_enabled = int(control["uplift_production_writes_enabled"])
        publication_write_mode = str(control["publication_write_mode"])
        expected_active = 1 if state == "cutover_active" else 0
        mode = "production" if expected_active else "shadow"
        write_gate = "enabled" if production_writes_enabled else "disabled"
    return [
        "# HELP nutsnews_backend_worker_uplift_ownership_available Whether the authoritative production cutover row is available and internally consistent.",
        "# TYPE nutsnews_backend_worker_uplift_ownership_available gauge",
        metric("nutsnews_backend_worker_uplift_ownership_available", 1 if available else 0),
        "# HELP nutsnews_backend_worker_uplift_control_generation Monotonic authoritative cutover-control generation, or 0 when unavailable.",
        "# TYPE nutsnews_backend_worker_uplift_control_generation gauge",
        metric("nutsnews_backend_worker_uplift_control_generation", generation),
        "# HELP nutsnews_backend_worker_uplift_expected_active Whether worker uplift owns production ingestion; meaningful only when ownership_available is 1.",
        "# TYPE nutsnews_backend_worker_uplift_expected_active gauge",
        metric("nutsnews_backend_worker_uplift_expected_active", expected_active),
        "# HELP nutsnews_backend_worker_uplift_legacy_dispatch_enabled Authoritative legacy-dispatch flag.",
        "# TYPE nutsnews_backend_worker_uplift_legacy_dispatch_enabled gauge",
        metric("nutsnews_backend_worker_uplift_legacy_dispatch_enabled", legacy_dispatch_enabled),
        "# HELP nutsnews_backend_worker_uplift_scheduler_enabled Authoritative uplift-scheduler flag.",
        "# TYPE nutsnews_backend_worker_uplift_scheduler_enabled gauge",
        metric("nutsnews_backend_worker_uplift_scheduler_enabled", uplift_scheduler_enabled),
        "# HELP nutsnews_backend_worker_uplift_production_writes_enabled Authoritative uplift production-write flag.",
        "# TYPE nutsnews_backend_worker_uplift_production_writes_enabled gauge",
        metric("nutsnews_backend_worker_uplift_production_writes_enabled", production_writes_enabled),
        "# HELP nutsnews_backend_worker_uplift_deployment_info Authoritative bounded ingestion-ownership state.",
        "# TYPE nutsnews_backend_worker_uplift_deployment_info gauge",
        metric(
            "nutsnews_backend_worker_uplift_deployment_info",
            1,
            {
                "ingestion_owner": ingestion_owner,
                "mode": mode,
                "publication_write_mode": publication_write_mode,
                "state": state,
                "write_gate": write_gate,
            },
        ),
    ]


def worker_uplift_observability_contract_metric_lines() -> list[str]:
    """Expose that current 0.x workers provide only basic scrape health."""
    status = os.environ.get(
        "NUTSNEWS_WORKER_UPLIFT_OBSERVABILITY_CONTRACT_STATUS",
        "awaiting-qualified-v1",
    ).strip()
    raw_enabled = os.environ.get(
        "NUTSNEWS_WORKER_UPLIFT_OBSERVABILITY_CONTRACT_ENABLED",
        "false",
    ).strip().lower()
    enabled_valid = raw_enabled in {"0", "false"}
    available = status == "awaiting-qualified-v1" and enabled_valid
    normalized_status = status if available else "invalid"
    return [
        "# HELP nutsnews_backend_worker_uplift_observability_contract_available Whether the bounded worker observability-contract declaration is valid.",
        "# TYPE nutsnews_backend_worker_uplift_observability_contract_available gauge",
        metric(
            "nutsnews_backend_worker_uplift_observability_contract_available",
            1 if available else 0,
        ),
        "# HELP nutsnews_backend_worker_uplift_observability_contract_enabled Whether qualified v1 worker telemetry may be used.",
        "# TYPE nutsnews_backend_worker_uplift_observability_contract_enabled gauge",
        metric("nutsnews_backend_worker_uplift_observability_contract_enabled", 0),
        "# HELP nutsnews_backend_worker_uplift_observability_contract_info Current bounded worker telemetry contract state.",
        "# TYPE nutsnews_backend_worker_uplift_observability_contract_info gauge",
        metric(
            "nutsnews_backend_worker_uplift_observability_contract_info",
            1,
            {"status": normalized_status},
        ),
    ]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_configured"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
    return data if isinstance(data, dict) else {"status": "unknown"}


def running_worker_uplift_label_sets() -> list[dict[str, str]] | None:
    """Read only bounded immutable identity labels from running worker containers."""
    try:
        containers = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.nutsnews.service",
                "--filter",
                "label=com.docker.compose.project=nutsnews-worker-uplift",
                "--format",
                "{{.ID}}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if containers.returncode != 0:
        return None
    container_ids = [
        value.strip() for value in containers.stdout.splitlines() if value.strip()
    ]
    if len(container_ids) > 24 or any(
        not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in container_ids
    ):
        return None
    if not container_ids:
        return []
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                (
                    '[{{json .Config.Image}},'
                    '{{json (index .Config.Labels "com.nutsnews.service")}},'
                    '{{json (index .Config.Labels "com.nutsnews.service_version")}},'
                    '{{json (index .Config.Labels "com.nutsnews.revision")}},'
                    '{{json (index .Config.Labels "com.nutsnews.image_digest")}}]'
                ),
                *container_ids,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inspected.returncode != 0:
        return None
    label_sets: list[dict[str, str]] = []
    for raw in inspected.stdout.splitlines():
        try:
            identity = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(identity, list) or len(identity) != 5 or not all(
            isinstance(value, str) for value in identity
        ):
            return None
        image, service, service_version, revision, image_digest = identity
        label_sets.append(
            {
                "__config_image": image,
                "com.nutsnews.service": service,
                "com.nutsnews.service_version": service_version,
                "com.nutsnews.revision": revision,
                "com.nutsnews.image_digest": image_digest,
            }
        )
    return label_sets


def worker_uplift_deployed_identity_metric_lines(
    manifest_path: Path = WORKER_UPLIFT_RUNTIME_MANIFEST_PATH,
    running_label_sets: list[dict[str, str]] | None = None,
) -> list[str]:
    """Export identities only when running containers match the reviewed manifest."""
    manifest = read_json(manifest_path)
    services = manifest.get("services")
    valid = bool(
        manifest.get("schema_version") == 1
        and manifest.get("generated_by") == "backend_worker_runtime"
        and isinstance(services, list)
        and len(services) == len(WORKER_UPLIFT_STAGES)
    )
    configured_identities: dict[str, dict[str, str]] = {}
    if valid and isinstance(services, list):
        for service in services:
            if not isinstance(service, dict):
                valid = False
                break
            name = str(service.get("name") or "")
            image = str(service.get("image") or "")
            provenance = service.get("provenance")
            service_version = str(service.get("service_version") or "0.1.0")
            revision = str(service.get("build_revision") or service.get("image_tag") or "")
            image_digest = str(
                service.get("image_digest")
                or (
                    provenance.get("subject_digest")
                    if isinstance(provenance, dict)
                    else ""
                )
            )
            identity_valid = bool(
                name in WORKER_UPLIFT_STAGES
                and name not in configured_identities
                and WORKER_SERVICE_VERSION_RE.fullmatch(service_version)
                and WORKER_BUILD_REVISION_RE.fullmatch(revision)
                and WORKER_IMAGE_DIGEST_RE.fullmatch(image_digest)
                and str(service.get("image_tag") or "") == revision
                and image.endswith(f"@{image_digest}")
                and isinstance(provenance, dict)
                and provenance.get("subject_digest") == image_digest
            )
            if not identity_valid:
                valid = False
                break
            configured_identities[name] = {
                "worker_service": name,
                "service_version": service_version,
                "revision": revision,
                "image_digest": image_digest,
                "image_reference": image,
            }
    valid = valid and set(configured_identities) == set(WORKER_UPLIFT_STAGES)

    observed = (
        running_label_sets
        if running_label_sets is not None
        else running_worker_uplift_label_sets()
    )
    observed_identities: dict[str, dict[str, str]] = {}
    observed_counts: dict[str, int] = {}
    if valid and observed is not None and len(WORKER_UPLIFT_STAGES) <= len(observed) <= 24:
        for labels in observed:
            name = labels.get("com.nutsnews.service", "")
            if name not in WORKER_UPLIFT_STAGES:
                valid = False
                break
            identity = {
                "worker_service": name,
                "service_version": labels.get("com.nutsnews.service_version", ""),
                "revision": labels.get("com.nutsnews.revision", ""),
                "image_digest": labels.get("com.nutsnews.image_digest", ""),
                "image_reference": labels.get("__config_image", ""),
            }
            if identity != configured_identities.get(name):
                valid = False
                break
            prior = observed_identities.get(name)
            if prior is not None and prior != identity:
                valid = False
                break
            observed_counts[name] = observed_counts.get(name, 0) + 1
            if observed_counts[name] > 3:
                valid = False
                break
            observed_identities[name] = identity
    else:
        valid = False
    valid = valid and set(observed_identities) == set(WORKER_UPLIFT_STAGES)

    lines = [
        "# HELP nutsnews_backend_worker_uplift_deployed_identity_available Whether all eight deployed worker identities are valid and available.",
        "# TYPE nutsnews_backend_worker_uplift_deployed_identity_available gauge",
        metric("nutsnews_backend_worker_uplift_deployed_identity_available", 1 if valid else 0),
        "# HELP nutsnews_backend_worker_uplift_deployed_service_info Exact deployed worker service version, Git revision, and image digest.",
        "# TYPE nutsnews_backend_worker_uplift_deployed_service_info gauge",
    ]
    if valid:
        for name in WORKER_UPLIFT_STAGES:
            lines.append(
                metric(
                    "nutsnews_backend_worker_uplift_deployed_service_info",
                    1,
                    {
                        key: value
                        for key, value in observed_identities[name].items()
                        if key != "image_reference"
                    },
                )
            )
    return lines


def service_active(service: str) -> int:
    return 1 if shell(f"systemctl is-active {service} 2>/dev/null || true") == "active" else 0


def service_enabled(service: str) -> int:
    return (
        1
        if shell(f"systemctl is-enabled {service} 2>/dev/null || true")
        in {"enabled", "static"}
        else 0
    )


def backup_stage_status(stage: str, data: dict[str, Any]) -> tuple[str, int]:
    status = str(data.get("freshness_status") or data.get("status") or "not_configured")
    if status not in STATUS_VALUE:
        status = "unknown"
    return status, STATUS_VALUE.get(status, 0)


def postgres_status_value(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "not_configured")
    if status in STATUS_VALUE:
        return status
    return "unknown"


def age_seconds(timestamp: str, now: int) -> int | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, now - int(parsed.timestamp()))


def timestamp_seconds(timestamp: str) -> int | None:
    if not timestamp:
        return None
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def numeric_value(data: dict[str, Any] | None, key: str) -> int | float | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def numeric_or_unavailable(data: dict[str, Any] | None, key: str) -> int | float:
    value = numeric_value(data, key)
    return value if value is not None else -1


def ratio_value(numerator: int | float | None, denominator: int | float | None) -> float:
    if numerator is None or denominator is None or denominator <= 0:
        return -1
    return round(float(numerator) / float(denominator), 6)


def postgres_json_query(query: str, *, database: str | None = None, timeout: int = 8) -> dict[str, Any] | None:
    selected_database = database or POSTGRES_METRICS_DATABASE
    if not SAFE_POSTGRES_IDENTIFIER.fullmatch(selected_database):
        return None
    environment = os.environ.copy()
    environment["PGCONNECT_TIMEOUT"] = "3"
    environment["PGOPTIONS"] = "-c statement_timeout=5000"
    try:
        completed = subprocess.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                f"--dbname={selected_database}",
                f"--command={query}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def fetch_json_url(url: str, *, timeout: int = 8) -> dict[str, Any] | None:
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        return None
    try:
        completed = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "3",
                "--max-time",
                str(timeout),
                url,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def relay_validation_values(data: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    """Return failed tables and row lag only for complete post-sync validation."""
    summary = data.get("validation_summary", {})
    if not isinstance(summary, dict) or summary.get("safe_metadata_only") is not True:
        return None, None, False
    expected = summary.get("expected_table_count")
    validated = summary.get("validated_table_count")
    failed = summary.get("failed_table_count")
    max_lag_rows = summary.get("max_table_lag_rows")
    complete = (
        summary.get("complete") is True
        and type(expected) is int
        and expected > 0
        and type(validated) is int
        and validated == expected
        and type(failed) is int
        and 0 <= failed <= expected
    )
    if not complete:
        return None, None, False
    normalized_lag = max_lag_rows if type(max_lag_rows) is int and max_lag_rows >= 0 else None
    return failed, normalized_lag, True


def relay_failed_table_count(data: dict[str, Any]) -> int | None:
    failed, _, complete = relay_validation_values(data)
    return failed if complete else None


def relay_last_success_timestamp(data: dict[str, Any]) -> int | None:
    """Use durable history, never a successful-looking dry-run timestamp."""
    persisted = timestamp_seconds(str(data.get("last_success_at_utc") or ""))
    if persisted is not None:
        return persisted
    sync = data.get("sync", {})
    post_sync = data.get("post_sync", {})
    if not (
        data.get("safe_metadata_only") is True
        and data.get("mode") == "sync-once"
        and data.get("status") == "pass"
        and isinstance(sync, dict)
        and sync.get("status") == "applied"
        and isinstance(post_sync, dict)
        and post_sync.get("status") == "pass"
    ):
        return None
    return timestamp_seconds(str(data.get("completed_at_utc") or ""))


def exporter_state_lines(now: int, *, available: bool) -> list[str]:
    return [
        "# HELP nutsnews_backend_metric_scrape_timestamp_seconds Unix timestamp of this textfile metric snapshot.",
        "# TYPE nutsnews_backend_metric_scrape_timestamp_seconds gauge",
        metric("nutsnews_backend_metric_scrape_timestamp_seconds", now),
        "# HELP nutsnews_backend_metric_exporter_available Whether the textfile exporter completed its collection.",
        "# TYPE nutsnews_backend_metric_exporter_available gauge",
        metric("nutsnews_backend_metric_exporter_available", 1 if available else 0),
        "# HELP nutsnews_backend_metric_exporter_error Whether the latest textfile collection failed.",
        "# TYPE nutsnews_backend_metric_exporter_error gauge",
        metric("nutsnews_backend_metric_exporter_error", 0 if available else 1),
    ]


def alloy_readiness_metric_lines(ready: int) -> list[str]:
    return [
        "# HELP nutsnews_alloy_readiness_probe_success Whether the backend host could complete Alloy's loopback readiness probe.",
        "# TYPE nutsnews_alloy_readiness_probe_success gauge",
        metric("nutsnews_alloy_readiness_probe_success", ready),
        "# HELP nutsnews_alloy_ready Whether the backend Alloy instance reported ready on its loopback readiness endpoint.",
        "# TYPE nutsnews_alloy_ready gauge",
        metric("nutsnews_alloy_ready", ready),
    ]


def backup_history_metric_lines(now: int, state: dict[str, Any]) -> list[str]:
    valid_shape = (
        state.get("schema_version") == 1
        and state.get("action") == "backup"
        and state.get("status") in {"healthy", "critical"}
    )
    last_run_timestamp = (
        timestamp_seconds(str(state.get("last_run_at_utc") or "")) if valid_shape else None
    )
    if last_run_timestamp is not None and last_run_timestamp > now + 300:
        last_run_timestamp = None
    available = last_run_timestamp is not None
    last_success_timestamp = (
        timestamp_seconds(str(state.get("last_success_at_utc") or "")) if available else None
    )
    if (
        last_success_timestamp is not None
        and (
            last_success_timestamp > now + 300
            or last_run_timestamp is None
            or last_success_timestamp > last_run_timestamp
        )
    ):
        last_success_timestamp = None
    last_run_age = max(0, now - last_run_timestamp) if last_run_timestamp is not None else -1
    last_success_age = (
        max(0, now - last_success_timestamp) if last_success_timestamp is not None else -1
    )
    success_fresh = (
        1
        if last_success_timestamp is not None and last_success_age <= BACKUP_STALE_AFTER_SECONDS
        else 0
        if last_success_timestamp is not None
        else -1
    )

    return [
        "# HELP nutsnews_backend_backup_status_available Whether the bounded backup run state is valid and timestamped.",
        "# TYPE nutsnews_backend_backup_status_available gauge",
        metric("nutsnews_backend_backup_status_available", 1 if available else 0),
        "# HELP nutsnews_backend_backup_last_run_timestamp_seconds Timestamp of the latest backup attempt, successful or failed; 0 when unavailable.",
        "# TYPE nutsnews_backend_backup_last_run_timestamp_seconds gauge",
        metric("nutsnews_backend_backup_last_run_timestamp_seconds", last_run_timestamp or 0),
        "# HELP nutsnews_backend_backup_last_run_age_seconds Age of the latest backup attempt, successful or failed; -1 when unavailable.",
        "# TYPE nutsnews_backend_backup_last_run_age_seconds gauge",
        metric("nutsnews_backend_backup_last_run_age_seconds", last_run_age),
        "# HELP nutsnews_backend_backup_last_success_timestamp_seconds Timestamp of the latest successfully verified backup; 0 when unavailable.",
        "# TYPE nutsnews_backend_backup_last_success_timestamp_seconds gauge",
        metric("nutsnews_backend_backup_last_success_timestamp_seconds", last_success_timestamp or 0),
        "# HELP nutsnews_backend_backup_last_success_age_seconds Age of the latest successfully verified backup; -1 when unavailable.",
        "# TYPE nutsnews_backend_backup_last_success_age_seconds gauge",
        metric("nutsnews_backend_backup_last_success_age_seconds", last_success_age),
        "# HELP nutsnews_backend_backup_last_success_fresh Whether the latest successfully verified backup is within the configured stale threshold; -1 when unavailable.",
        "# TYPE nutsnews_backend_backup_last_success_fresh gauge",
        metric("nutsnews_backend_backup_last_success_fresh", success_fresh),
        "# HELP nutsnews_backend_backup_stale_after_seconds Configured age threshold for a successfully verified backup.",
        "# TYPE nutsnews_backend_backup_stale_after_seconds gauge",
        metric("nutsnews_backend_backup_stale_after_seconds", BACKUP_STALE_AFTER_SECONDS),
    ]


def health_audit_metric_lines(now: int) -> list[str]:
    state = read_json(HEALTH_AUDIT_STATE_PATH)
    valid_state = (
        state.get("schema_version") == 1
        and state.get("safe_metadata_only") is True
        and state.get("source") == "github_actions"
    )
    audit_available = valid_state and state.get("available") is True
    raw_conclusion = str(state.get("conclusion") or "unknown") if valid_state else "unknown"
    conclusion = raw_conclusion if raw_conclusion in HEALTH_AUDIT_CONCLUSIONS else "unknown"
    last_run_timestamp = (
        timestamp_seconds(str(state.get("last_run_at_utc") or "")) if valid_state else None
    )
    last_success_timestamp = (
        timestamp_seconds(str(state.get("last_success_at_utc") or "")) if valid_state else None
    )
    consecutive_failures = numeric_value(state, "consecutive_failures") if valid_state else None
    critical_checks = numeric_value(state, "critical_checks") if valid_state else None
    expected_interval = numeric_value(state, "expected_interval_seconds") if valid_state else None
    if expected_interval is None or not 3600 <= expected_interval <= 7 * 24 * 60 * 60:
        expected_interval = 24 * 60 * 60

    return [
        "# HELP nutsnews_backend_health_audit_available Whether the latest bounded GitHub health-audit report was available for publication.",
        "# TYPE nutsnews_backend_health_audit_available gauge",
        metric("nutsnews_backend_health_audit_available", 1 if audit_available else 0),
        "# HELP nutsnews_backend_health_audit_conclusion Bounded conclusion of the latest scheduled backend health audit.",
        "# TYPE nutsnews_backend_health_audit_conclusion gauge",
        metric("nutsnews_backend_health_audit_conclusion", 1, {"conclusion": conclusion}),
        "# HELP nutsnews_backend_health_audit_last_run_timestamp_seconds Timestamp of the latest backend health-audit attempt, or 0 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_last_run_timestamp_seconds gauge",
        metric("nutsnews_backend_health_audit_last_run_timestamp_seconds", last_run_timestamp or 0),
        "# HELP nutsnews_backend_health_audit_last_run_age_seconds Age of the latest backend health-audit attempt, or -1 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_last_run_age_seconds gauge",
        metric(
            "nutsnews_backend_health_audit_last_run_age_seconds",
            max(0, now - last_run_timestamp) if last_run_timestamp is not None else -1,
        ),
        "# HELP nutsnews_backend_health_audit_last_success_timestamp_seconds Timestamp of the latest successful backend health audit, or 0 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_last_success_timestamp_seconds gauge",
        metric(
            "nutsnews_backend_health_audit_last_success_timestamp_seconds",
            last_success_timestamp or 0,
        ),
        "# HELP nutsnews_backend_health_audit_last_success_age_seconds Age of the latest successful backend health audit, or -1 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_last_success_age_seconds gauge",
        metric(
            "nutsnews_backend_health_audit_last_success_age_seconds",
            max(0, now - last_success_timestamp) if last_success_timestamp is not None else -1,
        ),
        "# HELP nutsnews_backend_health_audit_consecutive_failures Consecutive scheduled health-audit failures, or -1 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_consecutive_failures gauge",
        metric(
            "nutsnews_backend_health_audit_consecutive_failures",
            consecutive_failures if consecutive_failures is not None else -1,
        ),
        "# HELP nutsnews_backend_health_audit_critical_checks Critical checks in the latest health audit, or -1 when unavailable.",
        "# TYPE nutsnews_backend_health_audit_critical_checks gauge",
        metric(
            "nutsnews_backend_health_audit_critical_checks",
            critical_checks if critical_checks is not None else -1,
        ),
        "# HELP nutsnews_backend_health_audit_expected_interval_seconds Expected scheduled interval between backend health audits.",
        "# TYPE nutsnews_backend_health_audit_expected_interval_seconds gauge",
        metric("nutsnews_backend_health_audit_expected_interval_seconds", expected_interval),
    ]


def durable_content_metric_lines(now: int) -> list[str]:
    """Collect bounded database-backed service indicators with explicit unknown states."""
    worker = postgres_json_query(LEGACY_WORKER_METRICS_QUERY)
    worker_available = worker is not None
    worker_last_run_timestamp = timestamp_seconds(str((worker or {}).get("last_run_at") or ""))
    worker_last_success_timestamp = timestamp_seconds(str((worker or {}).get("last_success_at") or ""))
    worker_last_scheduled_run_timestamp = timestamp_seconds(
        str((worker or {}).get("last_scheduled_run_at") or "")
    )
    worker_last_scheduled_success_timestamp = timestamp_seconds(
        str((worker or {}).get("last_scheduled_success_at") or "")
    )
    worker_last_run_age = (
        max(0, now - worker_last_run_timestamp) if worker_last_run_timestamp is not None else -1
    )
    worker_last_success_age = (
        max(0, now - worker_last_success_timestamp) if worker_last_success_timestamp is not None else -1
    )
    worker_last_scheduled_run_age = (
        max(0, now - worker_last_scheduled_run_timestamp)
        if worker_last_scheduled_run_timestamp is not None
        else -1
    )
    worker_last_scheduled_success_age = (
        max(0, now - worker_last_scheduled_success_timestamp)
        if worker_last_scheduled_success_timestamp is not None
        else -1
    )
    raw_last_run_success = (worker or {}).get("last_run_success")
    worker_last_run_success = (
        1 if raw_last_run_success is True else 0 if raw_last_run_success is False else -1
    )
    raw_last_scheduled_run_success = (worker or {}).get("last_scheduled_run_success")
    worker_last_scheduled_run_success = (
        1
        if raw_last_scheduled_run_success is True
        else 0
        if raw_last_scheduled_run_success is False
        else -1
    )

    feed = postgres_json_query(FEED_HEALTH_METRICS_QUERY)
    feed_available = feed is not None
    feed_oldest_checked_timestamp = timestamp_seconds(str((feed or {}).get("oldest_checked_at") or ""))
    feed_latest_checked_timestamp = timestamp_seconds(str((feed or {}).get("latest_checked_at") or ""))
    feed_oldest_success_timestamp = timestamp_seconds(str((feed or {}).get("oldest_success_at") or ""))
    feed_latest_success_timestamp = timestamp_seconds(str((feed or {}).get("latest_success_at") or ""))

    content = postgres_json_query(CONTENT_COVERAGE_METRICS_QUERY)
    content_available = content is not None
    snapshot_rows = numeric_value(content, "snapshot_rows")
    snapshot_latest_timestamp = timestamp_seconds(str((content or {}).get("latest_published_at") or ""))
    recent_sample_rows = numeric_value(content, "recent_sample_rows")
    recent_image_rows = numeric_value(content, "recent_image_rows")
    recent_translated_pairs = numeric_value(content, "recent_translated_pairs")

    ai_usage = postgres_json_query(AI_USAGE_METRICS_QUERY)
    ai_available = ai_usage is not None
    ai_last_run_timestamp = timestamp_seconds(str((ai_usage or {}).get("last_run_at") or ""))

    database = postgres_json_query(DATABASE_GROWTH_METRICS_QUERY)
    database_available = database is not None

    worker_uplift_outbox = postgres_json_query(WORKER_UPLIFT_OUTBOX_METRICS_QUERY)

    public_snapshot = fetch_json_url(PUBLIC_FEED_STATUS_URL)
    public_snapshot_available = public_snapshot is not None
    raw_public_snapshot_status = str((public_snapshot or {}).get("status") or "unknown")
    public_snapshot_status = (
        raw_public_snapshot_status
        if raw_public_snapshot_status in PUBLIC_FEED_SNAPSHOT_STATUSES
        else "unknown"
    )
    public_snapshot_timestamp = timestamp_seconds(
        str(
            (public_snapshot or {}).get("refreshedAt")
            or (public_snapshot or {}).get("updatedAt")
            or ""
        )
    )
    public_snapshot_age = (
        max(0, now - public_snapshot_timestamp)
        if public_snapshot_timestamp is not None
        else numeric_or_unavailable(public_snapshot, "ageSeconds")
    )
    raw_public_snapshot_ready = (public_snapshot or {}).get("ready")
    public_snapshot_ready = (
        1 if raw_public_snapshot_ready is True else 0 if raw_public_snapshot_ready is False else -1
    )

    lines = [
        "# HELP nutsnews_backend_legacy_worker_available Whether aggregate legacy-worker telemetry is queryable.",
        "# TYPE nutsnews_backend_legacy_worker_available gauge",
        metric("nutsnews_backend_legacy_worker_available", 1 if worker_available else 0),
        "# HELP nutsnews_backend_legacy_worker_last_run_success Whether the latest legacy-worker run succeeded, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_run_success gauge",
        metric("nutsnews_backend_legacy_worker_last_run_success", worker_last_run_success),
        "# HELP nutsnews_backend_legacy_worker_last_run_timestamp_seconds Timestamp of the latest legacy-worker run, or 0 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_run_timestamp_seconds gauge",
        metric("nutsnews_backend_legacy_worker_last_run_timestamp_seconds", worker_last_run_timestamp or 0),
        "# HELP nutsnews_backend_legacy_worker_last_run_age_seconds Age of the latest legacy-worker run, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_run_age_seconds gauge",
        metric("nutsnews_backend_legacy_worker_last_run_age_seconds", worker_last_run_age),
        "# HELP nutsnews_backend_legacy_worker_last_success_timestamp_seconds Timestamp of the latest successful legacy-worker run, or 0 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_success_timestamp_seconds gauge",
        metric("nutsnews_backend_legacy_worker_last_success_timestamp_seconds", worker_last_success_timestamp or 0),
        "# HELP nutsnews_backend_legacy_worker_last_success_age_seconds Age of the latest successful legacy-worker run, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_success_age_seconds gauge",
        metric("nutsnews_backend_legacy_worker_last_success_age_seconds", worker_last_success_age),
        "# HELP nutsnews_backend_legacy_worker_fresh_within_15_minutes Whether the latest successful scheduled legacy-worker run is at most 15 minutes old, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_fresh_within_15_minutes gauge",
        metric(
            "nutsnews_backend_legacy_worker_fresh_within_15_minutes",
            1
            if 0 <= worker_last_scheduled_success_age <= 900
            else 0
            if worker_last_scheduled_success_age >= 0
            else -1,
        ),
        "# HELP nutsnews_backend_legacy_worker_runs_24h Legacy-worker runs started in the last 24 hours, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_runs_24h gauge",
        metric("nutsnews_backend_legacy_worker_runs_24h", numeric_or_unavailable(worker, "runs_24h")),
        "# HELP nutsnews_backend_legacy_worker_successful_runs_24h Successful legacy-worker runs started in the last 24 hours, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_successful_runs_24h gauge",
        metric(
            "nutsnews_backend_legacy_worker_successful_runs_24h",
            numeric_or_unavailable(worker, "successful_runs_24h"),
        ),
        "# HELP nutsnews_backend_legacy_worker_last_scheduled_run_success Whether the latest scheduled legacy-worker run succeeded, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_scheduled_run_success gauge",
        metric(
            "nutsnews_backend_legacy_worker_last_scheduled_run_success",
            worker_last_scheduled_run_success,
        ),
        "# HELP nutsnews_backend_legacy_worker_last_scheduled_run_timestamp_seconds Timestamp of the latest scheduled legacy-worker run, or 0 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_scheduled_run_timestamp_seconds gauge",
        metric(
            "nutsnews_backend_legacy_worker_last_scheduled_run_timestamp_seconds",
            worker_last_scheduled_run_timestamp or 0,
        ),
        "# HELP nutsnews_backend_legacy_worker_last_scheduled_run_age_seconds Age of the latest scheduled legacy-worker run, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_scheduled_run_age_seconds gauge",
        metric(
            "nutsnews_backend_legacy_worker_last_scheduled_run_age_seconds",
            worker_last_scheduled_run_age,
        ),
        "# HELP nutsnews_backend_legacy_worker_last_scheduled_success_timestamp_seconds Timestamp of the latest successful scheduled legacy-worker run, or 0 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_scheduled_success_timestamp_seconds gauge",
        metric(
            "nutsnews_backend_legacy_worker_last_scheduled_success_timestamp_seconds",
            worker_last_scheduled_success_timestamp or 0,
        ),
        "# HELP nutsnews_backend_legacy_worker_last_scheduled_success_age_seconds Age of the latest successful scheduled legacy-worker run, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_last_scheduled_success_age_seconds gauge",
        metric(
            "nutsnews_backend_legacy_worker_last_scheduled_success_age_seconds",
            worker_last_scheduled_success_age,
        ),
        "# HELP nutsnews_backend_legacy_worker_scheduled_runs_24h Scheduled legacy-worker runs started in the last 24 hours, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_scheduled_runs_24h gauge",
        metric(
            "nutsnews_backend_legacy_worker_scheduled_runs_24h",
            numeric_or_unavailable(worker, "scheduled_runs_24h"),
        ),
        "# HELP nutsnews_backend_legacy_worker_successful_scheduled_runs_24h Successful scheduled legacy-worker runs started in the last 24 hours, or -1 when unavailable.",
        "# TYPE nutsnews_backend_legacy_worker_successful_scheduled_runs_24h gauge",
        metric(
            "nutsnews_backend_legacy_worker_successful_scheduled_runs_24h",
            numeric_or_unavailable(worker, "successful_scheduled_runs_24h"),
        ),
        "# HELP nutsnews_backend_feed_health_available Whether aggregate feed-health telemetry is queryable.",
        "# TYPE nutsnews_backend_feed_health_available gauge",
        metric("nutsnews_backend_feed_health_available", 1 if feed_available else 0),
    ]

    for key, suffix in (
        ("active_count", "active_count"),
        ("healthy_count", "healthy_count"),
        ("warning_count", "warning_count"),
        ("failed_count", "failed_count"),
        ("unhealthy_count", "unhealthy_count"),
        ("stale_count", "stale_count"),
        ("untracked_count", "untracked_count"),
    ):
        lines.extend(
            [
                f"# HELP nutsnews_backend_feed_{suffix} Aggregate active-feed {suffix.replace('_', ' ')}, or -1 when unavailable.",
                f"# TYPE nutsnews_backend_feed_{suffix} gauge",
                metric(
                    f"nutsnews_backend_feed_{suffix}",
                    numeric_or_unavailable(feed, key),
                ),
            ]
        )

    lines.extend(
        [
            "# HELP nutsnews_backend_feed_oldest_check_age_seconds Age of the oldest active feed's last check, or -1 when unavailable.",
            "# TYPE nutsnews_backend_feed_oldest_check_age_seconds gauge",
            metric(
                "nutsnews_backend_feed_oldest_check_age_seconds",
                max(0, now - feed_oldest_checked_timestamp) if feed_oldest_checked_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_feed_latest_check_age_seconds Age of the newest active feed check, or -1 when unavailable.",
            "# TYPE nutsnews_backend_feed_latest_check_age_seconds gauge",
            metric(
                "nutsnews_backend_feed_latest_check_age_seconds",
                max(0, now - feed_latest_checked_timestamp) if feed_latest_checked_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_feed_oldest_success_age_seconds Age of the oldest active feed's last success, or -1 when unavailable.",
            "# TYPE nutsnews_backend_feed_oldest_success_age_seconds gauge",
            metric(
                "nutsnews_backend_feed_oldest_success_age_seconds",
                max(0, now - feed_oldest_success_timestamp) if feed_oldest_success_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_feed_latest_success_age_seconds Age of the newest active feed success, or -1 when unavailable.",
            "# TYPE nutsnews_backend_feed_latest_success_age_seconds gauge",
            metric(
                "nutsnews_backend_feed_latest_success_age_seconds",
                max(0, now - feed_latest_success_timestamp) if feed_latest_success_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_content_coverage_available Whether aggregate content-coverage telemetry is queryable.",
            "# TYPE nutsnews_backend_content_coverage_available gauge",
            metric("nutsnews_backend_content_coverage_available", 1 if content_available else 0),
            "# HELP nutsnews_backend_public_feed_snapshot_rows Current durable public-feed snapshot row count, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_snapshot_rows gauge",
            metric("nutsnews_backend_public_feed_snapshot_rows", snapshot_rows if snapshot_rows is not None else -1),
            "# HELP nutsnews_backend_recent_published_content_sample_rows Recent published article sample size used for image and translation coverage, or -1 when unavailable.",
            "# TYPE nutsnews_backend_recent_published_content_sample_rows gauge",
            metric(
                "nutsnews_backend_recent_published_content_sample_rows",
                recent_sample_rows if recent_sample_rows is not None else -1,
            ),
            "# HELP nutsnews_backend_public_feed_snapshot_newest_content_timestamp_seconds Timestamp of the newest durable published item, or 0 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_snapshot_newest_content_timestamp_seconds gauge",
            metric("nutsnews_backend_public_feed_snapshot_newest_content_timestamp_seconds", snapshot_latest_timestamp or 0),
            "# HELP nutsnews_backend_public_feed_snapshot_newest_content_age_seconds Age of the newest durable published item, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_snapshot_newest_content_age_seconds gauge",
            metric(
                "nutsnews_backend_public_feed_snapshot_newest_content_age_seconds",
                max(0, now - snapshot_latest_timestamp) if snapshot_latest_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_recent_published_image_coverage_ratio Fraction of the latest 60 published articles with an image, or -1 when unavailable.",
            "# TYPE nutsnews_backend_recent_published_image_coverage_ratio gauge",
            metric(
                "nutsnews_backend_recent_published_image_coverage_ratio",
                ratio_value(recent_image_rows, recent_sample_rows),
            ),
            "# HELP nutsnews_backend_recent_published_translation_coverage_ratio Fraction of required language pairs present for the latest 60 published articles, or -1 when unavailable.",
            "# TYPE nutsnews_backend_recent_published_translation_coverage_ratio gauge",
            metric(
                "nutsnews_backend_recent_published_translation_coverage_ratio",
                ratio_value(
                    recent_translated_pairs,
                    recent_sample_rows * len(REQUIRED_TRANSLATION_LANGUAGES)
                    if recent_sample_rows is not None
                    else None,
                ),
            ),
            "# HELP nutsnews_backend_recent_published_language_coverage_ratio Fraction of the latest 60 published articles translated for a required language, or -1 when unavailable.",
            "# TYPE nutsnews_backend_recent_published_language_coverage_ratio gauge",
        ]
    )
    for language, key in (
        ("fr", "translated_fr"),
        ("ja", "translated_ja"),
        ("de-CH", "translated_de_ch"),
        ("de", "translated_de"),
        ("el", "translated_el"),
    ):
        lines.append(
            metric(
                "nutsnews_backend_recent_published_language_coverage_ratio",
                ratio_value(numeric_value(content, key), recent_sample_rows),
                {"language": language},
            )
        )

    lines.extend(
        [
            "# HELP nutsnews_backend_ai_usage_available Whether aggregate AI-usage telemetry is queryable.",
            "# TYPE nutsnews_backend_ai_usage_available gauge",
            metric("nutsnews_backend_ai_usage_available", 1 if ai_available else 0),
            "# HELP nutsnews_backend_ai_runs_24h AI usage records started in the last 24 hours, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_runs_24h gauge",
            metric("nutsnews_backend_ai_runs_24h", numeric_or_unavailable(ai_usage, "runs_24h")),
            "# HELP nutsnews_backend_ai_last_run_age_seconds Age of the newest AI usage record, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_last_run_age_seconds gauge",
            metric(
                "nutsnews_backend_ai_last_run_age_seconds",
                max(0, now - ai_last_run_timestamp) if ai_last_run_timestamp is not None else -1,
            ),
            "# HELP nutsnews_backend_ai_calls_24h AI calls in the last 24 hours by bounded provider, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_calls_24h gauge",
            metric(
                "nutsnews_backend_ai_calls_24h",
                numeric_or_unavailable(ai_usage, "local_calls_24h"),
                {"provider": "local"},
            ),
            metric(
                "nutsnews_backend_ai_calls_24h",
                numeric_or_unavailable(ai_usage, "openai_calls_24h"),
                {"provider": "openai"},
            ),
            "# HELP nutsnews_backend_ai_tokens_24h AI tokens in the last 24 hours by bounded provider, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_tokens_24h gauge",
            metric(
                "nutsnews_backend_ai_tokens_24h",
                numeric_or_unavailable(ai_usage, "local_tokens_24h"),
                {"provider": "local"},
            ),
            metric(
                "nutsnews_backend_ai_tokens_24h",
                numeric_or_unavailable(ai_usage, "openai_tokens_24h"),
                {"provider": "openai"},
            ),
            "# HELP nutsnews_backend_ai_estimated_cost_usd_24h Estimated AI cost in USD in the last 24 hours by bounded provider, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_estimated_cost_usd_24h gauge",
            metric(
                "nutsnews_backend_ai_estimated_cost_usd_24h",
                numeric_or_unavailable(ai_usage, "openai_estimated_cost_usd_24h"),
                {"provider": "openai"},
            ),
            "# HELP nutsnews_backend_ai_cost_protection_events_24h AI cost-protection events in the last 24 hours, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_cost_protection_events_24h gauge",
            metric(
                "nutsnews_backend_ai_cost_protection_events_24h",
                numeric_or_unavailable(ai_usage, "cost_protection_events_24h"),
            ),
            "# HELP nutsnews_backend_ai_spike_warning_events_24h AI spike-warning events in the last 24 hours, or -1 when unavailable.",
            "# TYPE nutsnews_backend_ai_spike_warning_events_24h gauge",
            metric(
                "nutsnews_backend_ai_spike_warning_events_24h",
                numeric_or_unavailable(ai_usage, "spike_warning_events_24h"),
            ),
            "# HELP nutsnews_backend_database_growth_available Whether aggregate database-growth telemetry is queryable.",
            "# TYPE nutsnews_backend_database_growth_available gauge",
            metric("nutsnews_backend_database_growth_available", 1 if database_available else 0),
            "# HELP nutsnews_backend_database_size_bytes Current primary-shadow database size in bytes, or -1 when unavailable.",
            "# TYPE nutsnews_backend_database_size_bytes gauge",
            metric(
                "nutsnews_backend_database_size_bytes",
                numeric_or_unavailable(database, "database_size_bytes"),
            ),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_available Whether the durable public-feed edge snapshot status is queryable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_available gauge",
            metric(
                "nutsnews_backend_public_feed_edge_snapshot_available",
                1 if public_snapshot_available else 0,
            ),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_ready Whether the durable public-feed edge snapshot is ready, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_ready gauge",
            metric("nutsnews_backend_public_feed_edge_snapshot_ready", public_snapshot_ready),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_status Bounded durable public-feed edge snapshot state.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_status gauge",
            metric(
                "nutsnews_backend_public_feed_edge_snapshot_status",
                1,
                {"status": public_snapshot_status},
            ),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_refresh_timestamp_seconds Durable public-feed edge snapshot refresh timestamp, or 0 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_refresh_timestamp_seconds gauge",
            metric(
                "nutsnews_backend_public_feed_edge_snapshot_refresh_timestamp_seconds",
                public_snapshot_timestamp or 0,
            ),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_age_seconds Age of the durable public-feed edge snapshot, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_age_seconds gauge",
            metric("nutsnews_backend_public_feed_edge_snapshot_age_seconds", public_snapshot_age),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_articles Durable public-feed edge snapshot article count, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_articles gauge",
            metric(
                "nutsnews_backend_public_feed_edge_snapshot_articles",
                numeric_or_unavailable(public_snapshot, "articleCount"),
            ),
            "# HELP nutsnews_backend_public_feed_edge_snapshot_max_articles Configured durable public-feed edge snapshot capacity, or -1 when unavailable.",
            "# TYPE nutsnews_backend_public_feed_edge_snapshot_max_articles gauge",
            metric(
                "nutsnews_backend_public_feed_edge_snapshot_max_articles",
                numeric_or_unavailable(public_snapshot, "maxArticles"),
            ),
        ]
    )
    for key, suffix in (
        ("articles_rows", "articles_rows"),
        ("article_summaries_rows", "article_summaries_rows"),
        ("worker_runs_rows", "worker_runs_rows"),
        ("ai_usage_runs_rows", "ai_usage_runs_rows"),
    ):
        lines.extend(
            [
                f"# HELP nutsnews_backend_database_{suffix} Current durable {suffix.replace('_', ' ')}, or -1 when unavailable.",
                f"# TYPE nutsnews_backend_database_{suffix} gauge",
                metric(
                    f"nutsnews_backend_database_{suffix}",
                    numeric_or_unavailable(database, key),
                ),
            ]
        )
    lines.extend(
        [
            "# HELP nutsnews_backend_worker_uplift_outbox_available Whether worker-owned outbox backlog telemetry is queryable by bounded stage.",
            "# TYPE nutsnews_backend_worker_uplift_outbox_available gauge",
            "# HELP nutsnews_backend_worker_uplift_oldest_unconfirmed_outbox_age_seconds Age of the oldest unconfirmed worker-owned outbox item; 0 means no backlog and -1 unavailable.",
            "# TYPE nutsnews_backend_worker_uplift_oldest_unconfirmed_outbox_age_seconds gauge",
            "# HELP nutsnews_backend_worker_uplift_unconfirmed_outbox_items Worker-owned unconfirmed outbox items, or -1 when unavailable.",
            "# TYPE nutsnews_backend_worker_uplift_unconfirmed_outbox_items gauge",
        ]
    )
    for stage in WORKER_UPLIFT_STAGES:
        stage_data = (worker_uplift_outbox or {}).get(stage)
        stage_available = isinstance(stage_data, dict)
        lines.append(
            metric(
                "nutsnews_backend_worker_uplift_outbox_available",
                1 if stage_available else 0,
                {"stage": stage},
            )
        )
        lines.append(
            metric(
                "nutsnews_backend_worker_uplift_oldest_unconfirmed_outbox_age_seconds",
                numeric_or_unavailable(stage_data, "oldest_age_seconds"),
                {"stage": stage},
            )
        )
        lines.append(
            metric(
                "nutsnews_backend_worker_uplift_unconfirmed_outbox_items",
                numeric_or_unavailable(stage_data, "pending_count"),
                {"stage": stage},
            )
        )
    return lines


def collect() -> list[str]:
    now = int(datetime.now(UTC).timestamp())
    lines = [
        *exporter_state_lines(now, available=True),
        "# HELP nutsnews_backend_service_active Whether a backend service or timer is active.",
        "# TYPE nutsnews_backend_service_active gauge",
    ]

    for service in SERVICES:
        lines.append(metric("nutsnews_backend_service_active", service_active(service), {"unit": service}))

    failed_units = actionable_failed_systemd_unit_names(
        shell("systemctl --failed --no-legend --no-pager --plain || true")
    )
    upgradable = shell("apt list --upgradable 2>/dev/null | tail -n +2 | wc -l")
    reboot_required = 1 if Path("/var/run/reboot-required").exists() else 0
    endpoint_body = shell(
        "curl -fsS --connect-timeout 5 --resolve backend.nutsnews.com:443:127.0.0.1 "
        "https://backend.nutsnews.com/readyz 2>/dev/null || true"
    )
    alloy_ready = 1 if shell(
        "curl -fsS --connect-timeout 3 --max-time 5 "
        "http://127.0.0.1:12345/-/ready >/dev/null 2>&1 && printf ready || printf unavailable"
    ) == "ready" else 0
    try:
        endpoint = json.loads(endpoint_body)
    except (json.JSONDecodeError, TypeError):
        endpoint = {}
    endpoint_ready = (
        isinstance(endpoint, dict)
        and endpoint.get("ready") is True
        and endpoint.get("status") == "ready"
        and endpoint.get("deploymentEnvironment") == "production"
        and isinstance(endpoint.get("serviceVersion"), str)
        and bool(endpoint.get("serviceVersion"))
    )

    lines.extend(
        [
            "# HELP nutsnews_backend_failed_systemd_units Failed systemd units not covered by dedicated semantic checks.",
            "# TYPE nutsnews_backend_failed_systemd_units gauge",
            metric("nutsnews_backend_failed_systemd_units", len(failed_units)),
            "# HELP nutsnews_backend_upgradable_packages APT packages visible as upgradable.",
            "# TYPE nutsnews_backend_upgradable_packages gauge",
            metric("nutsnews_backend_upgradable_packages", int(upgradable or "0")),
            "# HELP nutsnews_backend_reboot_required Whether /var/run/reboot-required exists.",
            "# TYPE nutsnews_backend_reboot_required gauge",
            metric("nutsnews_backend_reboot_required", reboot_required),
            "# HELP nutsnews_backend_public_endpoint_ready Whether the local HTTPS readiness endpoint validates PostgreSQL and deployment identity.",
            "# TYPE nutsnews_backend_public_endpoint_ready gauge",
            metric("nutsnews_backend_public_endpoint_ready", 1 if endpoint_ready else 0),
            *alloy_readiness_metric_lines(alloy_ready),
        ]
    )

    backup = read_json(BACKUP_STATE_DIR / "last-backup.json")
    verification = read_json(BACKUP_STATE_DIR / "last-verification.json")
    restore_drill = read_json(BACKUP_STATE_DIR / "last-restore-verification.json")
    lines.extend(
        [
            "# HELP nutsnews_backend_backup_stage_healthy Whether a backup stage is healthy.",
            "# TYPE nutsnews_backend_backup_stage_healthy gauge",
            "# HELP nutsnews_backend_backup_stage_status Status value for a backup stage.",
            "# TYPE nutsnews_backend_backup_stage_status gauge",
        ]
    )
    for stage, data in {
        "backup": backup,
        "verification": verification,
        "restore_drill": restore_drill,
    }.items():
        status, healthy = backup_stage_status(stage, data)
        lines.append(metric("nutsnews_backend_backup_stage_healthy", healthy, {"stage": stage}))
        lines.append(metric("nutsnews_backend_backup_stage_status", 1, {"stage": stage, "status": status}))

    lines.extend(backup_history_metric_lines(now, backup))

    quota = backup.get("quota", {}) if isinstance(backup, dict) else {}
    quota_status = str(quota.get("status") or "not_configured") if isinstance(quota, dict) else "not_configured"
    verified = 1 if backup.get("latest_snapshot_verified_at_utc") else 0
    lines.extend(
        [
            "# HELP nutsnews_backend_backup_latest_snapshot_verified Whether the latest backup snapshot has a successful verification record.",
            "# TYPE nutsnews_backend_backup_latest_snapshot_verified gauge",
            metric("nutsnews_backend_backup_latest_snapshot_verified", verified),
            "# HELP nutsnews_backend_backup_storage_quota_configured Whether backup storage quota guardrail is configured.",
            "# TYPE nutsnews_backend_backup_storage_quota_configured gauge",
            metric("nutsnews_backend_backup_storage_quota_configured", 0 if quota_status == "not_configured" else 1),
        ]
    )

    rabbitmq_recovery = {
        stage: read_json(RABBITMQ_RECOVERY_STATE_DIR / filename)
        for stage, filename in RABBITMQ_RECOVERY_STATUS_FILES.items()
    }
    lines.extend(
        [
            "# HELP nutsnews_backend_rabbitmq_recovery_stage_healthy Whether a RabbitMQ recovery evidence stage is healthy.",
            "# TYPE nutsnews_backend_rabbitmq_recovery_stage_healthy gauge",
            "# HELP nutsnews_backend_rabbitmq_recovery_stage_status Status value for a RabbitMQ recovery evidence stage.",
            "# TYPE nutsnews_backend_rabbitmq_recovery_stage_status gauge",
        ]
    )
    for stage, data in rabbitmq_recovery.items():
        status, healthy = backup_stage_status(stage, data)
        lines.append(metric("nutsnews_backend_rabbitmq_recovery_stage_healthy", healthy, {"stage": stage}))
        lines.append(metric("nutsnews_backend_rabbitmq_recovery_stage_status", 1, {"stage": stage, "status": status}))
    definition_age = age_seconds(str(rabbitmq_recovery["definition_export"].get("finished_at_utc") or ""), now)
    if definition_age is not None:
        lines.extend(
            [
                "# HELP nutsnews_backend_rabbitmq_definition_export_age_seconds Age of the last sanitized RabbitMQ definition export.",
                "# TYPE nutsnews_backend_rabbitmq_definition_export_age_seconds gauge",
                metric("nutsnews_backend_rabbitmq_definition_export_age_seconds", definition_age),
            ]
        )

    postgres = read_json(POSTGRES_STATE_DIR / "status.json")
    replication_health = read_json(POSTGRES_REPLICATION_HEALTH_PATH)
    if isinstance(replication_health.get("replication"), dict):
        postgres["replication"] = replication_health["replication"]
    restore_drill = (
        postgres.get("last_restore_drill", {})
        if isinstance(postgres.get("last_restore_drill"), dict)
        else {}
    )
    restore_drill_status = postgres_status_value(restore_drill)
    restore_drill_age = age_seconds(str(restore_drill.get("completed_at_utc") or ""), now)
    postgres_status = postgres_status_value(postgres)
    replication = postgres.get("replication", {}) if isinstance(postgres, dict) else {}
    raw_lag_status = str(replication.get("lag_status") or "not_configured") if isinstance(replication, dict) else "not_configured"
    lag_status = raw_lag_status if raw_lag_status in REPLICATION_LAG_STATUSES else "unknown"
    max_lag_seconds = replication.get("max_lag_seconds") if isinstance(replication, dict) else None
    blocker_count = len(replication.get("blockers", [])) if isinstance(replication.get("blockers"), list) else 0
    replication_checked_at = str(
        replication_health.get("checked_at_utc")
        or (replication.get("checked_at_utc") if isinstance(replication, dict) else "")
        or ""
    )
    replication_evidence_age = age_seconds(replication_checked_at, now)
    replication_stale_threshold = numeric_value(replication, "validation_stale_threshold_seconds") or 900
    replication_fresh = (
        replication_evidence_age is not None and replication_evidence_age <= replication_stale_threshold
    )
    replication_required = lag_status != "not_configured"
    failover_ready = 1 if (
        postgres_status == "healthy"
        and restore_drill_status == "healthy"
        and (
            not replication_required
            or (replication_fresh and lag_status == "healthy" and blocker_count == 0)
        )
    ) else 0
    lines.extend(
        [
            "# HELP nutsnews_backend_postgres_failover_telemetry_available Whether bounded failover-state telemetry is available.",
            "# TYPE nutsnews_backend_postgres_failover_telemetry_available gauge",
            metric(
                "nutsnews_backend_postgres_failover_telemetry_available",
                1 if postgres_status in {"healthy", "warning", "critical"} else 0,
            ),
            "# HELP nutsnews_backend_postgres_failover_status Bounded PostgreSQL failover-state status.",
            "# TYPE nutsnews_backend_postgres_failover_status gauge",
            metric("nutsnews_backend_postgres_failover_status", 1, {"status": postgres_status}),
            "# HELP nutsnews_backend_postgres_failover_ready Whether the private PostgreSQL failover target has a healthy restore drill.",
            "# TYPE nutsnews_backend_postgres_failover_ready gauge",
            metric("nutsnews_backend_postgres_failover_ready", failover_ready),
            "# HELP nutsnews_backend_postgres_restore_drill_healthy Whether the latest PostgreSQL restore drill is healthy.",
            "# TYPE nutsnews_backend_postgres_restore_drill_healthy gauge",
            metric("nutsnews_backend_postgres_restore_drill_healthy", STATUS_VALUE.get(restore_drill_status, 0), {"status": restore_drill_status}),
            "# HELP nutsnews_backend_postgres_restore_drill_age_seconds Age of the latest PostgreSQL restore drill, or -1 when unavailable.",
            "# TYPE nutsnews_backend_postgres_restore_drill_age_seconds gauge",
            metric(
                "nutsnews_backend_postgres_restore_drill_age_seconds",
                restore_drill_age if restore_drill_age is not None else -1,
            ),
            "# HELP nutsnews_backend_postgres_replication_lag_configured Whether continuous replication lag is configured for the selected topology.",
            "# TYPE nutsnews_backend_postgres_replication_lag_configured gauge",
            metric("nutsnews_backend_postgres_replication_lag_configured", 0 if lag_status == "not_configured" else 1, {"status": lag_status}),
            "# HELP nutsnews_backend_postgres_replication_blockers Current replication health blocker count.",
            "# TYPE nutsnews_backend_postgres_replication_blockers gauge",
            metric("nutsnews_backend_postgres_replication_blockers", blocker_count),
            "# HELP nutsnews_backend_postgres_replication_telemetry_available Whether replication evidence has a valid observation timestamp.",
            "# TYPE nutsnews_backend_postgres_replication_telemetry_available gauge",
            metric("nutsnews_backend_postgres_replication_telemetry_available", 1 if replication_evidence_age is not None else 0),
            "# HELP nutsnews_backend_postgres_replication_telemetry_fresh Whether replication evidence is within its declared freshness threshold.",
            "# TYPE nutsnews_backend_postgres_replication_telemetry_fresh gauge",
            metric("nutsnews_backend_postgres_replication_telemetry_fresh", 1 if replication_fresh else 0),
            "# HELP nutsnews_backend_postgres_replication_telemetry_age_seconds Age of replication evidence, or -1 when unavailable.",
            "# TYPE nutsnews_backend_postgres_replication_telemetry_age_seconds gauge",
            metric(
                "nutsnews_backend_postgres_replication_telemetry_age_seconds",
                replication_evidence_age if replication_evidence_age is not None else -1,
            ),
            "# HELP nutsnews_backend_postgres_replication_max_lag_seconds Maximum observed logical replication lag, or -1 when unavailable.",
            "# TYPE nutsnews_backend_postgres_replication_max_lag_seconds gauge",
            metric(
                "nutsnews_backend_postgres_replication_max_lag_seconds",
                max_lag_seconds
                if replication_fresh and isinstance(max_lag_seconds, (int, float))
                else -1,
            ),
        ]
    )

    relay = read_json(SUPABASE_SYNC_RELAY_STATE_PATH)
    relay_expected_active = bool(
        service_enabled("nutsnews-supabase-sync-relay.timer")
    )
    raw_relay_status = str(relay.get("status") or "not_configured")
    relay_status = (
        raw_relay_status
        if raw_relay_status
        in {"pass", "fail", "blocked", "skipped_with_reason", "not_configured", "unknown"}
        else "unknown"
    )
    failed_table_count, max_table_lag_rows, validation_complete = relay_validation_values(relay)
    last_applied_at = str(relay.get("last_applied_at_utc") or "")
    relay_checked_at = str(relay.get("checked_at_utc") or "")
    relay_finished_at = str(relay.get("finished_at_utc") or "")
    last_applied_timestamp = timestamp_seconds(last_applied_at)
    if last_applied_timestamp is not None and last_applied_timestamp > now + 300:
        last_applied_timestamp = None
    relay_lag_seconds = (
        max(0, now - last_applied_timestamp)
        if last_applied_timestamp is not None
        else None
    )
    relay_collector_age = age_seconds(relay_finished_at, now)
    relay_collector_fresh = relay_collector_age is not None and relay_collector_age <= 600
    last_success_timestamp = relay_last_success_timestamp(relay)
    if last_success_timestamp is not None and last_success_timestamp > now + 300:
        last_success_timestamp = None
    relay_available = (
        relay.get("schema_version") == 2
        and relay.get("safe_metadata_only") is True
        and relay_status in {"pass", "fail", "blocked", "skipped_with_reason"}
        and timestamp_seconds(relay_checked_at) is not None
        and timestamp_seconds(relay_finished_at) is not None
    )
    if not relay_expected_active:
        relay_status = "not_configured"
        relay_available = True
        relay_collector_age = 0
        relay_collector_fresh = True
        failed_table_count = None
        max_table_lag_rows = None
        relay_lag_seconds = None
        last_success_timestamp = None
    sync = relay.get("sync", {})
    post_sync = relay.get("post_sync", {})
    relay_healthy = (
        relay_expected_active
        and service_active("nutsnews-supabase-sync-relay.timer") == 1
        and relay_status == "pass"
        and relay.get("mode") == "sync-once"
        and isinstance(sync, dict)
        and sync.get("status") == "applied"
        and isinstance(post_sync, dict)
        and post_sync.get("status") == "pass"
        and validation_complete
        and failed_table_count == 0
        and max_table_lag_rows == 0
        and relay.get("safe_metadata_only") is True
        and relay_collector_fresh
    )
    lines.extend(
        [
            "# HELP nutsnews_backend_sync_relay_expected_active Whether the reviewed deployment mode requires the sync relay to run.",
            "# TYPE nutsnews_backend_sync_relay_expected_active gauge",
            metric(
                "nutsnews_backend_sync_relay_expected_active",
                1 if relay_expected_active else 0,
            ),
            "# HELP nutsnews_backend_sync_relay_available Whether sync-relay telemetry is available.",
            "# TYPE nutsnews_backend_sync_relay_available gauge",
            metric("nutsnews_backend_sync_relay_available", 1 if relay_available else 0),
            "# HELP nutsnews_backend_sync_relay_collector_fresh Whether the sync-relay report was collected in the last ten minutes.",
            "# TYPE nutsnews_backend_sync_relay_collector_fresh gauge",
            metric("nutsnews_backend_sync_relay_collector_fresh", 1 if relay_collector_fresh else 0),
            "# HELP nutsnews_backend_sync_relay_collector_age_seconds Age of the latest sync-relay collection, or -1 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_collector_age_seconds gauge",
            metric(
                "nutsnews_backend_sync_relay_collector_age_seconds",
                relay_collector_age if relay_collector_age is not None else -1,
            ),
            "# HELP nutsnews_backend_sync_relay_healthy Whether the last sync-relay run passed all safe checks.",
            "# TYPE nutsnews_backend_sync_relay_healthy gauge",
            metric("nutsnews_backend_sync_relay_healthy", 1 if relay_healthy else 0),
            "# HELP nutsnews_backend_sync_relay_status Bounded state of the latest sync-relay report.",
            "# TYPE nutsnews_backend_sync_relay_status gauge",
            metric("nutsnews_backend_sync_relay_status", 1, {"status": relay_status}),
            "# HELP nutsnews_backend_sync_relay_lag_seconds Age of the last fully validated sync-once apply, or -1 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_lag_seconds gauge",
            metric("nutsnews_backend_sync_relay_lag_seconds", relay_lag_seconds if relay_lag_seconds is not None else -1),
            "# HELP nutsnews_backend_sync_relay_max_table_lag_rows Maximum source-to-target row-count lag across fully validated relay tables, or -1 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_max_table_lag_rows gauge",
            metric(
                "nutsnews_backend_sync_relay_max_table_lag_rows",
                max_table_lag_rows if max_table_lag_rows is not None else -1,
            ),
            "# HELP nutsnews_backend_sync_relay_failed_table_count Failed required relay tables, or -1 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_failed_table_count gauge",
            metric(
                "nutsnews_backend_sync_relay_failed_table_count",
                failed_table_count if failed_table_count is not None else -1,
            ),
            "# HELP nutsnews_backend_sync_relay_last_success_timestamp_seconds Timestamp of the last passing relay run, or 0 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_last_success_timestamp_seconds gauge",
            metric("nutsnews_backend_sync_relay_last_success_timestamp_seconds", last_success_timestamp or 0),
            "# HELP nutsnews_backend_sync_relay_last_success_age_seconds Age of the last passing relay run, or -1 when unavailable.",
            "# TYPE nutsnews_backend_sync_relay_last_success_age_seconds gauge",
            metric(
                "nutsnews_backend_sync_relay_last_success_age_seconds",
                max(0, now - last_success_timestamp) if last_success_timestamp is not None else -1,
            ),
        ]
    )

    lines.extend(durable_content_metric_lines(now))
    lines.extend(health_audit_metric_lines(now))
    lines.extend(worker_uplift_ownership_metric_lines())
    lines.extend(worker_uplift_deployed_identity_metric_lines())
    lines.extend(worker_uplift_observability_contract_metric_lines())
    lines.extend(docker_stats_metric_lines())

    return lines


def write_atomic(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        lines = collect()
    except Exception:
        # Replace prior output so a collection failure can never masquerade as fresh data.
        now = int(datetime.now(UTC).timestamp())
        lines = [
            *exporter_state_lines(now, available=False),
            *alloy_readiness_metric_lines(0),
            *worker_uplift_ownership_metric_lines(),
            *worker_uplift_deployed_identity_metric_lines(),
            *worker_uplift_observability_contract_metric_lines(),
            *docker_stats_unavailable_metric_lines(),
        ]
        write_atomic(args.output, lines)
        return 1
    write_atomic(args.output, lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
