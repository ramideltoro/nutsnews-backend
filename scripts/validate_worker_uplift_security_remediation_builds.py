#!/usr/bin/env python3
"""Validate immutable #164 build, image, SBOM, and provenance evidence."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_PATH = ROOT / "docs" / "worker-uplift-security-remediation-builds.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PACKAGE_REPOSITORIES = {
    "ramideltoro/nutsnews-worker-contracts",
    "ramideltoro/nutsnews-worker-runtime",
}
SERVICE_REPOSITORIES = {
    "scheduler": "ramideltoro/nutsnews-worker-feed-scheduler",
    "fetcher": "ramideltoro/nutsnews-worker-feed-fetcher",
    "canonicalizer": "ramideltoro/nutsnews-worker-article-canonicalizer",
    "enrichment": "ramideltoro/nutsnews-worker-article-enrichment",
    "approval": "ramideltoro/nutsnews-worker-article-approval",
    "translation": "ramideltoro/nutsnews-worker-article-translation",
    "persistence": "ramideltoro/nutsnews-worker-article-persistence",
    "publication": "ramideltoro/nutsnews-worker-article-publication",
}
REQUIRED_PACKAGE_CONTROLS = {
    "immutable_action_commits",
    "no_artifact_publish_dependency_cache_restore",
    "fail_closed_security_baseline_validator",
}


def load_json(path: Path = DEFAULT_EVIDENCE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if document.get("tracking_issue") != "ramideltoro/nutsnews-worker#164":
        errors.append("tracking_issue must identify nutsnews-worker#164")
    if document.get("evidence_scope") != "immutable_issue_164_security_remediation_build_and_deployment":
        errors.append("evidence_scope must preserve the immutable #164 security remediation proof")
    try:
        captured = datetime.fromisoformat(str(document.get("captured_at_utc", "")).replace("Z", "+00:00"))
        if captured.tzinfo is None:
            errors.append("captured_at_utc must include a timezone")
    except ValueError:
        errors.append("captured_at_utc must be an ISO timestamp")

    safety = document.get("safety", {})
    for field in ("legacy_worker_is_production_ingestion_owner", "uplift_services_are_shadow_only"):
        if safety.get(field) is not True:
            errors.append(f"safety.{field} must be true")
    for field in (
        "production_writes_enabled",
        "cutover_authorized",
        "dns_or_failover_changed",
        "secret_values_recorded",
    ):
        if safety.get(field) is not False:
            errors.append(f"safety.{field} must be false")

    packages = document.get("package_repositories", [])
    package_names = {str(item.get("repository", "")) for item in packages}
    if package_names != PACKAGE_REPOSITORIES or len(packages) != len(PACKAGE_REPOSITORIES):
        errors.append("package repository evidence must cover contracts and runtime exactly once")
    for package in packages:
        label = str(package.get("repository", "<unknown>"))
        if not SHA_RE.fullmatch(str(package.get("merge_commit", ""))):
            errors.append(f"{label} merge_commit must be a full Git SHA")
        for field in ("pull_request", "pr_ci_run", "post_merge_ci_run"):
            if not positive_integer(package.get(field)):
                errors.append(f"{label}.{field} must be a positive integer")
        if set(package.get("controls", [])) != REQUIRED_PACKAGE_CONTROLS:
            errors.append(f"{label} must record the complete package security controls")

    images = document.get("service_images", [])
    stages = {str(item.get("stage", "")) for item in images}
    if stages != set(SERVICE_REPOSITORIES) or len(images) != len(SERVICE_REPOSITORIES):
        errors.append("service image evidence must cover all eight stages exactly once")
    for image in images:
        stage = str(image.get("stage", "<unknown>"))
        expected_repository = SERVICE_REPOSITORIES.get(stage)
        repository = str(image.get("repository", ""))
        if repository != expected_repository:
            errors.append(f"{stage} repository does not match the declared owner")
        source_commit = str(image.get("source_commit", ""))
        if not SHA_RE.fullmatch(source_commit):
            errors.append(f"{stage}.source_commit must be a full Git SHA")
        for field in ("pull_request", "pr_ci_run", "post_merge_ci_run", "publish_run"):
            if not positive_integer(image.get(field)):
                errors.append(f"{stage}.{field} must be a positive integer")
        image_reference = str(image.get("image", ""))
        expected_prefix = f"ghcr.io/{repository}@"
        if not image_reference.startswith(expected_prefix):
            errors.append(f"{stage}.image must use its declared GHCR repository")
        image_digest = image_reference.rsplit("@", maxsplit=1)[-1]
        for field, value in (
            ("image", image_digest),
            ("attestation_manifest_digest", image.get("attestation_manifest_digest")),
            ("sbom_digest", image.get("sbom_digest")),
            ("provenance_digest", image.get("provenance_digest")),
        ):
            if not DIGEST_RE.fullmatch(str(value or "")):
                errors.append(f"{stage}.{field} must be a SHA-256 digest")
        if image.get("minimal_runtime_validated") is not True:
            errors.append(f"{stage} must prove minimal runtime validation")
        if image.get("signed") is not True:
            errors.append(f"{stage} image must be signed")

    fetcher = next((item for item in images if item.get("stage") == "fetcher"), {})
    if fetcher.get("dns_resolution_bound_to_connect") is not True:
        errors.append("fetcher must prove DNS resolution-to-connect binding")
    if fetcher.get("dns_binding_fail_closed_tested") is not True:
        errors.append("fetcher must prove fail-closed DNS binding tests")

    deployment = document.get("protected_shadow_deployment", {})
    if not SHA_RE.fullmatch(str(deployment.get("backend_merge_commit", ""))):
        errors.append("protected deployment backend_merge_commit must be a full Git SHA")
    for field in ("protected_check_run", "protected_apply_run", "status_run"):
        if not positive_integer(deployment.get(field)):
            errors.append(f"protected_shadow_deployment.{field} must be a positive integer")

    for field in ("protected_check_artifact", "protected_apply_artifact", "status_artifact"):
        artifact = deployment.get(field, {})
        if not positive_integer(artifact.get("artifact_id")):
            errors.append(f"protected_shadow_deployment.{field}.artifact_id must be positive")
        if not DIGEST_RE.fullmatch(str(artifact.get("artifact_digest", ""))):
            errors.append(f"protected_shadow_deployment.{field}.artifact_digest must be SHA-256")
    apply_artifact = deployment.get("protected_apply_artifact", {})
    for field in ("pre_report_sha256", "post_report_sha256"):
        if not re.fullmatch(r"^[0-9a-f]{64}$", str(apply_artifact.get(field, ""))):
            errors.append(f"protected apply {field} must be SHA-256")
    for field in ("pre_status", "post_status"):
        if apply_artifact.get(field) != "pass":
            errors.append(f"protected apply {field} must be pass")
    for field in ("pre_blockers", "post_blockers"):
        if apply_artifact.get(field) != []:
            errors.append(f"protected apply {field} must be empty")
    status_artifact = deployment.get("status_artifact", {})
    if not re.fullmatch(r"^[0-9a-f]{64}$", str(status_artifact.get("report_sha256", ""))):
        errors.append("protected status report_sha256 must be SHA-256")

    deploy_runs = deployment.get("service_deploy_runs", {})
    if set(deploy_runs) != set(SERVICE_REPOSITORIES):
        errors.append("protected deploy runs must cover all eight stages exactly once")
    for stage, run_id in deploy_runs.items():
        if not positive_integer(run_id):
            errors.append(f"{stage} protected deploy run must be a positive integer")

    deployed_images = deployment.get("deployed_images", [])
    deployed_by_stage = {str(item.get("stage", "")): item for item in deployed_images}
    if set(deployed_by_stage) != set(SERVICE_REPOSITORIES) or len(deployed_images) != len(
        SERVICE_REPOSITORIES
    ):
        errors.append("deployed image evidence must cover all eight stages exactly once")
    source_by_stage = {str(item.get("stage", "")): item for item in images}
    for stage, deployed in deployed_by_stage.items():
        source = source_by_stage.get(stage, {})
        if deployed.get("source_commit") != source.get("source_commit"):
            errors.append(f"{stage} deployed source commit does not match the built candidate")
        if deployed.get("image") != source.get("image"):
            errors.append(f"{stage} deployed image does not match the built candidate")

    if deployment.get("runtime_status") != "pass":
        errors.append("protected runtime status must pass")
    if deployment.get("mode") != "shadow":
        errors.append("protected runtime mode must remain shadow")
    if deployment.get("production_writes_enabled") is not False:
        errors.append("protected runtime production writes must remain disabled")
    if deployment.get("healthy_service_count") != 8:
        errors.append("protected runtime must prove eight healthy services")
    if deployment.get("required_consumer_queue_count") != 7:
        errors.append("protected runtime must prove seven consumer queues")
    for field in ("missing_consumers", "unverifiable_consumers"):
        if deployment.get(field) != []:
            errors.append(f"protected runtime {field} must be empty")
    if deployment.get("queue_messages_total") != 0:
        errors.append("protected runtime queues must be drained")

    deployment_safety = deployment.get("safety", {})
    if deployment_safety.get("legacy_worker_is_production_ingestion_owner") is not True:
        errors.append("protected deployment must preserve legacy ingestion ownership")
    for field in (
        "cutover_authorized",
        "dns_or_failover_changed",
        "queue_mutation_performed",
        "production_infrastructure_changed",
    ):
        if deployment_safety.get(field) is not False:
            errors.append(f"protected deployment safety.{field} must be false")

    return errors


def main() -> int:
    errors = validate(load_json())
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("worker-uplift #164 build and attestation evidence is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
