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
DEFAULT_RUNTIME_DEFAULTS_PATH = ROOT / "ansible" / "roles" / "backend_worker_runtime" / "defaults" / "main.yml"
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


def validate(document: dict[str, Any], defaults_text: str | None = None) -> list[str]:
    errors: list[str] = []
    runtime_defaults = (
        DEFAULT_RUNTIME_DEFAULTS_PATH.read_text(encoding="utf-8")
        if defaults_text is None
        else defaults_text
    )

    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if document.get("tracking_issue") != "ramideltoro/nutsnews-worker#164":
        errors.append("tracking_issue must identify nutsnews-worker#164")
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
        if f"image: {image_reference}" not in runtime_defaults:
            errors.append(f"{stage} image is not the source-controlled runtime candidate")
        if f"image_tag: {source_commit}" not in runtime_defaults:
            errors.append(f"{stage} source commit is not the source-controlled runtime candidate")
        if f"subject_digest: {image_digest}" not in runtime_defaults:
            errors.append(f"{stage} provenance subject does not match the image digest")

    fetcher = next((item for item in images if item.get("stage") == "fetcher"), {})
    if fetcher.get("dns_resolution_bound_to_connect") is not True:
        errors.append("fetcher must prove DNS resolution-to-connect binding")
    if fetcher.get("dns_binding_fail_closed_tested") is not True:
        errors.append("fetcher must prove fail-closed DNS binding tests")

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
