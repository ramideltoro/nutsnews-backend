#!/usr/bin/env python3
"""Validate backend Grafana producer-only handoff guardrails."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "docs" / "backend-grafana-handoff.json"
SPEC = ROOT / "grafana" / "backend-metrics" / "dashboards.json"
INVENTORY = ROOT / "docs" / "backend-credential-inventory.json"
GRAFANA_WORKFLOW = ROOT / ".github" / "workflows" / "backend-grafana-metrics.yml"
CREDENTIAL_WORKFLOW = ROOT / ".github" / "workflows" / "backend-credential-readiness.yml"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "backend-checks.yml"
PROVISION_SCRIPT = ROOT / "scripts" / "provision_grafana_metrics.py"
METRICS_EXPORTER = ROOT / "ansible" / "roles" / "backend_baseline" / "files" / "nutsnews_metrics_textfile.py"
WORKFLOW_DIR = ROOT / ".github" / "workflows"

MANAGEMENT_NAMES = {"GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"}
TELEMETRY_NAMES = {
    "GRAFANA_CLOUD_PROMETHEUS_URL",
    "GRAFANA_CLOUD_PROMETHEUS_USERNAME",
    "GRAFANA_CLOUD_PROMETHEUS_PASSWORD",
    "GRAFANA_CLOUD_LOKI_URL",
    "GRAFANA_CLOUD_LOKI_USERNAME",
    "GRAFANA_CLOUD_LOKI_PASSWORD",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def workflow_texts() -> list[tuple[str, str]]:
    return [(path.name, text(path)) for path in sorted(WORKFLOW_DIR.glob("*.yml"))]


def main() -> int:
    errors: list[str] = []
    handoff = load_json(HANDOFF)
    spec = load_json(SPEC)
    inventory = load_json(INVENTORY)
    grafana_workflow = text(GRAFANA_WORKFLOW)
    credential_workflow = text(CREDENTIAL_WORKFLOW)
    checks_workflow = text(CHECKS_WORKFLOW)
    provision_script = text(PROVISION_SCRIPT)
    metrics_exporter = text(METRICS_EXPORTER)
    workflows = workflow_texts()

    if handoff.get("resource_management_owner") != "ramideltoro/nutsnews-infra":
        errors.append("handoff must name nutsnews-infra as Grafana resource owner")
    if handoff.get("backend_role") != "telemetry_producer_collector":
        errors.append("backend role must be telemetry_producer_collector")
    if handoff.get("legacy_provisioning_retired") is not True:
        errors.append("legacy provisioning must be retired")

    removed = set(handoff.get("production_backend_management_credentials_removed", []))
    if removed != MANAGEMENT_NAMES:
        errors.append("handoff must record removed Grafana management credential names")
    retained = set(handoff.get("telemetry_write_credentials_retained", []))
    if retained != TELEMETRY_NAMES:
        errors.append("handoff must preserve exactly the backend telemetry write credential names")

    replacements = handoff.get("infra_replacements", {})
    if replacements.get("folder", {}).get("uid") != spec.get("folder", {}).get("uid"):
        errors.append("handoff folder UID must match backend catalog")
    if replacements.get("folder", {}).get("tofu_address") != "grafana_folder.backend_observability":
        errors.append("handoff folder must point at infra OpenTofu address")

    spec_dashboard_uids = {dashboard.get("uid") for dashboard in spec.get("dashboards", [])}
    handoff_dashboard_uids = {dashboard.get("uid") for dashboard in replacements.get("dashboards", [])}
    if handoff_dashboard_uids != spec_dashboard_uids:
        errors.append("handoff dashboard UID set must match backend Grafana catalog")
    if len(handoff_dashboard_uids) != 10:
        errors.append("handoff must preserve all 10 backend dashboards")
    for dashboard in replacements.get("dashboards", []):
        uid = dashboard.get("uid", "")
        if f'grafana_dashboard.backend_observability["{uid}"]' != dashboard.get("tofu_address"):
            errors.append(f"dashboard {uid} has wrong OpenTofu address")

    spec_alert_uids = {alert.get("uid") for alert in spec.get("alerts", [])}
    handoff_alert_uids = set(replacements.get("alert_uids", []))
    if handoff_alert_uids != spec_alert_uids:
        errors.append("handoff alert UID set must match backend Grafana catalog")
    if len(handoff_alert_uids) != 11:
        errors.append("handoff must preserve all 11 backend alert UIDs")
    if replacements.get("alert_rule_group", {}).get("tofu_address") != "grafana_rule_group.backend_guardrails":
        errors.append("handoff alert group must point at infra OpenTofu address")

    datasource_types = {item.get("type") for item in replacements.get("datasource_dependencies", [])}
    if datasource_types != {"prometheus", "loki"}:
        errors.append("handoff must record Prometheus and Loki datasource dependencies")
    datasource_text = json.dumps(replacements.get("datasource_dependencies", []), sort_keys=True)
    for variable in ("TF_VAR_prometheus_datasource_uid", "TF_VAR_loki_datasource_uid"):
        if variable not in datasource_text:
            errors.append(f"handoff datasource dependencies missing {variable}")

    inventory_names = {
        secret.get("name")
        for group in inventory.get("secret_groups", [])
        for key in ("secrets", "conditional_secrets")
        for secret in group.get(key, [])
    }
    if MANAGEMENT_NAMES & inventory_names:
        errors.append("backend credential inventory must not include Grafana management credentials")
    if not TELEMETRY_NAMES.issubset(inventory_names):
        errors.append("backend credential inventory must retain telemetry write credentials")

    workflow_forbidden = [
        ("backend Grafana workflow", grafana_workflow),
        ("credential readiness workflow", credential_workflow),
    ]
    for name, body in workflow_forbidden:
        for credential in MANAGEMENT_NAMES:
            if credential in body:
                errors.append(f"{name} must not reference {credential}")

    if "environment: production-backend" in grafana_workflow:
        errors.append("backend Grafana catalog validation workflow must not use production-backend")
    if re.search(r"(?m)^\s+- apply\s*$", grafana_workflow) or re.search(r"(?m)^\s+- verify\s*$", grafana_workflow):
        errors.append("backend Grafana workflow must not expose apply/verify choices")
    if "--apply" in grafana_workflow or "--verify" in grafana_workflow:
        errors.append("backend Grafana workflow must not call apply/verify")
    for name, body in workflows:
        for credential in MANAGEMENT_NAMES:
            if credential in body:
                errors.append(f"{name} must not reference {credential}")
        if re.search(r"provision_grafana_metrics\.py[^\n]*(--apply|--verify)", body):
            errors.append(f"{name} must not run backend Grafana apply/verify")
        if "api/v1/provisioning" in body or "api/dashboards/db" in body or "api/folders" in body:
            errors.append(f"{name} must not call Grafana resource provisioning APIs")

    if "OpenTofu. Use this backend script in --check mode only." not in provision_script:
        errors.append("backend Grafana script must explicitly reject apply/verify")
    for credential in MANAGEMENT_NAMES:
        if credential in provision_script:
            errors.append(f"backend Grafana script must not reference {credential}")

    for fragment in (
        "available = identity_valid and expected_valid and mode_valid and pairing_valid",
        'if available and raw_mode == "production":',
        'ingestion_owner = "worker-uplift"',
        'write_gate = "enabled"',
        'elif available and raw_mode == "shadow":',
        'ingestion_owner = "legacy-worker"',
        'write_gate = "disabled"',
        'ingestion_owner = "unknown"',
        'write_gate = "unknown"',
        '"nutsnews_backend_worker_uplift_deployment_info"',
        '"ingestion_owner": ingestion_owner',
        '"write_gate": write_gate',
    ):
        if fragment not in metrics_exporter:
            errors.append(f"backend ownership telemetry missing fragment: {fragment}")

    if "python3 scripts/validate_backend_grafana_handoff.py" not in checks_workflow:
        errors.append("backend checks must run the Grafana handoff validator")

    docs_text = " ".join(
        path.read_text(encoding="utf-8")
        for path in [
            ROOT / "runbooks/MONITORING_BASELINE.md",
            ROOT / "runbooks/CREDENTIAL_BOOTSTRAP.md",
            ROOT / "runbooks/ABUSE_PROTECTION.md",
        ]
    )
    for phrase in (
        "ramideltoro/nutsnews-infra",
        "telemetry producer",
        "Prometheus/Loki write",
        "backend-grafana-metrics.yml --check",
    ):
        if phrase not in docs_text:
            errors.append(f"backend runbooks missing phrase: {phrase}")
    if "Backend Grafana Observability" in docs_text:
        errors.append("backend runbooks must not reference the retired Grafana mutation workflow")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("backend Grafana handoff is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
