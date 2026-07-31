#!/usr/bin/env python3
"""Validate the protected, isolated worker-uplift dependency drill contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-dependency-outage-drills.yml"
REPORT_BUILDER = ROOT / "scripts/backend_worker_uplift_dependency_outage_report.py"
FIXTURE_DIR = ROOT / "tests/fixtures/worker_uplift_dependency_drills"
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
SCENARIOS = {
    "postgresql_unavailable",
    "backend_api_unavailable",
    "qwen_unavailable",
}
ALERT_UIDS = {
    "nn-wu-rmq-dlq-nonempty",
    "nn-wu-rmq-retry-redelivery",
    "nn-wu-slo-retry-dlq-burn",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def repository_errors() -> list[str]:
    errors: list[str] = []
    workflow = WORKFLOW.read_text(encoding="utf-8")
    builder = REPORT_BUILDER.read_text(encoding="utf-8")

    required_workflow_fragments = (
        "confirm_scope:",
        'worker-uplift-shadow-dependency-drills',
        "environment: production-backend",
        "repository: ramideltoro/nutsnews-worker-article-approval",
        "ref: 38185c74717221453c662f4ba1b093315eff5b83",
        "repository: ramideltoro/nutsnews-worker-article-persistence",
        "ref: 6dae69d96b4b081b23b5e1d85e10133ee36a6674",
        "repository: ramideltoro/nutsnews-infra",
        "ref: b10a76cf523c9f5b47bd69b8301dc3d9d9a4d8a6",
        "postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
        "sudo -n /usr/local/sbin/nutsnews-worker-runtime status",
        "curl --fail --silent --show-error --max-time 10 http://127.0.0.1:${port}/ready",
        "--pre-dependency-readiness",
        "--post-dependency-readiness",
        "--dry-run",
        "backend_worker_uplift_dependency_outage_report.py",
        "validate_worker_uplift_dependency_outage_drills.py",
        "backend-worker-uplift-dependency-outage-drills",
    )
    for fragment in required_workflow_fragments:
        if fragment not in workflow:
            errors.append(f"workflow is missing required fragment: {fragment}")

    if workflow.count("sudo -n /usr/local/sbin/nutsnews-worker-runtime status") != 2:
        errors.append("workflow must bracket drills with exactly two read-only live status calls")
    for forbidden in (
        "nutsnews-worker-runtime deploy",
        "nutsnews-worker-runtime promote",
        "nutsnews-worker-runtime restart",
        "nutsnews-worker-runtime scale",
        "nutsnews-worker-runtime rollback",
        "nutsnews-worker-runtime dlq-replay",
        "nutsnews-worker-runtime drain",
        "nutsnews-worker-runtime reconciliation",
        "--confirm-action",
    ):
        if forbidden in workflow:
            errors.append(f"workflow contains a forbidden runtime mutation: {forbidden}")
    package_block = workflow.split("Package immutable safe evidence", maxsplit=1)[-1]
    package_block = package_block.split("Upload immutable safe evidence", maxsplit=1)[0]
    for raw_status in ("pre-status.json", "post-status.json"):
        if raw_status in package_block:
            errors.append(f"raw private-host status must not be retained: {raw_status}")
    for raw_readiness in (
        "pre-approval-readiness-raw.json",
        "pre-persistence-readiness-raw.json",
        "post-approval-readiness-raw.json",
        "post-persistence-readiness-raw.json",
    ):
        if raw_readiness in package_block:
            errors.append(
                f"raw dependency readiness body must not be retained: {raw_readiness}"
            )

    fixture_contracts = {
        "postgres_probe.test.ts": "PostgresPersistenceInboxStore",
        "backend_api_probe.test.ts": "HttpPersistenceBackendWorkerApiClient",
        "qwen_probe.test.ts": "LocalAiApprovalQwenClient",
    }
    for name, implementation in fixture_contracts.items():
        text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        for fragment in (
            implementation,
            "failure_detected:",
            "recovered:",
            "endpoint_recorded: false",
            "credential_value_recorded: false",
            "toBeLessThan(2_000)",
        ):
            if fragment not in text:
                errors.append(f"{name} is missing adapter drill contract: {fragment}")
        for forbidden in (
            "process.env.LOCAL_AI",
            "process.env.POSTGRES",
            "process.env.BACKEND",
            "console.log",
        ):
            if forbidden in text:
                errors.append(f"{name} may expose runtime configuration: {forbidden}")

    for fragment in (
        '"tracking_issue": "ramideltoro/nutsnews-worker#161"',
        '"production_host_dependency_interrupted": False',
        '"production_data_mutated": False',
        '"uplift_production_writes_enabled": False',
        '"legacy_ingestion_changed": False',
        '"dns_or_failover_changed": False',
        '"cutover_performed": False',
        '"credential_values_recorded": False',
        '"private_host_data_recorded": False',
        '"issue": "ramideltoro/nutsnews-worker#168"',
    ):
        if fragment not in builder:
            errors.append(f"report builder is missing safety/evidence contract: {fragment}")

    return errors


def recursive_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for child in value.values()
            for nested in recursive_keys(child)
        }
    if isinstance(value, list):
        return {
            nested
            for child in value
            for nested in recursive_keys(child)
        }
    return set()


def report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != 1:
        errors.append("report schema_version must be 1")
    if report.get("tracking_issue") != "ramideltoro/nutsnews-worker#161":
        errors.append("report tracking issue mismatch")
    if report.get("implementation_repository") != "ramideltoro/nutsnews-backend":
        errors.append("report implementation repository mismatch")
    if report.get("status") != "pass" or report.get("errors") != []:
        errors.append("report must pass without errors")
    if report.get("mode") != "protected_isolated_current_candidate":
        errors.append("report mode mismatch")
    if not isinstance(report.get("workflow_run_id"), int) or report["workflow_run_id"] <= 0:
        errors.append("report workflow_run_id must be positive")
    if not SHA_RE.fullmatch(str(report.get("workflow_commit", ""))):
        errors.append("report workflow_commit must be immutable")

    safety = report.get("safety", {})
    false_safety_fields = (
        "production_host_dependency_interrupted",
        "production_data_mutated",
        "uplift_production_writes_enabled",
        "legacy_ingestion_changed",
        "dns_or_failover_changed",
        "cutover_performed",
        "credential_values_recorded",
        "private_host_data_recorded",
    )
    for field in false_safety_fields:
        if safety.get(field) is not False:
            errors.append(f"safety.{field} must be false")

    input_policy = report.get("input_policy", {})
    if input_policy.get("bounded_synthetic_inputs_per_scenario") != 1:
        errors.append("each scenario must use one bounded synthetic input")
    for field in ("payloads_recorded", "endpoints_recorded", "model_inputs_recorded"):
        if input_policy.get(field) is not False:
            errors.append(f"input_policy.{field} must be false")

    delivery = report.get("delivery_policy", {})
    expected_delivery = {
        "max_attempts": 4,
        "manual_ack_required": True,
        "retry_transfer_requires_confirm_before_ack": True,
        "dlq_transfer_requires_confirm_before_ack": True,
    }
    for field, expected in expected_delivery.items():
        if delivery.get(field) != expected:
            errors.append(f"delivery_policy.{field} mismatch")

    candidate = report.get("candidate", {})
    if not HASH_RE.fullmatch(str(candidate.get("runtime_defaults_sha256", ""))):
        errors.append("candidate runtime defaults hash is missing")
    if not HASH_RE.fullmatch(str(candidate.get("topology_sha256", ""))):
        errors.append("candidate topology hash is missing")
    services = candidate.get("services", [])
    if {item.get("service") for item in services} != STAGES:
        errors.append("candidate service set mismatch")
    for item in services:
        service = item.get("service")
        if not DIGEST_RE.fullmatch(str(item.get("image_digest", ""))):
            errors.append(f"candidate image is not digest-pinned: {service}")
        if not SHA_RE.fullmatch(str(item.get("source_commit", ""))):
            errors.append(f"candidate source is not immutable: {service}")
        if item.get("mode") != "shadow":
            errors.append(f"candidate is not shadow-only: {service}")
    checkouts = candidate.get("tested_source_checkouts", {})
    if set(checkouts) != {"approval", "persistence", "infra"}:
        errors.append("tested source checkout evidence is incomplete")
    for name, commit in checkouts.items():
        if not SHA_RE.fullmatch(str(commit)):
            errors.append(f"tested checkout is not immutable: {name}")

    scenarios = report.get("scenarios", [])
    if {item.get("scenario") for item in scenarios} != SCENARIOS:
        errors.append("outage scenario set mismatch")
    expected_assertions = {
        "postgresql_unavailable": 2,
        "backend_api_unavailable": 3,
        "qwen_unavailable": 3,
    }
    for item in scenarios:
        scenario = item.get("scenario")
        if item.get("status") != "pass":
            errors.append(f"scenario did not pass: {scenario}")
        for field in ("time_to_detect_ms", "time_to_recover_ms"):
            value = item.get(field)
            if not isinstance(value, int) or not 0 <= value < 2_000:
                errors.append(f"scenario timing is invalid: {scenario}/{field}")
        if len(item.get("focused_assertions", [])) != expected_assertions.get(scenario):
            errors.append(f"scenario focused assertion count mismatch: {scenario}")
        for field in (
            "retry_proved",
            "terminal_dlq_bound_proved",
            "structured_dependency_telemetry",
        ):
            if not item.get(field):
                errors.append(f"scenario evidence is missing: {scenario}/{field}")
        if item.get("max_attempts") != 4:
            errors.append(f"scenario max attempts mismatch: {scenario}")
        if item.get("retry_events_asserted") != 1:
            errors.append(f"scenario retry event count mismatch: {scenario}")
        if item.get("terminal_dlq_events_asserted") != 1:
            errors.append(f"scenario DLQ event count mismatch: {scenario}")
        for field in (
            "early_acknowledgement",
            "confirmed_input_silently_lost",
            "duplicate_final_or_api_side_effect",
        ):
            if item.get(field) is not False:
                errors.append(f"scenario safety outcome failed: {scenario}/{field}")

    telemetry = report.get("telemetry_contract_tests", [])
    if {item.get("service") for item in telemetry} != {"approval", "persistence"}:
        errors.append("dependency telemetry tests must cover approval and persistence")
    for item in telemetry:
        if item.get("status") != "passed":
            errors.append(f"dependency telemetry test failed: {item.get('service')}")
        if item.get("metric") != "nutsnews_worker_dependency_duration_ms":
            errors.append(f"dependency metric mismatch: {item.get('service')}")
        if item.get("structured_event") != "runtime.dependency.observed":
            errors.append(f"dependency structured event mismatch: {item.get('service')}")

    alerting = report.get("alerting", {})
    if alerting.get("owner_repository") != "ramideltoro/nutsnews-infra":
        errors.append("Grafana alert ownership mismatch")
    if alerting.get("status") != "pass":
        errors.append("Grafana alert contracts did not pass")
    if not SHA_RE.fullmatch(str(alerting.get("source_commit", ""))):
        errors.append("Grafana alert source is not immutable")
    contracts = alerting.get("contracts", [])
    if {item.get("uid") for item in contracts} != ALERT_UIDS:
        errors.append("Grafana retry/DLQ alert contract set mismatch")
    for item in contracts:
        if not HASH_RE.fullmatch(str(item.get("expression_sha256", ""))):
            errors.append(f"Grafana expression hash is missing: {item.get('uid')}")
        if item.get("test_drill") != "poison-message":
            errors.append(f"Grafana test-drill mapping mismatch: {item.get('uid')}")

    for label in ("live_shadow_before", "live_shadow_after"):
        snapshot = report.get(label, {})
        if snapshot.get("status") != "pass":
            errors.append(f"{label} did not pass")
        if snapshot.get("mode") != "shadow":
            errors.append(f"{label} is not shadow")
        if snapshot.get("production_writes_enabled") is not False:
            errors.append(f"{label} enabled production writes")
        if snapshot.get("healthy_services") != 8:
            errors.append(f"{label} does not have eight healthy services")
        if snapshot.get("missing_consumers") or snapshot.get("unverifiable_consumers"):
            errors.append(f"{label} has missing or unverifiable consumers")
        queues = snapshot.get("queues", [])
        if len(queues) != 7:
            errors.append(f"{label} queue count mismatch")
        for queue in queues:
            if queue.get("consumers") != 1:
                errors.append(f"{label} queue consumer count mismatch")
            for field in ("messages", "messages_ready", "messages_unacknowledged"):
                if queue.get(field) != 0:
                    errors.append(f"{label} queue did not drain: {field}")

    if report.get("queue_drain", {}).get("status") != "pass":
        errors.append("final queue drain did not pass")
    blocker = report.get("known_independent_blocker", {})
    if blocker.get("issue") != "ramideltoro/nutsnews-worker#168":
        errors.append("independent scheduler blocker is not preserved")
    if blocker.get("resolved_by_this_drill") is not False:
        errors.append("dependency drills must not claim to resolve scheduler blocker #168")

    forbidden_keys = {
        "commands",
        "stdout",
        "stderr",
        "argv",
        "container_id",
        "credential",
        "token",
        "password",
        "endpoint",
        "model_input",
        "payload",
    }
    exposed = recursive_keys(report) & forbidden_keys
    if exposed:
        errors.append(f"report contains forbidden private/value-bearing keys: {sorted(exposed)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    errors = repository_errors()
    if args.report is not None:
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read report: {exc}")
        else:
            errors.extend(report_errors(report))

    if errors:
        print("Worker-uplift dependency outage drill validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Worker-uplift dependency outage drills are isolated, immutable, and value-free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
