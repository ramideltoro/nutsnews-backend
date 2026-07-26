#!/usr/bin/env python3
"""Validate worker-uplift soak/capacity report wiring."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "worker-uplift-shadow-soak-capacity-report.json"
REPORT_SCRIPT = ROOT / "scripts" / "backend_worker_uplift_soak_report.py"
WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-uplift-soak-report.yml"
CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"


def main() -> int:
    errors: list[str] = []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    script = REPORT_SCRIPT.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.exists() else ""
    checks = CHECKS.read_text(encoding="utf-8")

    if manifest.get("report_id") != "worker-uplift-shadow-soak-capacity":
        errors.append("manifest report_id is invalid")
    if manifest.get("tracking_issue") != 123:
        errors.append("manifest must track issue 123")
    policy = manifest.get("observation_window_policy", {})
    if policy.get("writes_allowed") is not False:
        errors.append("soak report must be read-only")
    if policy.get("production_cutover_authorized") is not False:
        errors.append("soak report must not authorize cutover")
    if int(policy.get("minimum_hours", 0) or 0) < 48:
        errors.append("soak report must require at least 48 hours")
    budgets = manifest.get("approved_budgets", {})
    for budget in (
        "max_stage_pending_outbox_age_seconds",
        "max_failed_shadow_api_requests",
        "max_worker_uplift_active_owner_rows",
        "max_openai_shadow_records",
        "max_dlq_growth_per_smoke",
        "max_missing_consumers",
        "max_root_disk_used_percent",
        "max_memory_used_percent",
        "max_load_per_vcpu",
        "max_failed_systemd_units",
        "max_queue_messages_ready",
        "max_queue_messages_unacknowledged",
    ):
        if budget not in budgets:
            errors.append(f"missing approved budget: {budget}")
    tuned = manifest.get("tuned_runtime_values", {})
    for stage in ("scheduler", "fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"):
        if stage not in tuned:
            errors.append(f"missing tuned runtime values for {stage}")
    required_sections = {section.get("id") for section in manifest.get("required_sections", [])}
    for section in (
        "observation_window",
        "runtime_guardrails",
        "queue_and_dlq_headroom",
        "stage_error_and_retry_pressure",
        "ai_cost_and_qwen_saturation",
        "host_and_telemetry_headroom",
        "source_controlled_tuning",
    ):
        if section not in required_sections:
            errors.append(f"missing required report section: {section}")
    for forbidden in ("select *", "article_body", "prompt text", "model output", "bearer_token"):
        if forbidden in script.lower():
            errors.append(f"report script contains forbidden fragment: {forbidden}")
    for required in (
        "safe_metadata_only",
        "writes_performed",
        "production_cutover_authorized",
        "insufficient_window",
        "worker_uplift_soak_checks_failed",
        "worker_uplift_soak_window_incomplete",
    ):
        if required not in script:
            errors.append(f"report script missing required fragment: {required}")
    for required in (
        "Backend Worker-Uplift Soak Capacity Report",
        "production-backend",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "nutsnews_primary_shadow",
        "backend-worker-uplift-soak-report",
        "--require-window",
        "runtime-status.json",
        "chown",
    ):
        if required not in workflow:
            errors.append(f"soak workflow missing required fragment: {required}")
    for required in (
        "python3 scripts/validate_worker_uplift_soak_report.py",
        "python3 scripts/backend_worker_uplift_soak_report.py --offline --enforce",
    ):
        if required not in checks:
            errors.append(f"backend checks missing required command: {required}")

    proc = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), "--offline", "--enforce"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        errors.append("offline soak report must pass with --enforce")
    else:
        report = json.loads(proc.stdout)
        if report.get("safe_metadata_only") is not True:
            errors.append("offline report must be safe metadata only")
        if report.get("writes_performed") is not False:
            errors.append("offline report must not perform writes")
        if report.get("production_cutover_authorized") is not False:
            errors.append("offline report must not authorize cutover")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift soak/capacity report wiring is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
