#!/usr/bin/env python3
"""Validate the current #164 security dispositions and its fail-closed gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISPOSITIONS_PATH = ROOT / "docs" / "worker-uplift-security-dispositions.json"
BACKEND_CHECKS_PATH = ROOT / ".github" / "workflows" / "backend-checks.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
ISSUE_COMMENT_RE = re.compile(
    r"^https://github\.com/ramideltoro/nutsnews-worker/issues/164#issuecomment-[0-9]+$"
)
GENERIC_OWNER_NAMES = {
    "backend repository owner",
    "repository owner",
    "worker-uplift repository owner",
    "worker uplift repository owner",
    "owner",
    "maintainer",
    "automation",
    "github-actions",
    "github-actions[bot]",
}
FINDINGS = {
    "SEC-124-002": "medium",
    "SEC-124-003": "medium",
    "SEC-124-004": "medium",
    "SEC-124-005": "low",
    "SEC-124-006": "medium",
    "SEC-124-007": "low",
    "SEC-124-008": "medium",
    "SEC-124-009": "low",
}
STANDING_AUTHORIZATION_ID = "SEC-124-007-009-STANDING-2026-08-01"
STANDING_OWNER_COMMENT_SHA256 = (
    "c16828dab3ecefe4c01c5468288a575b5c16a39349af17d5806f459d6fb0d507"
)
STANDING_SCOPE_SHA256 = "917a1b18d80619c15bfac4cf8eac401a7be2eff989a1b3f7d5068fc68a44027f"
STANDING_FINDINGS = ["SEC-124-007", "SEC-124-008", "SEC-124-009"]
STANDING_FINGERPRINT_FIELDS = [
    "finding.id",
    "finding.affected_scope",
    "finding.risk_acceptance.scope",
    "finding.risk_acceptance.rationale",
    "finding.risk_acceptance.compensating_controls",
    "finding.risk_acceptance.reopen_trigger",
    "safety_invariants",
    "standing_authorization.excluded_authorities",
]
STANDING_EXCLUDED_AUTHORITIES = {
    "cutover",
    "production writes",
    "DNS changes",
    "failover changes",
    "legacy-worker changes",
    "ingestion-ownership changes",
    "production infrastructure mutation",
    "secret-value retrieval",
    "#125 production-readiness approval",
    "#166 final cutover-execution approval",
    "#127 cutover execution",
}
ALLOWED_STATUSES = {"pending", "remediated", "accepted_residual_risk"}
ALLOWED_EVIDENCE_KINDS = {
    "pull_request_merge",
    "workflow_run",
    "deployment_run",
    "artifact",
    "source_commit",
}
FORBIDDEN_VALUE_KEYS = {
    "value",
    "secret_value",
    "token_value",
    "password",
    "private_key",
    "connection_string",
    "authorization_header",
    "credential_value",
    "sql",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing security dispositions: {path}") from None
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


def standing_scope_payload(document: dict[str, Any]) -> dict[str, Any]:
    authorization = document.get("standing_authorization", {})
    finding_map = {
        str(finding.get("id", "")): finding for finding in document.get("findings", [])
    }
    scoped_findings: list[dict[str, Any]] = []
    for finding_id in authorization.get("applies_to_findings", []):
        finding = finding_map.get(str(finding_id), {})
        acceptance = finding.get("risk_acceptance") or {}
        scoped_findings.append(
            {
                "id": finding.get("id"),
                "affected_scope": finding.get("affected_scope"),
                "risk_scope": acceptance.get("scope"),
                "rationale": acceptance.get("rationale"),
                "compensating_controls": acceptance.get("compensating_controls"),
                "reopen_trigger": acceptance.get("reopen_trigger"),
            }
        )
    return {
        "finding_ids": authorization.get("applies_to_findings"),
        "findings": scoped_findings,
        "safety_invariants": document.get("safety_invariants"),
        "excluded_authorities": authorization.get("excluded_authorities"),
    }


def standing_scope_sha256(document: dict[str, Any]) -> str:
    payload = json.dumps(
        standing_scope_payload(document),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_date(value: object, label: str, errors: list[str]) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        errors.append(f"{label} must be an ISO date")
        return None


def parse_datetime(value: object, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an ISO timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed


def validate_named_login(
    value: object,
    label: str,
    authorized_logins: set[str],
    errors: list[str],
) -> str:
    login = str(value or "").strip()
    if not LOGIN_RE.fullmatch(login):
        errors.append(f"{label} must be a named GitHub login")
    if login.lower() in GENERIC_OWNER_NAMES or login.endswith("[bot]"):
        errors.append(f"{label} must not be anonymous, generic, or automation")
    if login not in authorized_logins:
        errors.append(f"{label} must be in authorized_owner_logins")
    return login


def validate_source_evidence(finding_id: str, evidence: dict, errors: list[str]) -> None:
    heads = evidence.get("source_heads", [])
    if not isinstance(heads, list) or not heads:
        errors.append(f"{finding_id} must record immutable source heads")
        return
    repositories: list[str] = []
    for index, source in enumerate(heads):
        label = f"{finding_id}.current_evidence.source_heads[{index}]"
        repository = str(source.get("repository", ""))
        repositories.append(repository)
        if not repository.startswith("ramideltoro/"):
            errors.append(f"{label}.repository must be a ramideltoro repository")
        if not SHA_RE.fullmatch(str(source.get("commit", ""))):
            errors.append(f"{label}.commit must be a full Git SHA")
        paths = source.get("paths", [])
        if not isinstance(paths, list) or not paths or any(not str(path) for path in paths):
            errors.append(f"{label}.paths must record affected source paths")
    if duplicate_values(repositories):
        errors.append(f"{finding_id} contains duplicate source repositories")
    if not evidence.get("observation"):
        errors.append(f"{finding_id} must record the current observation")
    file_hashes = evidence.get("file_sha256", {})
    if not isinstance(file_hashes, dict):
        errors.append(f"{finding_id}.current_evidence.file_sha256 must be an object")
    else:
        for path, digest in file_hashes.items():
            if not path or not SHA256_RE.fullmatch(str(digest)):
                errors.append(f"{finding_id} file evidence must use SHA-256 hashes")


def validate_immutable_evidence(
    finding_id: str,
    evidence: object,
    *,
    require_deployment: bool,
    errors: list[str],
) -> None:
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{finding_id} remediation must record immutable evidence")
        return
    kinds: set[str] = set()
    for index, item in enumerate(evidence):
        label = f"{finding_id}.remediation.immutable_evidence[{index}]"
        kind = str(item.get("kind", ""))
        kinds.add(kind)
        if kind not in ALLOWED_EVIDENCE_KINDS:
            errors.append(f"{label}.kind is invalid")
        if not str(item.get("url", "")).startswith("https://github.com/ramideltoro/"):
            errors.append(f"{label}.url must link immutable GitHub evidence")
        if not SHA_RE.fullmatch(str(item.get("head_commit", ""))):
            errors.append(f"{label}.head_commit must be a full Git SHA")
        if kind in {"workflow_run", "deployment_run"}:
            if not isinstance(item.get("run_id"), int):
                errors.append(f"{label}.run_id must be an integer")
            if item.get("conclusion") != "success":
                errors.append(f"{label}.conclusion must be success")
        if kind == "artifact":
            if not isinstance(item.get("artifact_id"), int):
                errors.append(f"{label}.artifact_id must be an integer")
            if not re.fullmatch(r"^sha256:[0-9a-f]{64}$", str(item.get("artifact_digest", ""))):
                errors.append(f"{label}.artifact_digest must be a SHA-256 digest")
    if "pull_request_merge" not in kinds:
        errors.append(f"{finding_id} remediation must include pull-request merge evidence")
    if "workflow_run" not in kinds:
        errors.append(f"{finding_id} remediation must include successful CI evidence")
    if require_deployment and "deployment_run" not in kinds:
        errors.append(f"{finding_id} runtime/configuration remediation requires deployment proof")


def validate_remediation(finding_id: str, finding: dict, errors: list[str]) -> None:
    remediation = finding.get("remediation")
    if not isinstance(remediation, dict):
        errors.append(f"{finding_id} remediated status requires remediation evidence")
        return
    for field in ("summary", "verification", "rollback"):
        if not remediation.get(field):
            errors.append(f"{finding_id} remediation must record {field}")
    deployment_required = remediation.get("deployment_required")
    if not isinstance(deployment_required, bool):
        errors.append(f"{finding_id} remediation.deployment_required must be boolean")
        deployment_required = False
    validate_immutable_evidence(
        finding_id,
        remediation.get("immutable_evidence"),
        require_deployment=deployment_required,
        errors=errors,
    )
    if finding.get("risk_acceptance") is not None:
        errors.append(f"{finding_id} remediated status must not carry risk acceptance")
    if finding.get("owner_action_required") is not False:
        errors.append(f"{finding_id} remediated status must clear owner_action_required")


def validate_risk_acceptance(
    finding_id: str,
    finding: dict,
    *,
    authorized_logins: set[str],
    today: date,
    standing_authorization: dict[str, Any],
    errors: list[str],
) -> None:
    acceptance = finding.get("risk_acceptance")
    if not isinstance(acceptance, dict):
        errors.append(f"{finding_id} accepted residual risk requires an explicit acceptance")
        return
    owner = validate_named_login(
        acceptance.get("authorized_owner_login"),
        f"{finding_id}.risk_acceptance.authorized_owner_login",
        authorized_logins,
        errors,
    )
    for field in ("scope", "rationale", "reopen_trigger"):
        if not acceptance.get(field):
            errors.append(f"{finding_id} risk acceptance must record {field}")
    controls = acceptance.get("compensating_controls", [])
    if not isinstance(controls, list) or not controls or any(not str(item) for item in controls):
        errors.append(f"{finding_id} risk acceptance must record compensating controls")
    decided_at = parse_datetime(
        acceptance.get("decided_at_utc"),
        f"{finding_id}.risk_acceptance.decided_at_utc",
        errors,
    )
    if decided_at is not None and decided_at > datetime.now(timezone.utc):
        errors.append(f"{finding_id} risk acceptance cannot be future-dated")
    expires_on = parse_date(
        acceptance.get("expires_on"),
        f"{finding_id}.risk_acceptance.expires_on",
        errors,
    )
    review_on = parse_date(
        acceptance.get("review_on"),
        f"{finding_id}.risk_acceptance.review_on",
        errors,
    )
    if expires_on is not None and expires_on <= today:
        errors.append(f"{finding_id} risk acceptance is expired")
    if review_on is not None and review_on < today:
        errors.append(f"{finding_id} risk acceptance review date is expired")
    if expires_on is not None and review_on is not None and review_on > expires_on:
        errors.append(f"{finding_id} risk acceptance review must not follow expiry")
    if review_on is not None and finding.get("review_by") != review_on.isoformat():
        errors.append(f"{finding_id} review_by must match the accepted-risk review date")

    evidence = acceptance.get("acceptance_evidence")
    if not isinstance(evidence, dict):
        errors.append(f"{finding_id} risk acceptance must record explicit owner evidence")
    else:
        kind = evidence.get("kind")
        if kind == "issue_comment":
            author = validate_named_login(
                evidence.get("author_login"),
                f"{finding_id}.risk_acceptance.acceptance_evidence.author_login",
                authorized_logins,
                errors,
            )
            if author != owner:
                errors.append(f"{finding_id} acceptance evidence author must match authorized owner")
            if not ISSUE_COMMENT_RE.fullmatch(str(evidence.get("url", ""))):
                errors.append(f"{finding_id} acceptance must link an explicit #164 issue comment")
            if parse_datetime(
                evidence.get("authored_at_utc"),
                f"{finding_id}.risk_acceptance.acceptance_evidence.authored_at_utc",
                errors,
            ) is None:
                pass
        elif kind == "standing_authorization":
            if review_on is not None and review_on > today + timedelta(days=31):
                errors.append(
                    f"{finding_id} risk acceptance review exceeds the 31-day standing limit"
                )
            if expires_on is not None and expires_on > today + timedelta(days=61):
                errors.append(
                    f"{finding_id} risk acceptance expiry exceeds the 61-day standing limit"
                )
            if finding_id not in STANDING_FINDINGS:
                errors.append(f"{finding_id} is outside the standing authorization finding set")
            if evidence.get("authorization_id") != standing_authorization.get("id"):
                errors.append(f"{finding_id} must reference the active standing authorization")
            if evidence.get("initial_owner_evidence_url") != standing_authorization.get(
                "owner_evidence", {}
            ).get("url"):
                errors.append(f"{finding_id} standing evidence must match the owner comment")
            author = validate_named_login(
                evidence.get("author_login"),
                f"{finding_id}.risk_acceptance.acceptance_evidence.author_login",
                authorized_logins,
                errors,
            )
            if author != owner or author != standing_authorization.get(
                "authorized_owner_login"
            ):
                errors.append(
                    f"{finding_id} standing evidence author must match the authorized owner"
                )
            if parse_datetime(
                evidence.get("authored_at_utc"),
                f"{finding_id}.risk_acceptance.acceptance_evidence.authored_at_utc",
                errors,
            ) != parse_datetime(
                standing_authorization.get("owner_evidence", {}).get("authored_at_utc"),
                "standing_authorization.owner_evidence.authored_at_utc",
                errors,
            ):
                errors.append(f"{finding_id} standing evidence timestamp must match owner evidence")
        elif kind == "signed_artifact":
            if not str(evidence.get("path", "")).startswith("docs/"):
                errors.append(f"{finding_id} signed acceptance artifact must be source controlled")
            if not SHA256_RE.fullmatch(str(evidence.get("sha256", ""))):
                errors.append(f"{finding_id} signed acceptance artifact must record SHA-256")
            signer = validate_named_login(
                evidence.get("signer_login"),
                f"{finding_id}.risk_acceptance.acceptance_evidence.signer_login",
                authorized_logins,
                errors,
            )
            if signer != owner:
                errors.append(f"{finding_id} acceptance signer must match authorized owner")
        else:
            errors.append(
                f"{finding_id} acceptance evidence must be an owner issue comment, standing "
                "authorization, or signed artifact"
            )
    if finding.get("remediation") is not None:
        errors.append(f"{finding_id} accepted residual risk must not claim remediation")
    if finding.get("owner_action_required") is not False:
        errors.append(f"{finding_id} accepted residual risk must clear owner_action_required")


def validate_value_free(label: str, value: object, errors: list[str], path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_VALUE_KEYS:
                errors.append(f"{label} contains forbidden value-bearing key: {key_path}")
            validate_value_free(label, child, errors, key_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_value_free(label, child, errors, f"{path}[{index}]")
    elif isinstance(value, str):
        lower = value.lower()
        if any(scheme in lower for scheme in ("postgres://", "postgresql://", "amqp://", "amqps://")):
            errors.append(f"{label} contains a connection string at {path}")
        if "authorization: bearer " in lower:
            errors.append(f"{label} contains an authorization value at {path}")


def validate_dispositions(
    document: dict[str, Any],
    *,
    today: date | None = None,
    enforce_closure: bool = False,
    backend_checks_text: str | None = None,
) -> list[str]:
    errors: list[str] = []
    effective_today = today or datetime.now(timezone.utc).date()

    if document.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if document.get("tracking_issue") != "ramideltoro/nutsnews-worker#164":
        errors.append("tracking_issue must identify nutsnews-worker#164")
    if document.get("implementation_repository") != "ramideltoro/nutsnews-backend":
        errors.append("implementation_repository must be nutsnews-backend")
    parse_datetime(document.get("captured_at_utc"), "captured_at_utc", errors)

    if backend_checks_text is None:
        try:
            backend_checks_text = BACKEND_CHECKS_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            backend_checks_text = ""
    closure_workflow_command = (
        "run: python3 scripts/validate_worker_uplift_security_dispositions.py "
        "--enforce-closure"
    )
    if closure_workflow_command not in backend_checks_text:
        errors.append("Backend Checks must enforce security disposition closure")

    source_review = document.get("source_review", {})
    if source_review.get("tracking_issue") != "ramideltoro/nutsnews-worker#124":
        errors.append("source review must identify nutsnews-worker#124")
    if source_review.get("artifact") != "docs/worker-uplift-security-review.json":
        errors.append("source review must link the committed #124 artifact")
    if not SHA256_RE.fullmatch(str(source_review.get("artifact_sha256", ""))):
        errors.append("source review artifact must record SHA-256")
    if not SHA_RE.fullmatch(str(source_review.get("merge_commit", ""))):
        errors.append("source review must record an immutable merge commit")
    if source_review.get("historical_acceptances_expired_at_gate") != (
        "ramideltoro/nutsnews-worker#125"
    ):
        errors.append("historical residual acceptances must expire at #125")
    if source_review.get("historical_acceptance_is_current_disposition") is not False:
        errors.append("historical acceptance must not satisfy #164")

    authorized = document.get("authorized_owner_logins", [])
    if not isinstance(authorized, list) or not authorized:
        errors.append("authorized_owner_logins must name at least one owner")
        authorized_logins: set[str] = set()
    else:
        authorized_logins = {str(item) for item in authorized}
        if len(authorized_logins) != len(authorized):
            errors.append("authorized_owner_logins must not contain duplicates")
        for login in authorized:
            validate_named_login(login, "authorized_owner_logins", authorized_logins, errors)

    safety = document.get("safety_invariants", {})
    for field in ("legacy_worker_is_production_ingestion_owner", "uplift_services_are_shadow_only"):
        if safety.get(field) is not True:
            errors.append(f"safety_invariants.{field} must be true")
    for field in (
        "production_writes_enabled",
        "cutover_authorized",
        "legacy_worker_modified",
        "ingestion_ownership_changed",
        "dns_or_failover_modified",
        "production_infrastructure_modified",
        "secret_values_retrieved_or_recorded",
    ):
        if safety.get(field) is not False:
            errors.append(f"safety_invariants.{field} must be false")

    policy = document.get("disposition_policy", {})
    if set(policy.get("allowed_decisions", [])) != {"remediated", "accepted_residual_risk"}:
        errors.append("disposition policy must allow only remediation or explicit residual acceptance")
    for field in (
        "pending_is_a_valid_current_record_but_not_a_disposition",
        "remediation_requires_separately_reviewed_repository_pr",
        "remediation_requires_immutable_merge_and_successful_ci_evidence",
        "deployment_proof_required_when_runtime_or_deployed_configuration_changes",
        "residual_acceptance_requires_explicit_named_owner_evidence",
        "no_disposition_authorizes_cutover_or_production_writes",
    ):
        if policy.get(field) is not True:
            errors.append(f"disposition_policy.{field} must be true")
    for field in (
        "issue_closure_is_acceptance",
        "advisory_approval_metadata_is_acceptance",
        "automation_is_acceptance",
        "expired_acceptance_is_valid",
    ):
        if policy.get(field) is not False:
            errors.append(f"disposition_policy.{field} must be false")

    for field in (
        "standing_authorization_may_replace_per_release_owner_comment",
        "standing_authorization_requires_exact_scope_fingerprint",
        "standing_authorization_survives_release_revisions_only",
        "standing_authorization_requires_protected_source_change_and_current_evidence",
        "standing_authorization_invalid_on_scope_or_invariant_change",
    ):
        if policy.get(field) is not True:
            errors.append(f"disposition_policy.{field} must be true")
    if policy.get("final_readiness_or_cutover_approval_is_covered") is not False:
        errors.append(
            "disposition_policy.final_readiness_or_cutover_approval_is_covered must be false"
        )

    standing_authorization = document.get("standing_authorization", {})
    if standing_authorization.get("id") != STANDING_AUTHORIZATION_ID:
        errors.append("standing authorization id is invalid")
    if standing_authorization.get("status") != "active":
        errors.append("standing authorization must be active")
    standing_owner = validate_named_login(
        standing_authorization.get("authorized_owner_login"),
        "standing_authorization.authorized_owner_login",
        authorized_logins,
        errors,
    )
    authorized_at = parse_datetime(
        standing_authorization.get("authorized_at_utc"),
        "standing_authorization.authorized_at_utc",
        errors,
    )
    if authorized_at is not None and authorized_at > datetime.now(timezone.utc):
        errors.append("standing authorization cannot be future-dated")
    owner_evidence = standing_authorization.get("owner_evidence", {})
    if owner_evidence.get("kind") != "issue_comment":
        errors.append("standing authorization requires an explicit owner issue comment")
    if not ISSUE_COMMENT_RE.fullmatch(str(owner_evidence.get("url", ""))):
        errors.append("standing authorization must link an explicit #164 issue comment")
    if owner_evidence.get("body_sha256") != STANDING_OWNER_COMMENT_SHA256:
        errors.append("standing authorization owner comment digest is not authorized")
    evidence_owner = validate_named_login(
        owner_evidence.get("author_login"),
        "standing_authorization.owner_evidence.author_login",
        authorized_logins,
        errors,
    )
    evidence_at = parse_datetime(
        owner_evidence.get("authored_at_utc"),
        "standing_authorization.owner_evidence.authored_at_utc",
        errors,
    )
    if evidence_owner != standing_owner:
        errors.append("standing authorization evidence author must match its owner")
    if authorized_at is not None and evidence_at != authorized_at:
        errors.append("standing authorization timestamp must match its owner evidence")
    if standing_authorization.get("applies_to_findings") != STANDING_FINDINGS:
        errors.append("standing authorization must cover exactly SEC-124-007 through SEC-124-009")
    for field in (
        "applies_to_current_and_future_release_revisions",
    ):
        if standing_authorization.get(field) is not True:
            errors.append(f"standing_authorization.{field} must be true")
    for field in (
        "per_release_owner_approval_required",
        "first_run_owner_approval_required",
        "review_refresh_requires_new_owner_approval",
    ):
        if standing_authorization.get(field) is not False:
            errors.append(f"standing_authorization.{field} must be false")
    if standing_authorization.get("scope_fingerprint_fields") != STANDING_FINGERPRINT_FIELDS:
        errors.append("standing authorization fingerprint field set is invalid")
    if set(standing_authorization.get("excluded_authorities", [])) != (
        STANDING_EXCLUDED_AUTHORITIES
    ):
        errors.append("standing authorization excluded authority set is invalid")
    if duplicate_values(standing_authorization.get("excluded_authorities", [])):
        errors.append("standing authorization excluded authorities must not contain duplicates")
    if not standing_authorization.get("invalidated_by"):
        errors.append("standing authorization must record invalidation triggers")
    revalidation = standing_authorization.get("revalidation", {})
    for field in (
        "value_free",
        "current_evidence_required",
        "protected_pull_request_required",
        "review_and_expiry_dates_remain_fail_closed",
    ):
        if revalidation.get(field) is not True:
            errors.append(f"standing_authorization.revalidation.{field} must be true")
    if revalidation.get("validator") != (
        "python3 scripts/validate_worker_uplift_security_dispositions.py --enforce-closure"
    ):
        errors.append("standing authorization must name the fail-closed validator")
    if revalidation.get("focused_tests") != (
        "python3 -m unittest tests.test_worker_uplift_security_dispositions"
    ):
        errors.append("standing authorization must name the focused tests")
    scope_digest = standing_authorization.get("scope_sha256")
    if not SHA256_RE.fullmatch(str(scope_digest)):
        errors.append("standing authorization scope_sha256 must be a SHA-256 digest")
    elif scope_digest != STANDING_SCOPE_SHA256:
        errors.append("standing authorization scope fingerprint is not owner-authorized")
    elif scope_digest != standing_scope_sha256(document):
        errors.append("standing authorization scope fingerprint does not match current scope")

    findings = document.get("findings", [])
    finding_ids = [str(item.get("id", "")) for item in findings]
    if duplicate_values(finding_ids):
        errors.append(f"duplicate findings: {sorted(duplicate_values(finding_ids))}")
    if set(finding_ids) != set(FINDINGS):
        errors.append(
            f"finding scope mismatch: missing={sorted(set(FINDINGS) - set(finding_ids))} "
            f"extra={sorted(set(finding_ids) - set(FINDINGS))}"
        )

    pending_ids: set[str] = set()
    completed_ids: set[str] = set()
    named_owner_decisions: list[dict[str, str]] = []
    for finding in findings:
        finding_id = str(finding.get("id", ""))
        if finding.get("severity") != FINDINGS.get(finding_id):
            errors.append(f"{finding_id} severity must match the source review")
        if not finding.get("title"):
            errors.append(f"{finding_id} must record a title")
        scope = finding.get("affected_scope", [])
        if not isinstance(scope, list) or not scope or any(
            not str(repo).startswith("ramideltoro/") for repo in scope
        ):
            errors.append(f"{finding_id} must record affected repository scope")
        validate_source_evidence(finding_id, finding.get("current_evidence", {}), errors)
        owner = finding.get("accountable_owner", {})
        validate_named_login(
            owner.get("login"),
            f"{finding_id}.accountable_owner.login",
            authorized_logins,
            errors,
        )
        if not owner.get("basis"):
            errors.append(f"{finding_id} accountable owner must record basis")
        if owner.get("acceptance_inferred") is not False:
            errors.append(f"{finding_id} must not infer acceptance from ownership")
        if not finding.get("planned_remediation"):
            errors.append(f"{finding_id} must record a remediation path")
        controls = finding.get("compensating_controls", [])
        if not isinstance(controls, list) or not controls or any(not str(item) for item in controls):
            errors.append(f"{finding_id} must record current compensating controls")
        review_by = parse_date(finding.get("review_by"), f"{finding_id}.review_by", errors)
        if review_by is not None and review_by < effective_today:
            errors.append(f"{finding_id} current disposition review date is expired")
        if not finding.get("reopen_trigger"):
            errors.append(f"{finding_id} must record a reopen trigger")

        status = finding.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{finding_id} has invalid status")
        elif status == "pending":
            pending_ids.add(finding_id)
            if finding.get("owner_action_required") is not True:
                errors.append(f"{finding_id} pending status must require owner action")
            if finding.get("remediation") is not None or finding.get("risk_acceptance") is not None:
                errors.append(f"{finding_id} pending status must not fabricate a disposition")
        elif status == "remediated":
            completed_ids.add(finding_id)
            validate_remediation(finding_id, finding, errors)
        elif status == "accepted_residual_risk":
            completed_ids.add(finding_id)
            validate_risk_acceptance(
                finding_id,
                finding,
                authorized_logins=authorized_logins,
                today=effective_today,
                standing_authorization=standing_authorization,
                errors=errors,
            )
            acceptance = finding.get("risk_acceptance", {})
            named_owner_decisions.append(
                {
                    "finding": finding_id,
                    "owner": str(acceptance.get("authorized_owner_login", "")),
                }
            )

    closure = document.get("closure_gate", {})
    if set(closure.get("unresolved_findings", [])) != pending_ids:
        errors.append("closure_gate.unresolved_findings must exactly match pending findings")
    if closure.get("named_owner_decisions") != named_owner_decisions:
        errors.append("closure_gate.named_owner_decisions must exactly match accepted residual risks")
    ready = not pending_ids and completed_ids == set(FINDINGS)
    if closure.get("ready") is not ready:
        errors.append("closure_gate.ready must reflect complete non-pending dispositions")
    expected_status = "pass" if ready else "blocked"
    if closure.get("status") != expected_status:
        errors.append(f"closure_gate.status must be {expected_status}")
    if closure.get("issue_closure_authorized") is not ready:
        errors.append("closure_gate.issue_closure_authorized must equal closure readiness")
    if closure.get("owner_action_required") is not bool(pending_ids):
        errors.append("closure_gate.owner_action_required must reflect pending findings")
    if closure.get("production_readiness_effect") != (
        "ramideltoro/nutsnews-worker#125 remains NO-GO"
    ):
        errors.append("#164 disposition artifact must not change #125 NO-GO")

    validation = document.get("validation", {})
    if validation.get("current_record") != (
        "python3 scripts/validate_worker_uplift_security_dispositions.py"
    ):
        errors.append("validation.current_record must name the disposition validator")
    if validation.get("closure") != (
        "python3 scripts/validate_worker_uplift_security_dispositions.py --enforce-closure"
    ):
        errors.append("validation.closure must name the fail-closed closure mode")
    if validation.get("focused_tests") != (
        "python3 -m unittest tests.test_worker_uplift_security_dispositions"
    ):
        errors.append("validation.focused_tests must name the focused tests")

    if document.get("decision") != ("complete" if ready else "blocked_pending_dispositions"):
        errors.append("top-level decision must reflect closure readiness")
    if enforce_closure and not ready:
        errors.append(
            "closure enforcement failed: every SEC-124-002 through SEC-124-009 finding "
            "must be remediated or explicitly accepted by a named authorized owner"
        )

    validate_value_free("security dispositions", document, errors)
    return errors


def main_args(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispositions", type=Path, default=DEFAULT_DISPOSITIONS_PATH)
    parser.add_argument("--enforce-closure", action="store_true")
    args = parser.parse_args(argv)

    document = load_json(args.dispositions)
    errors = validate_dispositions(
        document,
        enforce_closure=args.enforce_closure,
    )
    if errors:
        print("Worker-uplift security disposition validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if document.get("closure_gate", {}).get("ready") is True:
        print("Worker-uplift security dispositions are structurally valid and complete.")
    else:
        print(
            "Worker-uplift security dispositions are structurally valid; "
            "unresolved findings remain fail-closed."
        )
    return 0


def main() -> int:
    return main_args(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
