#!/usr/bin/env python3
"""Validate worker-uplift RabbitMQ Ansible/Compose provisioning."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = ROOT / "docs" / "worker-uplift-rabbitmq-capacity-security-decision.json"
BACKUP_MATRIX_PATH = ROOT / "docs" / "backend-backup-service-matrix.json"
ROLE = ROOT / "ansible" / "roles" / "backend_rabbitmq"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"
COMPOSE_TEMPLATE = ROLE / "templates" / "rabbitmq-compose.yml.j2"
RABBITMQ_CONFIG_TEMPLATE = ROLE / "templates" / "rabbitmq.conf.j2"
PROBE = ROLE / "files" / "nutsnews_rabbitmq_probe.py"
TOPOLOGY = ROLE / "files" / "nutsnews_rabbitmq_topology.py"
TOPOLOGY_TEMPLATE = ROLE / "templates" / "worker-uplift-topology.json.j2"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
PROTECTED_WORKFLOW = ROOT / ".github" / "workflows" / "protected-backend-ansible-apply.yml"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"
SAFETY = ROOT / "scripts" / "backend_deployment_safety.py"
CONTROLLED_MAINTENANCE = ROOT / "scripts" / "backend_controlled_maintenance.py"
RUNBOOK = ROOT / "runbooks" / "WORKER_UPLIFT_RABBITMQ_PROVISIONING.md"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def main() -> int:
    decision = json.loads(read(DECISION_PATH))
    backup_matrix = json.loads(read(BACKUP_MATRIX_PATH))
    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    handlers = read(HANDLERS)
    compose = read(COMPOSE_TEMPLATE)
    rabbitmq_conf = read(RABBITMQ_CONFIG_TEMPLATE)
    probe = read(PROBE)
    topology_script = read(TOPOLOGY)
    topology_template = read(TOPOLOGY_TEMPLATE)
    topology = json.loads(topology_template.replace("{{ backend_rabbitmq_vhost }}", "nutsnews-worker-uplift"))
    playbook = read(PLAYBOOK)
    protected = read(PROTECTED_WORKFLOW)
    checks = read(BACKEND_CHECKS)
    safety = read(SAFETY)
    controlled_maintenance = read(CONTROLLED_MAINTENANCE)
    runbook = read(RUNBOOK)
    errors: list[str] = []

    digest = decision.get("decision", {}).get("image", {}).get("linux_amd64_manifest_digest")
    if not digest or not str(digest).startswith("sha256:"):
        errors.append("capacity decision must record linux/amd64 digest")
    pinned_image = f"rabbitmq@{digest}"
    for label, text in (("defaults", defaults), ("benchmark/protected workflow", protected + checks)):
        if pinned_image not in text:
            errors.append(f"{label} must use approved pinned RabbitMQ image {pinned_image}")

    if "name: backend_rabbitmq" not in playbook or "backend_rabbitmq_enabled | default(false) | bool" not in playbook:
        errors.append("bootstrap playbook must include focused backend_rabbitmq role behind enable flag")

    for path in (DEFAULTS, TASKS, HANDLERS, COMPOSE_TEMPLATE, RABBITMQ_CONFIG_TEMPLATE, PROBE, TOPOLOGY, TOPOLOGY_TEMPLATE, RUNBOOK):
        if not path.exists():
            errors.append(f"missing RabbitMQ provisioning file: {path}")

    if "backend_rabbitmq_packages:" not in defaults or "docker.io" not in defaults or "docker-compose-v2" not in defaults:
        errors.append("RabbitMQ defaults must install docker.io and docker-compose-v2")
    if "backend_rabbitmq_bind_address: 127.0.0.1" not in defaults:
        errors.append("RabbitMQ defaults must bind host ports to loopback")
    if "backend_rabbitmq_memory_limit: 1g" not in defaults:
        errors.append("RabbitMQ defaults must set 1g memory limit")
    if "backend_rabbitmq_disk_free_limit: 20GB" not in defaults:
        errors.append("RabbitMQ defaults must set 20GB disk free limit")
    if "backend_rabbitmq_nofile: 65536" not in defaults:
        errors.append("RabbitMQ defaults must set nofile to 65536")
    if 'backend_rabbitmq_probe_state_dir: /var/lib/nutsnews/rabbitmq-probes' not in defaults:
        errors.append("RabbitMQ probe state must live outside the broker data mount")
    if 'backend_rabbitmq_container_uid: "999"' not in defaults or 'backend_rabbitmq_container_gid: "999"' not in defaults:
        errors.append("RabbitMQ defaults must declare the approved container UID/GID")

    for required in (
        'restart: unless-stopped',
        'env_file:',
        '{{ backend_rabbitmq_bind_address }}',
        '5672',
        '15672',
        '15692',
        'mem_limit:',
        'ulimits:',
        'rabbitmq-diagnostics -q ping',
        '/var/lib/rabbitmq',
    ):
        if required not in compose:
            errors.append(f"Compose template missing required setting: {required}")
    if "0.0.0.0" in compose:
        errors.append("Compose template must not bind RabbitMQ ports to 0.0.0.0")

    for required in (
        "vm_memory_high_watermark.absolute = {{ backend_rabbitmq_memory_high_watermark }}",
        "disk_free_limit.absolute = {{ backend_rabbitmq_disk_free_limit }}",
        "heartbeat = {{ backend_rabbitmq_heartbeat_seconds }}",
        "channel_max = {{ backend_rabbitmq_channel_max }}",
        "default_queue_type = classic",
    ):
        if required not in rabbitmq_conf:
            errors.append(f"RabbitMQ config template missing: {required}")

    if "RABBITMQ_DEFAULT_PASS={{ backend_rabbitmq_admin_password }}" not in tasks:
        errors.append("RabbitMQ environment file must be rendered from protected admin password var")
    env_task = tasks.split("Render RabbitMQ root-only environment", 1)[-1].split("- name:", 1)[0]
    if "no_log: true" not in env_task or 'mode: "0600"' not in env_task:
        errors.append("RabbitMQ environment render must be root-only and no_log")

    forbidden_process_arg_patterns = [
        r"docker\s+exec[^\n]+backend_rabbitmq_admin_password",
        r"curl[^\n]+backend_rabbitmq_admin_password",
        r"rabbitmqctl[^\n]+backend_rabbitmq_admin_password",
    ]
    for pattern in forbidden_process_arg_patterns:
        if re.search(pattern, tasks):
            errors.append(f"RabbitMQ secret could appear in process args: {pattern}")

    for compose_arg in ("docker", "compose", "{{ backend_rabbitmq_compose_path }}", "config"):
        if compose_arg not in tasks:
            errors.append(f"RabbitMQ role must validate Compose config with arg: {compose_arg}")
    if "Validate RabbitMQ Compose definition" not in tasks:
        errors.append("RabbitMQ role must validate Compose config")
    if "Pull pinned RabbitMQ image during protected apply" not in tasks:
        errors.append("RabbitMQ role must pull the pinned image during protected apply")
    data_tree_task = tasks.split("Repair RabbitMQ persistent data tree ownership", 1)[-1].split("- name:", 1)[0]
    if "recurse: true" not in data_tree_task or 'mode: "u+rwX"' not in data_tree_task:
        errors.append("RabbitMQ role must recursively repair broker data ownership and owner write bits before runtime probes")
    if "notify:" in data_tree_task or "Restart RabbitMQ" in data_tree_task:
        errors.append("RabbitMQ data-tree ownership repair must not restart RabbitMQ before live transfer probes")
    container_data_tree_task = tasks.split("Repair RabbitMQ persistent data tree ownership from container namespace", 1)[-1].split("- name:", 1)[0]
    for required in ("docker", "exec", "--user", "root", "/var/lib/rabbitmq", "chown -R rabbitmq:rabbitmq", "safe_metadata_only"):
        if required not in container_data_tree_task:
            errors.append(f"RabbitMQ container-namespace data repair missing: {required}")
    if "notify:" in container_data_tree_task or "Restart RabbitMQ" in container_data_tree_task:
        errors.append("RabbitMQ container-namespace data repair must not restart RabbitMQ before live transfer probes")
    if (
        "Wait for RabbitMQ diagnostics to pass" in tasks
        and "Repair RabbitMQ persistent data tree ownership from container namespace" in tasks
        and "Bootstrap RabbitMQ worker topology" in tasks
        and not (
            tasks.index("Wait for RabbitMQ diagnostics to pass")
            < tasks.index("Repair RabbitMQ persistent data tree ownership from container namespace")
            < tasks.index("Bootstrap RabbitMQ worker topology")
        )
    ):
        errors.append("RabbitMQ container-namespace data repair must run after diagnostics and before topology bootstrap")
    if "backend_rabbitmq_data_tree_permissions is changed" not in tasks:
        errors.append("RabbitMQ durable probe must run after broker data ownership repairs")
    if "backend_rabbitmq_container_data_tree_permissions is changed" not in tasks:
        errors.append("RabbitMQ durable probe must run after container-namespace broker data ownership repairs")
    if "backend_rabbitmq_legacy_probe_state_file is changed" not in tasks:
        errors.append("RabbitMQ durable probe must run after removing legacy broker-data probe state")
    if "Remove legacy probe state from RabbitMQ broker data directory" not in tasks:
        errors.append("RabbitMQ role must remove legacy root-owned probe state from broker data")
    if "ExecStartPre=/usr/bin/docker compose" in tasks:
        errors.append("RabbitMQ systemd unit must not require registry access during host restart")
    headroom_task = tasks.split("Capture root filesystem free bytes before RabbitMQ bootstrap", 1)[-1].split("- name:", 1)[0]
    if "check_mode: false" not in headroom_task:
        errors.append("RabbitMQ root filesystem headroom check must run in Ansible check mode")
    if "stdout_lines[-1]" in tasks:
        errors.append("RabbitMQ role must avoid negative-list indexing in Jinja assertions")
    if "backend_rabbitmq_runtime_manageable" not in tasks or "not ansible_check_mode" not in tasks:
        errors.append("RabbitMQ role must support check mode without managing missing services")
    if "Publish RabbitMQ durable restart probe message" not in tasks or "Verify RabbitMQ durable probe message after restart" not in tasks:
        errors.append("RabbitMQ role must run durable restart probe")
    if "Restart RabbitMQ" not in handlers or "when: not ansible_check_mode" not in handlers:
        errors.append("RabbitMQ handlers must be check-mode guarded")

    for required in (
        "backend_rabbitmq_topology_path: /usr/local/sbin/nutsnews-rabbitmq-topology",
        "backend_rabbitmq_topology_definition_path:",
        "backend_rabbitmq_topology_env_path:",
        "backend_rabbitmq_topology_enabled: true",
        "backend_rabbitmq_topology_transfer_probe_enabled: true",
        "backend_rabbitmq_topology_required_environment_names:",
    ):
        if required not in defaults:
            errors.append(f"RabbitMQ defaults missing topology setting: {required}")
    if defaults.count("RABBITMQ_") < 30:
        errors.append("RabbitMQ topology defaults must enumerate the protected service identity environment names")

    for required in (
        "Render RabbitMQ topology root-only credential environment",
        "Install RabbitMQ topology bootstrap",
        "Render RabbitMQ worker topology definition",
        "Bootstrap RabbitMQ worker topology",
        "Verify RabbitMQ worker topology drift",
        "Verify RabbitMQ least-privilege permissions",
        "Probe RabbitMQ retry and DLQ transfer routing",
        "Repair RabbitMQ private canary queue before apply canary",
        "repair-canary",
        "backend_rabbitmq_topology_bootstrap.stdout | from_json",
        "not ansible_check_mode",
    ):
        if required not in tasks:
            errors.append(f"RabbitMQ tasks missing topology bootstrap/check step: {required}")
    topology_env_task = tasks.split("Render RabbitMQ topology root-only credential environment", 1)[-1].split("- name:", 1)[0]
    if "no_log: true" not in topology_env_task or 'mode: "0600"' not in topology_env_task:
        errors.append("RabbitMQ topology credential env render must be root-only and no_log")

    if topology.get("tracking_issue") != 81:
        errors.append("RabbitMQ topology definition must track worker issue 81")
    if topology.get("vhost") != "nutsnews-worker-uplift":
        errors.append("RabbitMQ topology definition must render the approved production vhost")
    if topology.get("source", {}).get("contracts_commit") != "396d94dba76e3773ede50783463419501853b107":
        errors.append("RabbitMQ topology must pin the reviewed contracts commit")
    if topology.get("queue_type") != "classic":
        errors.append("RabbitMQ topology must apply the #79 durable classic queue decision")
    if "application_max_attempts_for_classic_queues" not in json.dumps(topology.get("delivery_behavior", {})):
        errors.append("RabbitMQ topology must record classic-queue delivery limit handling")
    if len(topology.get("exchanges", [])) != 4:
        errors.append("RabbitMQ topology must define main, retry, DLQ, and private canary exchanges")
    canary = topology.get("canary", {})
    if not isinstance(canary, dict) or canary.get("routing_key") != "worker.uplift.canary.v2":
        errors.append("RabbitMQ topology must define the isolated worker-uplift canary route")
    canary_queue = canary.get("queue", {}) if isinstance(canary, dict) else {}
    if not isinstance(canary_queue, dict) or canary_queue.get("name") != "worker.uplift.canary.v2":
        errors.append("RabbitMQ topology must define the isolated worker-uplift canary queue")
    if canary_queue.get("arguments", {}).get("x-max-length") != 10:
        errors.append("RabbitMQ canary queue must stay tightly bounded")
    if len(topology.get("routes", [])) != 7:
        errors.append("RabbitMQ topology must define seven worker routes")
    if len(topology.get("users", [])) != 16:
        errors.append("RabbitMQ topology must define break-glass, monitoring/canary, and fourteen route users")
    route_names = {route.get("stage") for route in topology.get("routes", [])}
    if route_names != {"fetch", "canonicalization", "enrichment", "approval", "translation", "persistence", "publication"}:
        errors.append(f"RabbitMQ topology route stages mismatch: {sorted(route_names)}")
    total_retry_queues = sum(len(route.get("retry_queues", [])) for route in topology.get("routes", []))
    if total_retry_queues != 21:
        errors.append("RabbitMQ topology must define three retry queues per route")
    if topology.get("queue_limits", {}).get("main", {}).get("x-overflow") != "reject-publish":
        errors.append("RabbitMQ topology main queues must reject publish on overflow")
    if topology.get("queue_limits", {}).get("retry", {}).get("x-max-length") != 1000:
        errors.append("RabbitMQ topology retry queues must use the #79 retry queue cap")
    if topology.get("queue_limits", {}).get("dlq", {}).get("x-message-ttl") != 1209600000:
        errors.append("RabbitMQ topology DLQ retention must be 14 days")
    if "guest" in topology_template:
        errors.append("RabbitMQ topology definition must not create a guest user")

    for required in (
        "def action_canary",
        "def action_drill",
        "confirm_delivery",
        "nutsnews_backend_rabbitmq_canary_success",
    ):
        if required not in probe:
            errors.append(f"RabbitMQ probe script missing canary behavior: {required}")

    for required in (
        "def live_drift",
        "def permission_matrix",
        "def action_repair_canary",
        "def action_probe_transfers",
        "ensure_guest_deleted",
        '"scope": "canary_queue_only"',
        '"operation": "ensure_only"',
        "production_queues_touched",
        "x-overflow",
        "reject-publish",
        "x-message-ttl",
        "x-dead-letter-exchange",
        "x-dead-letter-routing-key",
        "refusing transfer probe because queue is non-empty",
        "refusing transfer probe because queue has active consumers",
        "--skip-non-empty",
        "skipped_stages",
        "skipped_consumers",
        "skip_without_mutating_existing_messages",
    ):
        if required not in topology_script:
            errors.append(f"RabbitMQ topology script missing required behavior: {required}")
    transfer_probe_task = tasks.split("Probe RabbitMQ retry and DLQ transfer routing", 1)[-1].split("- name:", 1)[0]
    if "--skip-non-empty" not in transfer_probe_task:
        errors.append("RabbitMQ protected transfer probe must skip non-empty route queues without mutating backlog")
    readonly_block = topology_script.split("def live_drift", 1)[-1].split("def regex_allows", 1)[0]
    if 'client.request("PUT"' in readonly_block or 'client.request("POST"' in readonly_block or 'client.request("DELETE"' in readonly_block:
        errors.append("RabbitMQ topology live_drift must remain read-only")

    for required in (
        "NUTSNEWS_BACKEND_RABBITMQ_ENABLED",
        "RABBITMQ_VHOST",
        "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME",
        "RABBITMQ_ERLANG_COOKIE",
        "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD",
        "backend_rabbitmq_enabled",
        "backend_rabbitmq_admin_password",
        "backend_rabbitmq_erlang_cookie",
    ):
        if required not in protected:
            errors.append(f"protected workflow missing RabbitMQ mapping: {required}")
    for required_secret in ("--required-secret RABBITMQ_ERLANG_COOKIE", "--required-secret RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD"):
        if required_secret not in protected:
            errors.append(f"protected workflow missing deployment safety secret check: {required_secret}")
    for required_topology_name in (
        "RABBITMQ_MONITORING_USERNAME",
        "RABBITMQ_MONITORING_PASSWORD",
        "RABBITMQ_SCHEDULER_PUBLISHER_USERNAME",
        "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD",
        "RABBITMQ_FETCHER_CONSUMER_USERNAME",
        "RABBITMQ_FETCHER_CONSUMER_PASSWORD",
        "RABBITMQ_PUBLICATION_CONSUMER_USERNAME",
        "RABBITMQ_PUBLICATION_CONSUMER_PASSWORD",
        "backend_rabbitmq_topology_environment",
    ):
        if required_topology_name not in protected:
            errors.append(f"protected workflow missing RabbitMQ topology credential wiring: {required_topology_name}")
    for required_secret in (
        "--required-secret RABBITMQ_MONITORING_PASSWORD",
        "--required-secret RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD",
        "--required-secret RABBITMQ_PUBLICATION_CONSUMER_PASSWORD",
    ):
        if required_secret not in protected:
            errors.append(f"protected workflow missing RabbitMQ topology secret check: {required_secret}")

    if "rabbitmq_health" not in safety or "rabbitmq_post_apply_blockers" not in safety:
        errors.append("deployment safety must classify RabbitMQ post-apply health")
    for required in (
        "RABBITMQ_HOST_RESTART_QUEUE",
        "rabbitmq_probe_command",
        "run_rabbitmq_probe_action",
        "rabbitmq_host_reboot_probe",
        "RabbitMQ host-restart probe publish failed",
        "/var/lib/nutsnews/rabbitmq-probes/host-restart-probe.json",
    ):
        if required not in controlled_maintenance:
            errors.append(f"controlled maintenance reboot path missing RabbitMQ host-restart probe support: {required}")

    services = {service.get("id"): service for service in backup_matrix.get("services", [])}
    rabbitmq_backup = services.get("rabbitmq_broker_state")
    if not rabbitmq_backup:
        errors.append("backup matrix must include rabbitmq_broker_state")
    else:
        data_sources = set(rabbitmq_backup.get("data_sources", []))
        if "/var/lib/nutsnews/rabbitmq" in data_sources:
            errors.append("RabbitMQ backup matrix must exclude live persistent data dir from normal Restic paths")
        if "/var/lib/nutsnews/rabbitmq-recovery" not in data_sources:
            errors.append("RabbitMQ backup matrix must include recovery metadata state dir")
        if "/etc/nutsnews-rabbitmq/rabbitmq.env" in data_sources:
            errors.append("RabbitMQ backup matrix must not include secret env file")
        backup_method = rabbitmq_backup.get("backup_method", "")
        restore_method = rabbitmq_backup.get("restore_method", "")
        if "live_message_store_excluded" not in backup_method:
            errors.append("RabbitMQ backup method must exclude live message-store snapshots")
        if "PostgreSQL" not in restore_method and "postgresql" not in restore_method:
            errors.append("RabbitMQ restore method must mention PostgreSQL recovery source")
        if "stopped_volume_restore_only_from_quiesced_snapshot" not in restore_method:
            errors.append("RabbitMQ restore method must constrain message-store restore to quiesced snapshots")

    if "python3 scripts/validate_worker_uplift_rabbitmq_provisioning.py" not in checks:
        errors.append("Backend Checks must run RabbitMQ provisioning validator")
    if "host restart" not in runbook.lower() or "durable probe" not in runbook.lower():
        errors.append("RabbitMQ provisioning runbook must document durable probe and host restart verification")
    if "Backend Controlled Maintenance" not in runbook or "backend-controlled-maintenance-report" not in runbook:
        errors.append("RabbitMQ provisioning runbook must route host restart verification through controlled maintenance")
    if "legacy Cloudflare Worker" not in runbook:
        errors.append("RabbitMQ provisioning runbook must keep legacy Worker guardrail visible")
    if "--skip-non-empty" not in runbook or "skipped_stages" not in runbook or "skipped_consumers" not in runbook:
        errors.append("RabbitMQ provisioning runbook must document non-empty and active-consumer transfer-probe skip reporting")

    if probe.count("RABBITMQ_DEFAULT_PASS") < 1:
        errors.append("RabbitMQ probe must read credentials from env file")
    if "argparse" not in probe or "Authorization" not in probe:
        errors.append("RabbitMQ probe must use local management API without password args")
    if "delete_probe_queue_if_present" not in probe or "ignored_statuses=(404,)" not in probe:
        errors.append("RabbitMQ probe publish must delete its own stale probe queue before declaring and publishing")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("worker-uplift RabbitMQ provisioning is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
