#!/usr/bin/env python3
"""Validate the backend service-aware backup matrix."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "backend-backup-service-matrix.json"
REQUIRED_ALERT_KINDS = {
    "backup_failure",
    "stale_backup",
    "unverified_latest_snapshot",
    "storage_quota_warning",
}
REQUIRED_SERVICES = {
    "host_baseline_config",
    "caddy_reverse_proxy",
    "ops_dashboard_state",
    "backup_status_metadata",
    "docker_volumes",
    "rabbitmq_broker_state",
    "postgresql_data",
    "runtime_secrets",
}


def main() -> int:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []

    if matrix.get("version") != 1:
        errors.append("version must be 1")
    if matrix.get("repository") != "ramideltoro/nutsnews-backend":
        errors.append("repository must be ramideltoro/nutsnews-backend")
    if not str(matrix.get("status_file_dir", "")).startswith("/var/lib/nutsnews/"):
        errors.append("status_file_dir must be under /var/lib/nutsnews")

    alert_kinds = set(matrix.get("alert_kinds", []))
    missing_alerts = sorted(REQUIRED_ALERT_KINDS - alert_kinds)
    if missing_alerts:
        errors.append(f"missing alert kinds: {', '.join(missing_alerts)}")

    restic = matrix.get("restic", {})
    for field in ("repository_secret", "password_secret", "provider_variable", "provider_secret_sets"):
        if field not in restic:
            errors.append(f"restic missing {field}")

    seen_ids: set[str] = set()
    for service in matrix.get("services", []):
        service_id = service.get("id", "")
        if not service_id:
            errors.append("service missing id")
            continue
        if service_id in seen_ids:
            errors.append(f"duplicate service id: {service_id}")
        seen_ids.add(service_id)

        for field in (
            "title",
            "current_state",
            "criticality",
            "data_sources",
            "backup_method",
            "restore_method",
            "verification_method",
            "exclusion_rationale",
        ):
            if field not in service:
                errors.append(f"{service_id} missing {field}")

        if service.get("backup_method") == "excluded_from_restic" and not service.get("exclusion_rationale"):
            errors.append(f"{service_id} excluded from restic without rationale")
        if service.get("criticality") in {"critical", "future_critical"} and not service.get("restore_method"):
            errors.append(f"{service_id} critical service missing restore method")
        if service.get("criticality") in {"critical", "future_critical"} and not service.get("verification_method"):
            errors.append(f"{service_id} critical service missing verification method")

    missing_services = sorted(REQUIRED_SERVICES - seen_ids)
    if missing_services:
        errors.append(f"missing required services: {', '.join(missing_services)}")

    services = {service.get("id"): service for service in matrix.get("services", []) if service.get("id")}
    rabbitmq = services.get("rabbitmq_broker_state")
    if not rabbitmq:
        errors.append("rabbitmq_broker_state service is required")
    else:
        data_sources = set(rabbitmq.get("data_sources", []))
        if "/var/lib/nutsnews/rabbitmq" in data_sources:
            errors.append("rabbitmq_broker_state must exclude live /var/lib/nutsnews/rabbitmq from normal Restic paths")
        if "/var/lib/nutsnews/rabbitmq-recovery" not in data_sources:
            errors.append("rabbitmq_broker_state must include /var/lib/nutsnews/rabbitmq-recovery recovery metadata")
        for secret_path in ("/etc/nutsnews-rabbitmq/rabbitmq.env", "/etc/nutsnews-rabbitmq/topology.env"):
            if secret_path in data_sources:
                errors.append(f"rabbitmq_broker_state must exclude secret env file {secret_path}")
        backup_method = str(rabbitmq.get("backup_method", ""))
        restore_method = str(rabbitmq.get("restore_method", ""))
        verification_method = str(rabbitmq.get("verification_method", ""))
        rationale = str(rabbitmq.get("exclusion_rationale", ""))
        if "live_message_store_excluded" not in backup_method:
            errors.append("rabbitmq_broker_state backup_method must record live_message_store_excluded")
        if "postgresql" not in restore_method.lower() or "outbox" not in restore_method.lower():
            errors.append("rabbitmq_broker_state restore_method must use PostgreSQL outbox/reconciliation as source of truth")
        if "stopped_volume_restore_only_from_quiesced_snapshot" not in restore_method:
            errors.append("rabbitmq_broker_state restore_method must constrain message-store restores to quiesced snapshots")
        for required in ("definition_export", "clean_rebuild_drill", "stopped_volume_restore_drill"):
            if required not in verification_method:
                errors.append(f"rabbitmq_broker_state verification_method missing {required}")
        if "running-node message-store copies can be inconsistent" not in rationale:
            errors.append("rabbitmq_broker_state exclusion_rationale must document RabbitMQ live-copy risk")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Validated {len(seen_ids)} backup service matrix entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
