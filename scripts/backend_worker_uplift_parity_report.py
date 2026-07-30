#!/usr/bin/env python3
"""Build a read-only worker-uplift legacy-to-shadow parity report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "worker-uplift-legacy-to-shadow-parity-report.json"
DEFAULT_READINESS_DECISION = ROOT / "docs" / "worker-uplift-production-readiness-decision.json"
DEFAULT_SMOKE_REPORT = Path("/var/lib/nutsnews/worker-uplift-runtime/reports/last-smoke.json")
DEFAULT_RUNTIME_MANIFEST = Path("/etc/nutsnews-worker-uplift/services.json")
DEFAULT_RUNTIME_COMPOSE = Path("/opt/nutsnews-worker-uplift/compose.yml")
MAX_SMOKE_AGE_SECONDS = 24 * 60 * 60
EXPECTED_SERVICES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
STAGES = ("fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication")
STAGE_SCHEMAS = {
    "fetcher": "worker_uplift_fetcher",
    "canonicalizer": "worker_uplift_canonicalizer",
    "enrichment": "worker_uplift_enrichment",
    "approval": "worker_uplift_approval",
    "translation": "worker_uplift_translation",
    "persistence": "worker_uplift_persistence",
    "publication": "worker_uplift_publication",
}
TARGET_LANGUAGES = ("fr", "ja", "de-CH", "de", "el")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
            timeout=45,
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
        return int(values.get(key, "0"))
    except ValueError:
        return 0


def stage_flow_query() -> str:
    selects: list[str] = []
    for stage, schema in STAGE_SCHEMAS.items():
        selects.extend(
            [
                f"select '{stage}_processed_inbox=' || count(*)::text from {schema}.inbox where status in ('processed', 'duplicate');",
                f"select '{stage}_failed_inbox=' || count(*)::text from {schema}.inbox where status in ('failed', 'parked');",
                f"select '{stage}_pending_outbox=' || count(*)::text from {schema}.outbox where status in ('pending', 'retrying', 'published') and confirmed_at is null;",
                f"select '{stage}_confirmed_outbox=' || count(*)::text from {schema}.outbox where status = 'confirmed' or confirmed_at is not null;",
            ]
        )
    return "\n".join(selects)


def final_shadow_query() -> str:
    return """
select 'final_shadow_aggregates=' || count(*)::text from worker_uplift_final.article_shadow_aggregates;
select 'ready_final_shadow_aggregates=' || count(*)::text from worker_uplift_final.article_shadow_aggregates where publication_status in ('ready', 'shadow_only');
select 'api_shadow_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where provider_mode = 'backend_postgres_shadow';
select 'failed_api_receipts=' || count(*)::text from worker_uplift_final.api_command_receipts where status = 'rejected';
select 'stage_health_rows=' || count(*)::text from worker_uplift_final.stage_health_projections;
select 'active_ingestion_owner_legacy_shards=' || count(*)::text from worker_uplift_final.stage_health_projections where active_ingestion_owner = 'legacy_shards';
select 'active_ingestion_owner_worker_uplift=' || count(*)::text from worker_uplift_final.stage_health_projections where active_ingestion_owner = 'worker_uplift';
"""


def policy_query() -> str:
    return """
select 'approval_approved=' || count(*)::text from worker_uplift_approval.approval_decisions where decision = 'approved';
select 'approval_rejected=' || count(*)::text from worker_uplift_approval.approval_decisions where decision = 'rejected';
select 'translation_accepted=' || count(*)::text from worker_uplift_translation.translation_records where quality_status = 'accepted';
select 'translation_distinct_languages=' || count(distinct language_code)::text from worker_uplift_translation.translation_records where quality_status = 'accepted';
select 'publication_ready=' || count(*)::text from worker_uplift_publication.publication_readiness where status = 'ready';
select 'publication_shadow_comparisons=' || count(*)::text from worker_uplift_publication.publication_decisions where backend_api_operation = 'shadow-publication-comparison';
"""


def feed_query() -> str:
    return """
select 'fetch_versions=' || count(*)::text from worker_uplift_fetcher.fetch_versions;
select 'feed_health_projections=' || count(*)::text from worker_uplift_fetcher.feed_health_projections;
select 'article_identities=' || count(*)::text from worker_uplift_canonicalizer.article_identities;
select 'enrichment_records=' || count(*)::text from worker_uplift_enrichment.enrichment_records;
"""


QUERY_CATALOG = {
    "stage_flow_counts": stage_flow_query,
    "final_shadow_and_api_compatibility": final_shadow_query,
    "translation_policy": policy_query,
    "legacy_baseline_requirements": feed_query,
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


def load_smoke_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def smoke_summary(smoke_report: dict[str, Any] | None) -> dict[str, Any]:
    if not smoke_report:
        return {"status": "missing", "reason": "smoke report not found"}
    smoke = smoke_report.get("smoke", {})
    if not isinstance(smoke, dict):
        return {"status": "invalid", "reason": "smoke section missing"}
    queues_after = smoke.get("queues_after", {})
    consumers: dict[str, int] = {}
    if isinstance(queues_after, dict):
        for stage, queue_list in queues_after.items():
            if isinstance(queue_list, list) and queue_list and isinstance(queue_list[0], dict):
                metrics = queue_list[0].get("metrics", {})
                if isinstance(metrics, dict):
                    consumers[stage] = int(metrics.get("consumers") or 0)
    idempotency = smoke.get("idempotency", {})
    db_checks = smoke.get("db_checks", {})
    health = smoke.get("health", {})
    health_statuses: dict[str, str] = {}
    if isinstance(health, dict):
        for service, value in health.items():
            if isinstance(value, dict):
                health_statuses[service] = str(value.get("status", "unknown"))
    fixture_hits = smoke.get("fixture_hits", {})
    return {
        "status": smoke_report.get("status"),
        "generated_at_utc": smoke_report.get("generated_at_utc"),
        "contract": smoke.get("contract"),
        "trigger": smoke.get("trigger"),
        "fixture_id": smoke.get("fixture", {}).get("fixture_id") if isinstance(smoke.get("fixture"), dict) else None,
        "missing_consumers": smoke.get("missing_consumers", []),
        "dlq_growth": smoke.get("dlq_growth", {}),
        "legacy_ingestion_endpoints_invoked": smoke.get("legacy_ingestion_endpoints_invoked"),
        "queue_consumers_after": consumers,
        "fixture_hits": fixture_hits if isinstance(fixture_hits, dict) else {},
        "db_checks": db_checks if isinstance(db_checks, dict) else {},
        "health_statuses": health_statuses,
        "versions": smoke.get("versions", {}),
        "idempotency": {
            "expected_single_final_shadow_result": idempotency.get("expected_single_final_shadow_result") if isinstance(idempotency, dict) else None,
            "duplicate_publish_idempotency_key": idempotency.get("duplicate_publish_idempotency_key") if isinstance(idempotency, dict) else None,
        },
    }


def candidate_summary(
    decision: dict[str, Any],
    runtime_manifest: dict[str, Any],
    runtime_manifest_path: Path,
    runtime_compose_path: Path,
    smoke: dict[str, Any],
    checked_at: datetime,
) -> tuple[dict[str, Any], list[str]]:
    mismatches: list[str] = []
    expected = {
        str(item.get("stage")): item
        for item in decision.get("deployed_services", [])
        if isinstance(item, dict)
    }
    deployed = {
        str(item.get("name")): item
        for item in runtime_manifest.get("services", [])
        if isinstance(item, dict)
    }
    smoke_versions = smoke.get("versions", {})
    if not isinstance(smoke_versions, dict):
        smoke_versions = {}

    service_evidence: list[dict[str, Any]] = []
    for name in EXPECTED_SERVICES:
        expected_service = expected.get(name)
        deployed_service = deployed.get(name)
        smoke_service = smoke_versions.get(name)
        if not isinstance(expected_service, dict):
            mismatches.append(f"{name}.candidate_missing")
            continue
        if not isinstance(deployed_service, dict):
            mismatches.append(f"{name}.deployed_manifest_missing")
            continue
        if not isinstance(smoke_service, dict):
            mismatches.append(f"{name}.smoke_version_missing")
            smoke_service = {}

        comparisons = {
            "image": (
                expected_service.get("image"),
                deployed_service.get("image"),
                smoke_service.get("image"),
            ),
            "contract_version": (
                expected_service.get("contract_version"),
                deployed_service.get("contract_version"),
                smoke_service.get("contract_version"),
            ),
            "runtime_package_version": (
                expected_service.get("runtime_package_version"),
                deployed_service.get("runtime_package_version"),
                smoke_service.get("runtime_package_version"),
            ),
            "mode": (
                expected_service.get("mode"),
                deployed_service.get("runtime_mode"),
                smoke_service.get("runtime_mode"),
            ),
        }
        for field, values in comparisons.items():
            if len(set(values)) != 1:
                mismatches.append(f"{name}.{field}_mismatch")
        if expected_service.get("source_commit") != deployed_service.get("image_tag"):
            mismatches.append(f"{name}.source_commit_mismatch")
        provenance = deployed_service.get("provenance", {})
        if not isinstance(provenance, dict) or (
            expected_service.get("source_repository") != provenance.get("source_repository")
        ):
            mismatches.append(f"{name}.source_repository_mismatch")
        postgres = deployed_service.get("postgres", {})
        if not isinstance(postgres, dict) or postgres.get("production_write_path") is not False:
            mismatches.append(f"{name}.production_write_path_not_false")
        if expected_service.get("production_writes_enabled") is not False:
            mismatches.append(f"{name}.candidate_production_writes_not_false")

        service_evidence.append(
            {
                "stage": name,
                "source_repository": expected_service.get("source_repository"),
                "source_commit": expected_service.get("source_commit"),
                "image": expected_service.get("image"),
                "contract_version": expected_service.get("contract_version"),
                "runtime_package_version": expected_service.get("runtime_package_version"),
                "mode": expected_service.get("mode"),
                "production_writes_enabled": False,
                "matches_deployed_manifest": all(
                    not mismatch.startswith(f"{name}.") for mismatch in mismatches
                ),
            }
        )

    safety = decision.get("safety_invariants", {})
    if not isinstance(safety, dict):
        safety = {}
    if safety.get("legacy_worker_is_production_ingestion_owner") is not True:
        mismatches.append("legacy_single_writer_not_confirmed")
    if runtime_manifest.get("mode") != "shadow":
        mismatches.append("runtime_mode_not_shadow")
    if runtime_manifest.get("production_writes_enabled") is not False:
        mismatches.append("runtime_production_writes_not_false")
    backend_api = runtime_manifest.get("backend_api", {})
    if not isinstance(backend_api, dict) or backend_api.get("writes_enabled") is not False:
        mismatches.append("runtime_backend_api_writes_not_false")

    smoke_time = parse_utc(smoke.get("generated_at_utc"))
    smoke_age_seconds = (
        int((checked_at - smoke_time).total_seconds()) if smoke_time is not None else None
    )
    smoke_fresh = (
        smoke_age_seconds is not None
        and 0 <= smoke_age_seconds <= MAX_SMOKE_AGE_SECONDS
    )
    if not smoke_fresh:
        mismatches.append("smoke_window_not_fresh")

    source_hashes = [
        item
        for item in decision.get("source_control_hashes", [])
        if isinstance(item, dict)
    ]
    source_commit = os.environ.get("GITHUB_SHA") or None
    return (
        {
            "status": "pass" if not mismatches else "fail",
            "tracking_issue": "ramideltoro/nutsnews-worker#158",
            "workflow_source_commit": source_commit,
            "source_hash_validation": {
                "status": "pass" if source_commit else "not_run_in_github",
                "validator": "scripts/validate_worker_uplift_production_readiness.py",
                "source_commit": source_commit,
            },
            "services": service_evidence,
            "immutable_packages": decision.get("immutable_packages", []),
            "configuration_hashes": {
                "source_control": source_hashes,
                "deployed_runtime_manifest": {
                    "path": "/etc/nutsnews-worker-uplift/services.json",
                    "sha256": sha256_file(runtime_manifest_path),
                },
                "deployed_compose": {
                    "path": "/opt/nutsnews-worker-uplift/compose.yml",
                    "sha256": sha256_file(runtime_compose_path),
                },
            },
            "comparison_window": {
                "kind": "latest_protected_scheduler_shadow_smoke_fixture",
                "fixture_id": smoke.get("fixture_id"),
                "start_utc": smoke.get("generated_at_utc"),
                "end_utc": checked_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "max_smoke_age_seconds": MAX_SMOKE_AGE_SECONDS,
                "smoke_age_seconds": smoke_age_seconds,
                "fresh": smoke_fresh,
            },
            "single_writer": {
                "owner": "legacy_shards",
                "repository": safety.get("active_single_writer_repository"),
                "commit": safety.get("active_single_writer_commit"),
                "uplift_mode": runtime_manifest.get("mode"),
                "production_writes_enabled": runtime_manifest.get("production_writes_enabled"),
                "backend_api_writes_enabled": backend_api.get("writes_enabled")
                if isinstance(backend_api, dict)
                else None,
            },
            "mismatches": mismatches,
            "safe_metadata_only": True,
        },
        mismatches,
    )


def comparison_results(
    manifest: dict[str, Any],
    checks: list[dict[str, Any]],
    smoke: dict[str, Any],
    candidate_mismatches: list[str],
) -> dict[str, Any]:
    by_id = {str(check.get("id")): check for check in checks}
    stage_values = by_id.get("stage_flow_counts", {}).get("values", {})
    final_values = by_id.get("final_shadow_and_api_compatibility", {}).get(
        "values", {}
    )
    smoke_db = smoke.get("db_checks", {})
    api_audit = smoke_db.get("api_audit", {}) if isinstance(smoke_db, dict) else {}
    failed_checks = [
        str(check.get("id"))
        for check in checks
        if check.get("status") not in {"pass", "skipped_with_reason"}
    ]
    dlq_growth = smoke.get("dlq_growth", {})
    return {
        "article_counts": {
            key: parse_int(final_values, key)
            for key in (
                "final_shadow_aggregates",
                "ready_final_shadow_aggregates",
                "api_shadow_receipts",
                "failed_api_receipts",
            )
        },
        "stage_counts": stage_values if isinstance(stage_values, dict) else {},
        "mismatches": {
            "candidate": candidate_mismatches,
            "failed_required_checks": failed_checks,
            "count": len(candidate_mismatches) + len(failed_checks),
        },
        "exclusions": manifest.get("intentional_differences", []),
        "error_budget": {
            "policy": manifest.get("approved_tolerances", {}),
            "observed": {
                "failed_required_checks": len(failed_checks),
                "dlq_growth_total": sum(
                    int(value or 0)
                    for value in dlq_growth.values()
                )
                if isinstance(dlq_growth, dict)
                else None,
                "failed_shadow_api_requests": int(
                    api_audit.get("failed_api_requests", 0) or 0
                )
                if isinstance(api_audit, dict)
                else None,
            },
            "within_budget": not candidate_mismatches and not failed_checks,
        },
        "guardrails": {
            "writes_performed": False,
            "production_cutover_authorized": False,
            "legacy_ingestion_endpoints_invoked": smoke.get(
                "legacy_ingestion_endpoints_invoked"
            ),
            "safe_metadata_only": True,
        },
    }


def check_status(check_id: str, values: dict[str, str], smoke: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if check_id == "legacy_baseline_requirements":
        fixture_hits = smoke.get("fixture_hits", {})
        if smoke.get("status") != "pass":
            reasons.append("latest_smoke_not_pass")
        if smoke.get("contract") != "scheduler-feed-to-final-shadow-v1":
            reasons.append("unexpected_smoke_contract")
        if smoke.get("trigger") != "scheduler-compatible-feed-fetch-request":
            reasons.append("unexpected_smoke_trigger")
        if not isinstance(fixture_hits, dict) or int(fixture_hits.get("feed", 0) or 0) < 1:
            reasons.append("feed_fixture_hit_missing")
        if not isinstance(fixture_hits, dict) or int(fixture_hits.get("article", 0) or 0) < 1:
            reasons.append("article_fixture_hit_missing")
    elif check_id == "stage_flow_counts":
        consumers = smoke.get("queue_consumers_after", {})
        health = smoke.get("health_statuses", {})
        db_checks = smoke.get("db_checks", {})
        if smoke.get("status") != "pass":
            reasons.append("latest_smoke_not_pass")
        if not isinstance(consumers, dict) or any(int(consumers.get(stage, 0) or 0) < 1 for stage in STAGES):
            reasons.append("not_all_stage_consumers_active")
        if not isinstance(health, dict) or any(health.get(stage) != "healthy" for stage in ("fetcher", "canonicalizer", "enrichment", *STAGES[3:])):
            reasons.append("not_all_stage_health_checks_healthy")
        if not isinstance(db_checks, dict):
            reasons.append("smoke_db_checks_missing")
        else:
            expected = {
                ("approval", "processed_inbox"): "1",
                ("translation", "processed_inbox"): "1",
                ("translation", "distinct_languages"): str(len(TARGET_LANGUAGES)),
                ("persistence", "final_shadow_aggregate"): "1",
                ("publication", "publication_readiness"): "1",
                ("publication", "publication_shadow_comparison"): "1",
                ("api_audit", "failed_api_requests"): "0",
            }
            for (section, key), expected_value in expected.items():
                section_values = db_checks.get(section, {})
                if not isinstance(section_values, dict) or str(section_values.get(key)) != expected_value:
                    reasons.append(f"smoke_{section}_{key}_unexpected")
    elif check_id == "translation_policy":
        if parse_int(values, "approval_approved") < 1:
            reasons.append("approval_approved_missing")
        if parse_int(values, "translation_distinct_languages") < len(TARGET_LANGUAGES):
            reasons.append("translation_language_coverage_missing")
        if parse_int(values, "publication_ready") < 1:
            reasons.append("publication_ready_missing")
        if parse_int(values, "publication_shadow_comparisons") < 1:
            reasons.append("publication_shadow_comparison_missing")
    elif check_id == "final_shadow_and_api_compatibility":
        if parse_int(values, "final_shadow_aggregates") < 1:
            reasons.append("final_shadow_aggregate_missing")
        if parse_int(values, "api_shadow_receipts") < 1:
            reasons.append("shadow_api_receipt_missing")
        if parse_int(values, "failed_api_receipts") > 0:
            reasons.append("failed_api_receipts_nonzero")
        if parse_int(values, "active_ingestion_owner_worker_uplift") > 0:
            reasons.append("worker_uplift_marked_active_owner_before_cutover")
    elif check_id == "queue_retry_dlq_versions":
        if smoke.get("status") != "pass":
            reasons.append("latest_smoke_not_pass")
        if smoke.get("missing_consumers"):
            reasons.append("smoke_missing_consumers")
        if smoke.get("dlq_growth"):
            reasons.append("smoke_dlq_growth_nonzero")
        if smoke.get("legacy_ingestion_endpoints_invoked") is not False:
            reasons.append("legacy_ingestion_invocation_not_false")
        consumers = smoke.get("queue_consumers_after", {})
        if not isinstance(consumers, dict) or any(int(consumers.get(stage, 0) or 0) < 1 for stage in STAGES):
            reasons.append("not_all_stage_consumers_active")
    return ("pass" if not reasons else "fail", reasons)


def build_checks(manifest: dict[str, Any], sections: dict[str, dict[str, str]], smoke: dict[str, Any], query_errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query_error_by_id = {item["id"]: item for item in query_errors}
    checks: list[dict[str, Any]] = []
    for section in manifest.get("required_sections", []):
        check_id = str(section.get("id", ""))
        if check_id in query_error_by_id:
            checks.append({**section, **query_error_by_id[check_id]})
            continue
        if check_id == "queue_retry_dlq_versions":
            values: dict[str, str] = {}
        else:
            values = sections.get(check_id, {})
        status, reasons = check_status(check_id, values, smoke)
        checks.append({
            **section,
            "status": status,
            "reasons": reasons,
            "values": values,
        })
    return checks


def write_report(report: dict[str, Any], output: str) -> None:
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--readiness-decision", default=str(DEFAULT_READINESS_DECISION))
    parser.add_argument("--db-url-env", default="NUTSNEWS_WORKER_UPLIFT_PARITY_DB_URL")
    parser.add_argument("--smoke-report", default=str(DEFAULT_SMOKE_REPORT))
    parser.add_argument("--runtime-manifest", default=str(DEFAULT_RUNTIME_MANIFEST))
    parser.add_argument("--runtime-compose", default=str(DEFAULT_RUNTIME_COMPOSE))
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    readiness_decision_path = Path(args.readiness_decision)
    runtime_manifest_path = Path(args.runtime_manifest)
    runtime_compose_path = Path(args.runtime_compose)
    db_url = os.environ.get(args.db_url_env, "").strip()
    smoke = smoke_summary(load_smoke_report(Path(args.smoke_report)))
    checked_at = utc_now()

    report: dict[str, Any] = {
        "status": "pass",
        "checked_at_utc": checked_at,
        "report_id": manifest.get("report_id"),
        "manifest": safe_manifest_path(manifest_path),
        "manifest_version": manifest.get("version"),
        "tracking_issue": manifest.get("tracking_issue"),
        "safe_metadata_only": True,
        "source_labels": manifest.get("source_labels", {}),
        "comparison_window_policy": manifest.get("comparison_window_policy", {}),
        "writes_performed": False,
        "production_cutover_authorized": False,
        "db_url_env": args.db_url_env,
        "db_url_present": bool(db_url),
        "smoke_report_present": Path(args.smoke_report).exists(),
        "smoke": smoke,
        "checks": [],
        "errors": [],
    }

    if args.offline:
        report["status"] = "skipped"
        report["reason"] = "offline mode"
        report["checks"] = [
            {
                **section,
                "status": "skipped_with_reason",
                "reason": "offline mode",
            }
            for section in manifest.get("required_sections", [])
        ]
        write_report(report, args.output)
        return 0

    if not db_url:
        report["status"] = "blocked"
        report["reason"] = f"missing database URL env {args.db_url_env}"
        report["errors"].append("missing_worker_uplift_parity_db_url")
        write_report(report, args.output)
        return 1 if args.enforce else 0

    missing_candidate_inputs = [
        str(path)
        for path in (
            readiness_decision_path,
            runtime_manifest_path,
            runtime_compose_path,
        )
        if not path.is_file()
    ]
    if missing_candidate_inputs:
        report["status"] = "blocked"
        report["reason"] = "current candidate evidence inputs are missing"
        report["errors"].append("missing_current_candidate_evidence_inputs")
        report["missing_candidate_inputs"] = missing_candidate_inputs
        write_report(report, args.output)
        return 1 if args.enforce else 0

    sections, query_errors = run_report_queries(db_url)
    checks = build_checks(manifest, sections, smoke, query_errors)
    checked_at_dt = parse_utc(checked_at) or datetime.now(UTC)
    candidate, candidate_mismatches = candidate_summary(
        load_json(readiness_decision_path),
        load_json(runtime_manifest_path),
        runtime_manifest_path,
        runtime_compose_path,
        smoke,
        checked_at_dt,
    )
    checks.append(
        {
            "id": "current_candidate_identity",
            "category": "candidate_identity",
            "sensitivity": "safe_runtime_metadata_only",
            "status": candidate["status"],
            "reasons": candidate_mismatches,
            "values": {
                "expected_services": len(EXPECTED_SERVICES),
                "matched_services": sum(
                    1
                    for service in candidate["services"]
                    if service.get("matches_deployed_manifest") is True
                ),
                "fresh_smoke_window": candidate["comparison_window"]["fresh"],
            },
        }
    )
    failed = [check["id"] for check in checks if check.get("status") == "fail"]
    blocked = [check["id"] for check in checks if check.get("status") == "blocked"]
    report["checks"] = checks
    report["current_candidate"] = candidate
    report["comparison_results"] = comparison_results(
        manifest, checks, smoke, candidate_mismatches
    )
    report["failed_checks"] = failed
    report["blocked_checks"] = blocked
    report["status"] = "fail" if failed else ("blocked" if blocked else "pass")
    if failed:
        report["errors"].append("worker_uplift_parity_checks_failed")
    if blocked:
        report["errors"].append("worker_uplift_parity_checks_blocked")
    write_report(report, args.output)
    return 1 if args.enforce and report["status"] != "pass" else 0


if __name__ == "__main__":
    sys.exit(main())
