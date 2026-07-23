#!/usr/bin/env python3
"""Validate the worker-uplift RabbitMQ capacity and security decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "worker-uplift-rabbitmq-capacity-security-decision.json"
BASELINE_PATH = ROOT / "docs" / "backend-service-baseline.json"
IDENTITIES_PATH = ROOT / "docs" / "worker-uplift-runtime-identities.json"
ADR_PATH = ROOT / "docs" / "worker-uplift-architecture-adr.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def require_keys(errors: list[str], mapping: dict, keys: tuple[str, ...], prefix: str) -> None:
    for key in keys:
        if key not in mapping:
            errors.append(f"missing {prefix}.{key}")


def main() -> int:
    decision = load_json(DECISION_PATH)
    baseline = load_json(BASELINE_PATH)
    identities = load_json(IDENTITIES_PATH)
    adr = load_json(ADR_PATH)
    errors: list[str] = []

    if decision.get("tracking_issue") != 79:
        errors.append("tracking_issue must be 79")
    if decision.get("status") != "approved_for_bootstrap_not_provisioned":
        errors.append("status must be approved_for_bootstrap_not_provisioned")

    guardrail = decision.get("guardrail", {})
    for flag in ("broker_provisioned_by_this_issue", "legacy_path_changed_by_this_issue", "production_write_enabled"):
        if guardrail.get(flag) is not False:
            errors.append(f"guardrail.{flag} must be false")

    deps = set(decision.get("depends_on", []))
    for dep in (
        "ramideltoro/nutsnews-worker#69",
        "ramideltoro/nutsnews-worker#70",
        "ramideltoro/nutsnews-worker#78",
        "docs/worker-uplift-architecture-adr.json",
        "docs/worker-uplift-operation-map.json",
        "docs/worker-uplift-runtime-identities.json",
    ):
        if dep not in deps:
            errors.append(f"missing dependency: {dep}")

    selected = decision.get("decision", {})
    if selected.get("rabbitmq_release") != "4.3.3":
        errors.append("RabbitMQ release must be 4.3.3 until this approval is revised")
    if selected.get("selected_queue_type") != "durable_classic":
        errors.append("selected_queue_type must be durable_classic for the single-node phase")
    if "not broker high availability" not in selected.get("single_node_ha_statement", ""):
        errors.append("single-node statement must explicitly reject broker HA")

    image = selected.get("image", {})
    if image.get("mutable_tag_allowed") is not False:
        errors.append("mutable RabbitMQ tags must not be allowed")
    for digest_key in ("oci_index_digest", "linux_amd64_manifest_digest"):
        if not str(image.get(digest_key, "")).startswith("sha256:"):
            errors.append(f"image.{digest_key} must be a sha256 digest")
    if "@sha256:" not in image.get("required_reference", ""):
        errors.append("image.required_reference must be digest-pinned")

    support = selected.get("release_support", {})
    require_keys(
        errors,
        support,
        ("release_date", "community_support_end", "commercial_support_end", "erlang_otp_minimum", "erlang_otp_maximum"),
        "decision.release_support",
    )

    upgrade = selected.get("upgrade_path", {})
    for required in ("pinned", "feature flags", "release notes", "Erlang compatibility"):
        if required not in json.dumps(upgrade):
            errors.append(f"upgrade path must mention {required}")

    retry = selected.get("retry_dlq_strategy", {})
    if retry.get("selected") != "application_confirmed_retry_dlq_transfer":
        errors.append("retry/DLQ strategy must be application-confirmed transfer")
    if retry.get("publisher_confirms_required") is not True:
        errors.append("publisher confirms must be required")
    if retry.get("quorum_at_least_once_dlx_selected") is not False:
        errors.append("quorum at-least-once DLX must not be selected for the current classic-queue decision")
    caveats = "\n".join(retry.get("quorum_dlx_caveats_for_future_review", []))
    for required in ("reject-publish", "stream feature flag", "target availability", "source messages", "memory"):
        if required not in caveats:
            errors.append(f"future quorum DLX caveats must account for {required}")

    host = decision.get("measured_host_headroom", {})
    require_keys(
        errors,
        host,
        ("cpu", "memory", "disk", "io", "file_descriptors", "docker", "postgresql", "qwen_or_local_ai", "backup", "firewall_and_listeners"),
        "measured_host_headroom",
    )
    if host.get("cpu", {}).get("vcpus") != 4:
        errors.append("measured host CPU must record 4 vCPU")
    if host.get("memory", {}).get("available_bytes", 0) < 8_000_000_000:
        errors.append("measured host memory headroom must remain explicit and above 8 GB at capture")
    if host.get("disk", {}).get("root_available_bytes", 0) < 50_000_000_000:
        errors.append("measured disk headroom must remain explicit and above 50 GB at capture")
    if host.get("docker", {}).get("backend_host_status") != "not_installed":
        errors.append("this design issue must not mark Docker/RabbitMQ as installed on the backend host")
    if host.get("qwen_or_local_ai", {}).get("service_status") != "not_found":
        errors.append("Qwen/local AI status must be measured and not assumed deployed")

    public_ports = {int(entry["port"]) for entry in baseline.get("public_tcp_ports", [])}
    if not public_ports.issubset({22, 80, 443}):
        errors.append(f"service baseline exposes unsupported public ports: {sorted(public_ports)}")
    not_deployed = set(baseline.get("not_deployed", []))
    followup = decision.get("followup_provisioning_status", {})
    if "RabbitMQ broker" not in not_deployed:
        if followup.get("status") != "provisioned_by_later_protected_backend_bootstrap":
            errors.append("capacity decision must record later protected provisioning before RabbitMQ leaves not_deployed")
        if "loopback-only" not in followup.get("statement", ""):
            errors.append("later RabbitMQ provisioning addendum must preserve loopback-only listener scope")
        followup_issues = set(followup.get("tracking_issues", []))
        for issue in ("ramideltoro/nutsnews-worker#80", "ramideltoro/nutsnews-worker#84"):
            if issue not in followup_issues:
                errors.append(f"later RabbitMQ provisioning addendum missing tracking issue: {issue}")

    identity_routes = {route.get("route_id") for route in identities.get("rabbitmq", {}).get("route_permissions", [])}
    workload = decision.get("workload_estimate", {})
    if set(workload.get("routes", [])) != identity_routes:
        errors.append("workload routes must match worker-uplift RabbitMQ identities")
    queue_count = workload.get("queue_count", {})
    if queue_count.get("main_queues") != len(identity_routes):
        errors.append("main queue count must match the seven route identities")
    if queue_count.get("retry_tiers_per_route") != 3:
        errors.append("retry tier count must be three per route")
    if queue_count.get("retry_queues") != len(identity_routes) * 3:
        errors.append("retry queue count must be three per route")
    if queue_count.get("dlq_queues") != len(identity_routes):
        errors.append("DLQ count must match route count")
    if queue_count.get("total_queues") != 35:
        errors.append("total queue count must be 35 for seven routes with three retry tiers and DLQs")

    message_shape = workload.get("message_shape", {})
    if message_shape.get("hard_max_body_bytes") != 65536:
        errors.append("hard max message body must remain 65536 bytes")
    forbidden = set(message_shape.get("forbidden_payload_content", []))
    for field in ("article_body", "full_prompt", "secret", "credential", "bearer_token", "raw_provider_response"):
        if field not in forbidden:
            errors.append(f"message shape must forbid {field}")

    limits = decision.get("hard_limits", {})
    queues = limits.get("queues", {})
    if queues.get("type") != "classic":
        errors.append("hard queue limits must apply to classic queues")
    if queues.get("durable") is not True:
        errors.append("queues must be durable")
    if queues.get("overflow") != "reject-publish":
        errors.append("queue overflow must be reject-publish")
    if queues.get("drop_head_allowed") is not False:
        errors.append("resource pressure must not silently drop oldest work")
    if queues.get("total_hard_message_cap") != 49000:
        errors.append("total hard message cap must be 49000")
    if queues.get("total_hard_body_bytes_cap") != 6576668672:
        errors.append("total hard body byte cap must be 6576668672")
    for tier in ("main", "retry", "dlq"):
        tier_limits = queues.get(tier, {})
        if tier_limits.get("x_max_length", 0) <= 0:
            errors.append(f"{tier} queues must define x_max_length")
        if tier_limits.get("x_max_length_bytes", 0) <= 0:
            errors.append(f"{tier} queues must define x_max_length_bytes")

    broker_memory = limits.get("broker_memory", {})
    if broker_memory.get("container_memory_limit_bytes") != 1073741824:
        errors.append("broker memory container limit must be 1 GiB")
    if broker_memory.get("vm_memory_high_watermark_absolute_bytes") != 536870912:
        errors.append("broker memory watermark must be 512 MiB")
    broker_disk = limits.get("broker_disk", {})
    if broker_disk.get("disk_free_limit_absolute_bytes") != 21474836480:
        errors.append("broker disk free alarm must be 20 GiB")
    if broker_disk.get("publisher_block_on_disk_alarm") is not True:
        errors.append("disk alarm must block publishers")
    if limits.get("file_descriptors", {}).get("nofile_soft") != 65536:
        errors.append("RabbitMQ nofile soft limit must be 65536")

    flow = limits.get("connections_channels_and_flow", {})
    for required in ("heartbeat_seconds", "app_connection_limit", "app_channel_limit", "prefetch_per_consumer", "publisher_confirm_inflight_per_channel", "publisher_confirm_timeout_ms"):
        if flow.get(required, 0) <= 0:
            errors.append(f"missing positive flow limit: {required}")

    benchmark = decision.get("benchmark_evidence", {})
    if benchmark.get("production_host_benchmark_ran") is not False:
        errors.append("benchmark must not run on the production host for this design issue")
    if benchmark.get("scenario", {}).get("queues_per_type") != 35:
        errors.append("benchmark scenario must cover 35 queues per queue type")
    if "worker_uplift_rabbitmq_capacity_benchmark.py" not in benchmark.get("benchmark_script", ""):
        errors.append("benchmark script path must be recorded")
    comparison = {item.get("queue_type"): item for item in benchmark.get("comparison", [])}
    if comparison.get("durable_classic", {}).get("selected") is not True:
        errors.append("benchmark comparison must select durable classic")
    if comparison.get("single_replica_quorum", {}).get("selected") is not False:
        errors.append("benchmark comparison must not select single-replica quorum")
    for item in comparison.values():
        if "No broker high availability" not in item.get("ha_statement", ""):
            errors.append(f"benchmark HA statement missing for {item.get('queue_type')}")

    network = decision.get("network_security", {})
    if network.get("planned_public_amqp_management_or_prometheus_listeners") != []:
        errors.append("AMQP, management, and Prometheus listeners must not be planned for public access")
    for listener in ("amqp", "management", "prometheus"):
        if network.get(listener, {}).get("public_access_allowed") is not False:
            errors.append(f"{listener} public access must be false")
    if "127.0.0.1" not in network.get("management", {}).get("binding", ""):
        errors.append("management binding must be loopback/private")
    if "127.0.0.1" not in network.get("prometheus", {}).get("binding", ""):
        errors.append("Prometheus binding must be loopback/private")
    credentials = network.get("credentials", {})
    if credentials.get("identity_source") != "docs/worker-uplift-runtime-identities.json":
        errors.append("credentials must source worker-uplift runtime identities")
    if "delete or disable" not in credentials.get("default_guest_user_policy", ""):
        errors.append("default guest user must be deleted or disabled")

    monitoring = decision.get("monitoring_and_maintenance", {})
    for metric in ("rabbitmq_up", "rabbitmq_queue_messages_ready", "worker_runtime_message_dlq_total"):
        if metric not in monitoring.get("metrics", []):
            errors.append(f"missing monitoring metric: {metric}")
    if "protected backend" not in monitoring.get("maintenance", ""):
        errors.append("maintenance must require protected backend workflow/approval")
    if "Do not treat broker queue contents" not in monitoring.get("definition_backup", ""):
        errors.append("broker backup role must reject queue contents as the only backup")

    recovery = decision.get("recovery_model", {})
    if "PostgreSQL" not in recovery.get("authoritative_recovery_source", ""):
        errors.append("PostgreSQL outbox/reconciliation must be the recovery source")
    if "not the disaster-recovery source" not in recovery.get("broker_backup_role", ""):
        errors.append("broker backup must not be the disaster-recovery source")
    if "outbox" not in "\n".join(recovery.get("broker_loss_procedure", [])).lower():
        errors.append("broker loss procedure must replay from outbox")

    migration = decision.get("managed_or_three_node_migration_trigger", {})
    if "three-node" not in migration.get("decision", "") and "managed RabbitMQ" not in migration.get("decision", ""):
        errors.append("managed/three-node migration trigger must be explicit")
    if len(migration.get("triggers", [])) < 5:
        errors.append("managed/three-node migration triggers must be specific")

    references = set(decision.get("source_references", []))
    for url in (
        "https://www.rabbitmq.com/release-information",
        "https://www.rabbitmq.com/docs/which-erlang",
        "https://hub.docker.com/_/rabbitmq",
        "https://www.rabbitmq.com/docs/upgrade",
        "https://www.rabbitmq.com/docs/quorum-queues",
        "https://www.rabbitmq.com/docs/dlx",
        "https://www.rabbitmq.com/docs/alarms",
        "https://www.rabbitmq.com/docs/maxlength",
    ):
        if url not in references:
            errors.append(f"missing source reference: {url}")

    if adr.get("deployment_model", {}).get("single_node_ha_statement") != "A single RabbitMQ node is not high availability.":
        errors.append("architecture ADR must still reject single-node RabbitMQ HA")

    validation = decision.get("validation", {})
    if validation.get("local_validator") != "python3 scripts/validate_worker_uplift_rabbitmq_capacity_security.py":
        errors.append("validation.local_validator must name this script")
    if "Benchmark RabbitMQ" not in validation.get("ci_workflow", ""):
        errors.append("validation.ci_workflow must name the benchmark workflow")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker-uplift RabbitMQ capacity/security decision is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
