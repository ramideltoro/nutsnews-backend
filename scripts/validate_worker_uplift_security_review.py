#!/usr/bin/env python3
"""Validate the worker-uplift security review and its closure guardrails."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_PATH = ROOT / "docs" / "worker-uplift-security-review.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGES = {
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
}
CONTROL_DOMAINS = {
    "repository",
    "supply_chain",
    "secrets_and_credentials",
    "network_and_ssrf",
    "rabbitmq",
    "postgres",
    "backend_api",
    "ai",
    "telemetry",
    "operations",
}
ACCEPTANCE_CRITERIA = {
    "no_unresolved_critical_or_high",
    "packages_and_images_traceable_scanned_pinned_signed_or_attested",
    "one_service_credential_cannot_control_unrelated_systems",
    "production_gates_fail_closed",
    "legacy_and_failover_independent",
}
REPOSITORIES = {
    "ramideltoro/nutsnews-backend",
    "ramideltoro/nutsnews-worker-contracts",
    "ramideltoro/nutsnews-worker-runtime",
    "ramideltoro/nutsnews-worker-scheduler",
    "ramideltoro/nutsnews-worker-fetcher",
    "ramideltoro/nutsnews-worker-canonicalizer",
    "ramideltoro/nutsnews-worker-enrichment",
    "ramideltoro/nutsnews-worker-approval",
    "ramideltoro/nutsnews-worker-translation",
    "ramideltoro/nutsnews-worker-persistence",
    "ramideltoro/nutsnews-worker-publication",
    "ramideltoro/nutsnews-infra",
    "ramideltoro/nutsnews-docs",
}
PASS_STATUSES = {"pass", "pass_with_accepted_residual_risk"}
FINDING_STATUSES = {"remediated", "accepted_residual_risk", "not_affected"}
SEVERITIES = {"critical", "high", "medium", "low"}


def load_review(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing security review: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def duplicate_values(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def validate_review(review: dict) -> list[str]:
    errors: list[str] = []

    if review.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if review.get("tracking_issue") != "ramideltoro/nutsnews-worker#124":
        errors.append("tracking_issue must identify nutsnews-worker#124")
    if review.get("review_mode") != "non_mutating_shadow_security_review":
        errors.append("review_mode must remain non-mutating and shadow-only")
    if review.get("decision") != "pass_with_accepted_shadow_only_residual_risks":
        errors.append("decision must record the bounded shadow-only residual-risk decision")

    base = review.get("review_base", {})
    if base.get("repository") != "ramideltoro/nutsnews-backend":
        errors.append("review_base repository must be nutsnews-backend")
    if not SHA_RE.fullmatch(str(base.get("commit", ""))):
        errors.append("review_base commit must be a full Git commit SHA")

    safety = review.get("safety_invariants", {})
    required_true = {
        "legacy_worker_is_production_ingestion_owner",
        "uplift_services_are_shadow_only",
    }
    required_false = {
        "production_writes_enabled",
        "legacy_worker_modified",
        "cloudflare_or_dns_modified",
        "failover_modified",
        "cutover_state_modified",
        "production_data_mutated",
        "secret_values_retrieved_or_recorded",
    }
    for key in required_true:
        if safety.get(key) is not True:
            errors.append(f"safety_invariants.{key} must be true")
    for key in required_false:
        if safety.get(key) is not False:
            errors.append(f"safety_invariants.{key} must be false")

    repositories = review.get("repositories", [])
    repo_names = [str(item.get("name", "")) for item in repositories]
    if duplicate_values(repo_names):
        errors.append(f"duplicate repositories: {sorted(duplicate_values(repo_names))}")
    if set(repo_names) != REPOSITORIES:
        errors.append(
            "repository scope mismatch: "
            f"missing={sorted(REPOSITORIES - set(repo_names))} "
            f"extra={sorted(set(repo_names) - REPOSITORIES)}"
        )
    for item in repositories:
        name = item.get("name", "<unknown>")
        if not SHA_RE.fullmatch(str(item.get("commit", ""))):
            errors.append(f"{name} commit must be a full Git commit SHA")
        if item.get("secret_scanning_open_alerts") != 0:
            errors.append(f"{name} must have zero open secret-scanning alerts")
        if item.get("role") in {"shared_package", "service"}:
            for field in ("code_scanning_open_alerts", "dependabot_open_alerts"):
                if item.get(field) != 0:
                    errors.append(f"{name} must have zero open {field.replace('_', ' ')}")
            for field in ("ci_run", "codeql_run"):
                if not isinstance(item.get(field), int):
                    errors.append(f"{name} must record {field}")

    packages = review.get("package_artifacts", [])
    if {item.get("name") for item in packages} != {
        "@nutsnews/worker-contracts",
        "@nutsnews/worker-runtime",
    }:
        errors.append("both shared package artifacts must be reviewed")
    for item in packages:
        name = item.get("name", "<unknown>")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("tarball_sha256", ""))):
            errors.append(f"{name} must record a SHA-256 tarball digest")
        for field in ("publish_run", "attestation_id"):
            if not isinstance(item.get(field), int):
                errors.append(f"{name} must record {field}")
        for field in ("immutable", "public_package"):
            if item.get(field) is not True:
                errors.append(f"{name}.{field} must be true")

    images = review.get("deployed_images", [])
    stages = [str(item.get("stage", "")) for item in images]
    if duplicate_values(stages):
        errors.append(f"duplicate image stages: {sorted(duplicate_values(stages))}")
    if set(stages) != STAGES:
        errors.append(
            f"deployed images must cover all stages: missing={sorted(STAGES - set(stages))} "
            f"extra={sorted(set(stages) - STAGES)}"
        )
    for item in images:
        stage = item.get("stage", "<unknown>")
        if not DIGEST_RE.fullmatch(str(item.get("digest", ""))):
            errors.append(f"{stage} must record an exact image digest")
        for field in ("signed", "runtime_digest_pinned"):
            if item.get(field) is not True:
                errors.append(f"{stage}.{field} must be true")
        if item.get("secret_findings") != 0:
            errors.append(f"{stage} image must have zero secret findings")
        if not isinstance(item.get("triaged_high_or_critical_vulnerabilities"), int):
            errors.append(f"{stage} must record vulnerability triage count")

    domains = review.get("control_domains", [])
    domain_ids = [str(item.get("id", "")) for item in domains]
    if duplicate_values(domain_ids):
        errors.append(f"duplicate control domains: {sorted(duplicate_values(domain_ids))}")
    if set(domain_ids) != CONTROL_DOMAINS:
        errors.append(
            f"control domain mismatch: missing={sorted(CONTROL_DOMAINS - set(domain_ids))} "
            f"extra={sorted(set(domain_ids) - CONTROL_DOMAINS)}"
        )
    for item in domains:
        domain_id = item.get("id", "<unknown>")
        if item.get("status") not in PASS_STATUSES:
            errors.append(f"{domain_id} must pass or have only accepted residual risk")
        if not item.get("evidence"):
            errors.append(f"{domain_id} must record evidence")

    finding_ids: list[str] = []
    for finding in review.get("findings", []):
        finding_id = str(finding.get("id", ""))
        finding_ids.append(finding_id)
        severity = finding.get("severity")
        status = finding.get("status")
        if severity not in SEVERITIES:
            errors.append(f"{finding_id} has invalid severity")
        if status not in FINDING_STATUSES:
            errors.append(f"{finding_id} has invalid status")
        if severity in {"critical", "high"} and status not in {"remediated", "not_affected"}:
            errors.append(f"{finding_id} critical/high finding must be remediated or not affected")
        if status == "remediated" and not finding.get("verification"):
            errors.append(f"{finding_id} remediation must record verification")
        if status == "accepted_residual_risk":
            acceptance = finding.get("risk_acceptance", {})
            for field in ("scope", "rationale", "owner", "expires_at_gate", "required_follow_up"):
                if not acceptance.get(field):
                    errors.append(f"{finding_id} risk acceptance must record {field}")
            if acceptance.get("expires_at_gate") != "ramideltoro/nutsnews-worker#125":
                errors.append(f"{finding_id} residual acceptance must expire at issue #125")
    if duplicate_values(finding_ids):
        errors.append(f"duplicate findings: {sorted(duplicate_values(finding_ids))}")
    if not finding_ids:
        errors.append("security review must record findings and their dispositions")

    criteria = review.get("acceptance_criteria", [])
    criterion_ids = [str(item.get("id", "")) for item in criteria]
    if duplicate_values(criterion_ids):
        errors.append(f"duplicate acceptance criteria: {sorted(duplicate_values(criterion_ids))}")
    if set(criterion_ids) != ACCEPTANCE_CRITERIA:
        errors.append(
            f"acceptance criteria mismatch: missing={sorted(ACCEPTANCE_CRITERIA - set(criterion_ids))} "
            f"extra={sorted(set(criterion_ids) - ACCEPTANCE_CRITERIA)}"
        )
    for item in criteria:
        criterion_id = item.get("id", "<unknown>")
        if item.get("status") != "pass":
            errors.append(f"{criterion_id} must pass before issue closure")
        if not item.get("evidence"):
            errors.append(f"{criterion_id} must record evidence")

    if review.get("unresolved_critical_or_high_findings") != []:
        errors.append("unresolved_critical_or_high_findings must be empty")

    rollback = review.get("rollback", {})
    for field in ("review_artifacts", "workflow_hardening"):
        if not rollback.get(field):
            errors.append(f"rollback must record {field}")

    return errors


def main_args(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_PATH)
    args = parser.parse_args(argv)

    errors = validate_review(load_review(args.review))
    if errors:
        print("Worker-uplift security review validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Worker-uplift security review is complete and closure guardrails pass.")
    return 0


def main() -> int:
    return main_args(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
