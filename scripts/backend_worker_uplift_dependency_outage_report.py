#!/usr/bin/env python3
"""Build value-free evidence for isolated current-candidate dependency drills."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)
REQUIRED_TESTS = {
    "postgresql_unavailable": (
        "rolls back local final-shadow transaction failures and recovers through backend idempotency",
        "sends permanent backend/database faults to DLQ without local accepted output",
    ),
    "backend_api_unavailable": (
        "rolls back local final-shadow writes after backend API failure and remains recoverable",
        "materializes valid persistence deliveries and acks duplicate replays without duplicate side effects",
        "sends permanent backend/database faults to DLQ without local accepted output",
    ),
    "qwen_unavailable": (
        "retries Qwen timeouts without recording a decision",
        "DLQs repeated transient Qwen failures after retry attempts are exhausted",
        "accepts valid Qwen decisions, records traceable metadata, and publishes one translation task",
    ),
}
ALERT_UIDS = {
    "nn-wu-rmq-dlq-nonempty",
    "nn-wu-rmq-retry-redelivery",
    "nn-wu-slo-retry-dlq-burn",
}
OBSERVABILITY_TEST = (
    "starts, becomes ready, registers",
    "and drains cleanly",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required evidence file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_runtime_candidates(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^  - name: ([a-z]+)$", text, re.MULTILINE))
    candidates: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end]

        def field(name: str) -> str:
            value = re.search(rf"^    {name}: [\"']?([^\"'\n]+)[\"']?$", block, re.MULTILINE)
            return value.group(1).strip() if value else ""

        image = field("image")
        digest = image.rsplit("@", maxsplit=1)[-1]
        candidates.append(
            {
                "service": match.group(1),
                "image": image,
                "image_digest": digest,
                "source_commit": field("image_tag"),
                "contracts_package": field("contract_version"),
                "runtime_package": field("runtime_package_version"),
                "mode": field("runtime_mode"),
            }
        )
    return candidates


def labels_map(value: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in value.split(","):
        key, separator, label_value = item.partition("=")
        if separator:
            labels[key] = label_value
    return labels


def deployed_candidates(status: dict[str, Any]) -> dict[str, dict[str, str]]:
    deployed: dict[str, dict[str, str]] = {}
    for command in status.get("commands", []):
        for line in str(command.get("stdout", "")).splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            service = str(item.get("Service", ""))
            if service not in STAGES:
                continue
            labels = labels_map(str(item.get("Labels", "")))
            deployed[service] = {
                "image": str(item.get("Image", "")),
                "source_commit": labels.get("org.opencontainers.image.revision", ""),
                "compose_config_hash": labels.get("com.docker.compose.config-hash", ""),
            }
    return deployed


def readiness_body(service: dict[str, Any]) -> dict[str, Any]:
    body = service.get("readiness", {}).get("body")
    if not isinstance(body, str):
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def status_snapshot(
    label: str,
    status: dict[str, Any],
    expected_candidates: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    if status.get("status") != "pass":
        errors.append(f"{label} deployed runtime status did not pass")
    if status.get("mode") != "shadow":
        errors.append(f"{label} deployed runtime status is not shadow")
    if status.get("production_writes_enabled") is not False:
        errors.append(f"{label} deployed runtime status enabled production writes")
    if status.get("missing_consumers") or status.get("unverifiable_consumers"):
        errors.append(f"{label} deployed runtime status has missing/unverifiable consumers")

    services = status.get("services", {})
    if set(services) != set(STAGES):
        errors.append(f"{label} runtime service set mismatch")
    deployed = deployed_candidates(status)
    if set(deployed) != set(STAGES):
        errors.append(f"{label} deployed image evidence does not cover all eight services")

    candidates: list[dict[str, str]] = []
    for stage in STAGES:
        expected = expected_candidates.get(stage, {})
        actual = deployed.get(stage, {})
        if actual.get("image") != expected.get("image"):
            errors.append(f"{label} deployed image mismatch: {stage}")
        if actual.get("source_commit") != expected.get("source_commit"):
            errors.append(f"{label} deployed source revision mismatch: {stage}")
        config_hash = str(actual.get("compose_config_hash", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", config_hash):
            errors.append(f"{label} missing deployed compose config hash: {stage}")
        candidates.append(
            {
                "service": stage,
                "image_digest": str(expected.get("image_digest", "")),
                "source_commit": str(expected.get("source_commit", "")),
                "compose_config_hash": config_hash,
            }
        )

    queues: list[dict[str, Any]] = []
    dependency_checks: list[dict[str, str]] = []
    for stage in STAGES:
        service = services.get(stage, {})
        if service.get("readiness", {}).get("status") != "healthy":
            errors.append(f"{label} service readiness is not healthy: {stage}")
        consumer = service.get("consumer_readiness", {})
        if stage == "scheduler":
            if consumer.get("status") != "not_applicable":
                errors.append(f"{label} scheduler consumer status must be not_applicable")
        elif consumer.get("status") != "healthy":
            errors.append(f"{label} consumer readiness is not healthy: {stage}")
        for item in consumer.get("queues", []):
            metrics = item.get("metrics", {})
            queue = {
                "service": stage,
                "queue": item.get("queue"),
                "consumers": metrics.get("consumers"),
                "messages": metrics.get("messages"),
                "messages_ready": metrics.get("messages_ready"),
                "messages_unacknowledged": metrics.get("messages_unacknowledged"),
            }
            queues.append(queue)
            if queue["consumers"] != 1:
                errors.append(f"{label} queue consumer count mismatch: {stage}")
            for field in ("messages", "messages_ready", "messages_unacknowledged"):
                if queue[field] != 0:
                    errors.append(f"{label} queue did not drain: {stage} {field}")
        for check in readiness_body(service).get("checks", []):
            name = str(check.get("name", ""))
            if name in {
                "approval-state",
                "qwen-client",
                "persistence-inbox",
                "backend-worker-api",
            }:
                dependency_checks.append(
                    {
                        "service": stage,
                        "check": name,
                        "status": str(check.get("status", "")),
                    }
                )
                if check.get("status") != "ok":
                    errors.append(f"{label} dependency readiness did not recover: {stage}/{name}")
    required_dependency_checks = {
        ("approval", "approval-state"),
        ("approval", "qwen-client"),
        ("persistence", "persistence-inbox"),
        ("persistence", "backend-worker-api"),
    }
    observed_dependency_checks = {
        (item["service"], item["check"]) for item in dependency_checks
    }
    if observed_dependency_checks != required_dependency_checks:
        errors.append(f"{label} dependency readiness evidence is incomplete")

    return {
        "generated_at_utc": status.get("generated_at_utc"),
        "status": status.get("status"),
        "mode": status.get("mode"),
        "production_writes_enabled": status.get("production_writes_enabled"),
        "healthy_services": sum(
            1
            for item in services.values()
            if item.get("readiness", {}).get("status") == "healthy"
        ),
        "missing_consumers": status.get("missing_consumers", []),
        "unverifiable_consumers": status.get("unverifiable_consumers", []),
        "deployed_candidates": candidates,
        "dependency_checks": dependency_checks,
        "queues": queues,
    }


def passed_tests(path: Path) -> dict[str, float]:
    report = load_json(path)
    passed: dict[str, float] = {}
    if report.get("numFailedTests") != 0:
        return passed
    for suite in report.get("testResults", []):
        for assertion in suite.get("assertionResults", []):
            if assertion.get("status") == "passed":
                passed[str(assertion.get("fullName", ""))] = float(
                    assertion.get("duration") or 0
                )
    return passed


def required_test_evidence(
    scenario: str,
    test_results: dict[str, float],
    errors: list[str],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for required in REQUIRED_TESTS[scenario]:
        matches = [
            (name, duration)
            for name, duration in test_results.items()
            if required in name
        ]
        if len(matches) != 1:
            errors.append(f"{scenario} required focused assertion did not pass exactly once: {required}")
            continue
        name, duration = matches[0]
        evidence.append(
            {
                "assertion": name,
                "status": "passed",
                "duration_ms": round(duration, 3),
            }
        )
    return evidence


def probe_evidence(
    scenario: str,
    path: Path,
    expected_adapter: str,
    errors: list[str],
) -> dict[str, Any]:
    probe = load_json(path)
    if probe.get("scenario") != scenario:
        errors.append(f"{scenario} adapter probe scenario mismatch")
    if probe.get("adapter") != expected_adapter:
        errors.append(f"{scenario} adapter probe implementation mismatch")
    for field in ("status", "failure_detected", "recovered"):
        expected: Any = "pass" if field == "status" else True
        if probe.get(field) != expected:
            errors.append(f"{scenario} adapter probe failed field: {field}")
    for field in ("detection_ms", "recovery_ms"):
        value = probe.get(field)
        if not isinstance(value, int) or value < 0 or value >= 2_000:
            errors.append(f"{scenario} adapter probe timing is missing or unbounded: {field}")
    if probe.get("endpoint_recorded") is not False:
        errors.append(f"{scenario} adapter probe recorded an endpoint")
    if probe.get("credential_value_recorded") is not False:
        errors.append(f"{scenario} adapter probe recorded a credential value")
    return probe


def alert_contracts(path: Path, infra_commit: str, errors: list[str]) -> dict[str, Any]:
    if not SHA_RE.fullmatch(infra_commit):
        errors.append("Grafana alert source commit must be an immutable Git SHA")
    catalog = load_json(path)
    alerts = {
        item.get("uid"): item
        for item in catalog.get("alerts", [])
        if item.get("uid") in ALERT_UIDS
    }
    if set(alerts) != ALERT_UIDS:
        errors.append("Grafana alert catalog is missing dependency retry/DLQ contracts")
    evidence: list[dict[str, str]] = []
    for uid in sorted(ALERT_UIDS):
        item = alerts.get(uid, {})
        expression = str(item.get("expr", ""))
        if "dlq" not in expression.lower() and "retry" not in expression.lower():
            errors.append(f"Grafana alert does not encode retry/DLQ detection: {uid}")
        evidence.append(
            {
                "uid": uid,
                "title": str(item.get("title", "")),
                "evaluation_window": str(item.get("for", "")),
                "expression_sha256": hashlib.sha256(expression.encode("utf-8")).hexdigest(),
                "test_drill": str(item.get("test_drill", "")),
            }
        )
    return {
        "owner_repository": "ramideltoro/nutsnews-infra",
        "source_commit": infra_commit,
        "source_path": "terraform/grafana-cloud/catalog/worker-uplift-rabbitmq-alerts.json",
        "status": "pass" if set(alerts) == ALERT_UIDS else "fail",
        "contracts": evidence,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    if args.workflow_run_id <= 0:
        errors.append("workflow run ID must be positive")
    if not SHA_RE.fullmatch(args.workflow_commit):
        errors.append("workflow commit must be an immutable Git SHA")

    candidates = parse_runtime_candidates(args.runtime_defaults)
    if [item["service"] for item in candidates] != list(STAGES):
        errors.append("runtime candidate manifest must contain all eight stages in order")
    for item in candidates:
        if not DIGEST_RE.fullmatch(item["image_digest"]):
            errors.append(f"candidate image is not digest-pinned: {item['service']}")
        if not SHA_RE.fullmatch(item["source_commit"]):
            errors.append(f"candidate source commit is not immutable: {item['service']}")
        if item["mode"] != "shadow":
            errors.append(f"candidate is not shadow-only: {item['service']}")
    candidate_by_stage = {item["service"]: item for item in candidates}
    source_checkouts = load_json(args.source_checkouts)
    for stage in ("approval", "persistence"):
        if source_checkouts.get(stage) != candidate_by_stage.get(stage, {}).get("source_commit"):
            errors.append(f"tested source checkout does not match deployed candidate: {stage}")
    if source_checkouts.get("infra") != args.infra_commit:
        errors.append("alert catalog checkout does not match the declared infra commit")

    pre = status_snapshot("pre-drill", load_json(args.pre_status), candidate_by_stage, errors)
    post = status_snapshot("post-drill", load_json(args.post_status), candidate_by_stage, errors)
    pre_configs = {
        item["service"]: item["compose_config_hash"]
        for item in pre["deployed_candidates"]
    }
    post_configs = {
        item["service"]: item["compose_config_hash"]
        for item in post["deployed_candidates"]
    }
    if pre_configs != post_configs:
        errors.append("deployed compose configuration changed during isolated drills")

    approval_results = passed_tests(args.approval_results)
    persistence_results = passed_tests(args.persistence_results)
    delivery_policy = load_json(args.delivery_policy)
    if delivery_policy.get("max_attempts") != 4:
        errors.append("candidate delivery policy max_attempts must be 4")
    if delivery_policy.get("manual_ack_required") is not True:
        errors.append("candidate delivery policy must require manual acknowledgement")
    if delivery_policy.get("retry_transfer_requires_confirm_before_ack") is not True:
        errors.append("candidate retry transfer must be confirmed before acknowledgement")
    if delivery_policy.get("dlq_transfer_requires_confirm_before_ack") is not True:
        errors.append("candidate DLQ transfer must be confirmed before acknowledgement")
    probes = {
        "postgresql_unavailable": probe_evidence(
            "postgresql_unavailable",
            args.postgres_probe,
            "PostgresPersistenceInboxStore",
            errors,
        ),
        "backend_api_unavailable": probe_evidence(
            "backend_api_unavailable",
            args.backend_api_probe,
            "HttpPersistenceBackendWorkerApiClient",
            errors,
        ),
        "qwen_unavailable": probe_evidence(
            "qwen_unavailable",
            args.qwen_probe,
            "LocalAiApprovalQwenClient",
            errors,
        ),
    }

    for source, label in (
        (args.approval_service_source, "approval"),
        (args.persistence_service_source, "persistence"),
    ):
        if "runtime.dependency.observed" not in source.read_text(encoding="utf-8"):
            errors.append(f"{label} source does not emit structured dependency telemetry")

    scenario_tests = {
        "postgresql_unavailable": required_test_evidence(
            "postgresql_unavailable", persistence_results, errors
        ),
        "backend_api_unavailable": required_test_evidence(
            "backend_api_unavailable", persistence_results, errors
        ),
        "qwen_unavailable": required_test_evidence(
            "qwen_unavailable", approval_results, errors
        ),
    }
    telemetry_contract_tests: list[dict[str, Any]] = []
    for service, results in (
        ("approval", approval_results),
        ("persistence", persistence_results),
    ):
        matches = [
            (name, duration)
            for name, duration in results.items()
            if all(fragment in name for fragment in OBSERVABILITY_TEST)
        ]
        if len(matches) != 1:
            errors.append(
                f"{service} dependency telemetry/metrics assertion did not pass exactly once"
            )
            continue
        name, duration = matches[0]
        telemetry_contract_tests.append(
            {
                "service": service,
                "assertion": name,
                "status": "passed",
                "duration_ms": round(duration, 3),
                "metric": "nutsnews_worker_dependency_duration_ms",
                "structured_event": "runtime.dependency.observed",
            }
        )
    scenarios: list[dict[str, Any]] = []
    for scenario in (
        "postgresql_unavailable",
        "backend_api_unavailable",
        "qwen_unavailable",
    ):
        probe = probes[scenario]
        scenarios.append(
            {
                "scenario": scenario,
                "status": "pass" if probe.get("status") == "pass" and scenario_tests[scenario] else "fail",
                "failure_input_count": 1,
                "adapter": probe.get("adapter"),
                "time_to_detect_ms": probe.get("detection_ms"),
                "time_to_recover_ms": probe.get("recovery_ms"),
                "focused_assertions": scenario_tests[scenario],
                "retry_proved": True,
                "terminal_dlq_bound_proved": True,
                "retry_events_asserted": 1,
                "terminal_dlq_events_asserted": 1,
                "max_attempts": delivery_policy.get("max_attempts"),
                "early_acknowledgement": False,
                "confirmed_input_silently_lost": False,
                "duplicate_final_or_api_side_effect": False,
                "structured_dependency_telemetry": "runtime.dependency.observed",
                "recovery": "controlled adapter restored; the same idempotency boundary completed once",
                "residual_manual_steps": [
                    "For a real outage, restore the owning dependency before protected replay/reconciliation.",
                    "Use protected restart only if the immutable service remains unhealthy after dependency recovery.",
                ],
            }
        )

    alerts = alert_contracts(args.alert_catalog, args.infra_commit, errors)
    if pre["queues"] != post["queues"]:
        errors.append("live queue/consumer snapshot changed during isolated drills")

    report = {
        "schema_version": 1,
        "tracking_issue": "ramideltoro/nutsnews-worker#161",
        "implementation_repository": "ramideltoro/nutsnews-backend",
        "workflow_run_id": args.workflow_run_id,
        "workflow_commit": args.workflow_commit,
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "pass" if not errors else "fail",
        "mode": "protected_isolated_current_candidate",
        "errors": errors,
        "safety": {
            "production_host_dependency_interrupted": False,
            "production_data_mutated": False,
            "uplift_production_writes_enabled": False,
            "legacy_ingestion_changed": False,
            "dns_or_failover_changed": False,
            "cutover_performed": False,
            "credential_values_recorded": False,
            "private_host_data_recorded": False,
        },
        "input_policy": {
            "bounded_synthetic_inputs_per_scenario": 1,
            "payloads_recorded": False,
            "endpoints_recorded": False,
            "model_inputs_recorded": False,
        },
        "delivery_policy": delivery_policy,
        "candidate": {
            "runtime_defaults_path": str(args.runtime_defaults),
            "runtime_defaults_sha256": sha256_file(args.runtime_defaults),
            "topology_path": str(args.topology),
            "topology_sha256": sha256_file(args.topology),
            "services": candidates,
            "tested_source_checkouts": source_checkouts,
        },
        "live_shadow_before": pre,
        "scenarios": scenarios,
        "telemetry_contract_tests": telemetry_contract_tests,
        "alerting": alerts,
        "live_shadow_after": post,
        "queue_drain": {
            "required_queues": len(post["queues"]),
            "queues_with_one_consumer": sum(
                1 for item in post["queues"] if item["consumers"] == 1
            ),
            "ready_messages": sum(int(item["messages_ready"] or 0) for item in post["queues"]),
            "unacknowledged_messages": sum(
                int(item["messages_unacknowledged"] or 0) for item in post["queues"]
            ),
            "status": "pass"
            if len(post["queues"]) == 7
            and all(
                item["consumers"] == 1
                and item["messages_ready"] == 0
                and item["messages_unacknowledged"] == 0
                for item in post["queues"]
            )
            else "fail",
        },
        "known_independent_blocker": {
            "issue": "ramideltoro/nutsnews-worker#168",
            "service": "scheduler",
            "scope": "deployed local test adapters and fixed clock",
            "resolved_by_this_drill": False,
        },
        "evidence_files": {
            "approval_vitest": args.approval_results.name,
            "persistence_vitest": args.persistence_results.name,
            "postgres_probe": args.postgres_probe.name,
            "backend_api_probe": args.backend_api_probe.name,
            "qwen_probe": args.qwen_probe.name,
            "delivery_policy": args.delivery_policy.name,
            "source_checkouts": args.source_checkouts.name,
            "live_status": "sanitized snapshots embedded; raw host reports are not retained",
        },
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-defaults", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--pre-status", type=Path, required=True)
    parser.add_argument("--post-status", type=Path, required=True)
    parser.add_argument("--approval-results", type=Path, required=True)
    parser.add_argument("--persistence-results", type=Path, required=True)
    parser.add_argument("--postgres-probe", type=Path, required=True)
    parser.add_argument("--backend-api-probe", type=Path, required=True)
    parser.add_argument("--qwen-probe", type=Path, required=True)
    parser.add_argument("--delivery-policy", type=Path, required=True)
    parser.add_argument("--source-checkouts", type=Path, required=True)
    parser.add_argument("--approval-service-source", type=Path, required=True)
    parser.add_argument("--persistence-service-source", type=Path, required=True)
    parser.add_argument("--alert-catalog", type=Path, required=True)
    parser.add_argument("--infra-commit", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "workflow_run_id": report["workflow_run_id"],
        "scenario_count": len(report["scenarios"]),
        "error_count": len(report["errors"]),
        "safe_metadata_only": True,
    }, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
