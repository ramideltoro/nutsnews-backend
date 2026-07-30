#!/usr/bin/env python3
"""Validate worker-uplift legacy-to-shadow parity report wiring."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "worker-uplift-legacy-to-shadow-parity-report.json"
REPORT_SCRIPT = ROOT / "scripts" / "backend_worker_uplift_parity_report.py"
WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-uplift-parity-report.yml"
CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"


def main() -> int:
    errors: list[str] = []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    script = REPORT_SCRIPT.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    checks = CHECKS.read_text(encoding="utf-8")

    if manifest.get("report_id") != "worker-uplift-legacy-to-shadow-parity":
        errors.append("manifest report_id is invalid")
    if manifest.get("tracking_issue") != 122:
        errors.append("manifest must track issue 122")
    if manifest.get("comparison_window_policy", {}).get("writes_allowed") is not False:
        errors.append("parity report must be read-only")
    if manifest.get("comparison_window_policy", {}).get("production_cutover_authorized") is not False:
        errors.append("parity report must not authorize cutover")
    required_sections = {section.get("id") for section in manifest.get("required_sections", [])}
    for section in (
        "legacy_baseline_requirements",
        "stage_flow_counts",
        "translation_policy",
        "final_shadow_and_api_compatibility",
        "queue_retry_dlq_versions",
    ):
        if section not in required_sections:
            errors.append(f"missing required report section: {section}")
    for forbidden in ("select *", "article_body", "prompt", "model output", "bearer_token", "postgresql://"):
        if forbidden in script.lower():
            errors.append(f"report script contains forbidden fragment: {forbidden}")
    for required in (
        "safe_metadata_only",
        "writes_performed",
        "production_cutover_authorized",
        "legacy_ingestion_endpoints_invoked",
        "worker_uplift_parity_checks_failed",
        "current_candidate_identity",
        "immutable_packages",
        "configuration_hashes",
        "smoke_window_not_fresh",
        "legacy_single_writer_not_confirmed",
    ):
        if required not in script:
            errors.append(f"report script missing required fragment: {required}")
    for required in (
        "Backend Worker-Uplift Parity Report",
        "production-backend",
        "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
        "nutsnews_primary_shadow",
        "backend-worker-uplift-parity-report",
        "validate_worker_uplift_production_readiness.py",
        "--readiness-decision",
        "--runtime-manifest",
        "--runtime-compose",
    ):
        if required not in workflow:
            errors.append(f"parity workflow missing required fragment: {required}")
    for required in (
        "python3 scripts/validate_worker_uplift_parity_report.py",
        "python3 scripts/backend_worker_uplift_parity_report.py --offline --enforce",
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
        errors.append("offline parity report must pass with --enforce")
    else:
        report = json.loads(proc.stdout)
        if report.get("safe_metadata_only") is not True:
            errors.append("offline report must be safe metadata only")
        if report.get("production_cutover_authorized") is not False:
            errors.append("offline report must not authorize cutover")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift parity report wiring is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
