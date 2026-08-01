#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts import provision_grafana_metrics


METRICS_PATH = Path("ansible/roles/backend_baseline/files/nutsnews_metrics_textfile.py")
GRAFANA_SPEC = Path("grafana/backend-metrics/dashboards.json")
ALLOY_TEMPLATE = Path("ansible/roles/backend_baseline/templates/alloy-config.alloy.j2")
METRICS_TASKS = Path("ansible/roles/backend_baseline/tasks/metrics.yml")
NEW_RELIC_METRICS = Path("ansible/roles/backend_baseline/files/nutsnews_newrelic_job_metrics.py")
NEW_RELIC_METRICS_TASKS = Path("ansible/roles/backend_baseline/tasks/newrelic_metrics.yml")
PROTECTED_APPLY_WORKFLOW = Path(".github/workflows/protected-backend-ansible-apply.yml")
RABBITMQ_COMPOSE = Path("ansible/roles/backend_rabbitmq/templates/rabbitmq-compose.yml.j2")
WORKER_COMPOSE = Path("ansible/roles/backend_worker_runtime/templates/worker-uplift-compose.yml.j2")
WORKER_LOGS_CHECK = Path("scripts/backend_worker_uplift_logs_check.py")
WORKER_LOGS_WORKFLOW = Path(".github/workflows/backend-worker-uplift-logs-check.yml")


def load_metrics_module():
    spec = importlib.util.spec_from_file_location("nutsnews_metrics_textfile", METRICS_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_newrelic_metrics_module():
    spec = importlib.util.spec_from_file_location("nutsnews_newrelic_job_metrics", NEW_RELIC_METRICS)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_worker_logs_check_module():
    spec = importlib.util.spec_from_file_location("backend_worker_uplift_logs_check", WORKER_LOGS_CHECK)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackendMetricsTests(unittest.TestCase):
    def test_worker_uplift_ownership_gate_is_host_owned_and_fail_closed(self):
        metrics = load_metrics_module()
        with mock.patch.dict(
            "os.environ",
            {
                "NUTSNEWS_WORKER_UPLIFT_EXPECTED_ACTIVE": "1",
                "NUTSNEWS_WORKER_UPLIFT_DEPLOYMENT_MODE": "production",
                "NUTSNEWS_DEPLOYMENT_ENVIRONMENT": "production",
                "NUTSNEWS_TELEMETRY_HOST": "backend.nutsnews.com",
            },
            clear=False,
        ):
            output = "\n".join(metrics.worker_uplift_ownership_metric_lines())
        self.assertIn("nutsnews_backend_worker_uplift_expected_active 1", output)
        self.assertIn("nutsnews_backend_worker_uplift_ownership_available 1", output)
        self.assertNotIn("exported_host", output)
        self.assertIn('mode="production"', output)

        with mock.patch.dict(
            "os.environ",
            {
                "NUTSNEWS_WORKER_UPLIFT_EXPECTED_ACTIVE": "1",
                "NUTSNEWS_WORKER_UPLIFT_DEPLOYMENT_MODE": "shadow",
            },
            clear=False,
        ):
            invalid = "\n".join(metrics.worker_uplift_ownership_metric_lines())
        self.assertIn("nutsnews_backend_worker_uplift_ownership_available", invalid)
        self.assertIn(" 0", invalid)

    def test_worker_uplift_deployed_identity_is_exact_bounded_and_fail_closed(self):
        metrics = load_metrics_module()
        services = []
        for index, name in enumerate(metrics.WORKER_UPLIFT_STAGES, start=1):
            revision = f"{index:x}" * 40
            digest = f"sha256:{index:x}" + ("a" * 63)
            services.append(
                {
                    "name": name,
                    "image": f"ghcr.io/ramideltoro/{name}@{digest}",
                    "image_tag": revision,
                    "service_version": "0.1.0",
                    "build_revision": revision,
                    "image_digest": digest,
                    "provenance": {"subject_digest": digest},
                }
            )
        manifest = {
            "schema_version": 1,
            "generated_by": "backend_worker_runtime",
            "services": services,
        }
        running_label_sets = [
            {
                "com.nutsnews.service": service["name"],
                "com.nutsnews.service_version": service["service_version"],
                "com.nutsnews.revision": service["build_revision"],
                "com.nutsnews.image_digest": service["image_digest"],
                "__config_image": service["image"],
            }
            for service in services
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "services.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            output = "\n".join(
                metrics.worker_uplift_deployed_identity_metric_lines(path, running_label_sets)
            )
            self.assertIn("nutsnews_backend_worker_uplift_deployed_identity_available 1", output)
            self.assertEqual(output.count("nutsnews_backend_worker_uplift_deployed_service_info{"), 8)
            self.assertIn('worker_service="scheduler"', output)
            self.assertIn('service_version="0.1.0"', output)
            self.assertIn('revision="1111111111111111111111111111111111111111"', output)
            self.assertIn('image_digest="sha256:1', output)

            invalid_running_cases = {
                "image mismatch": [
                    {**item, "__config_image": "ghcr.io/ramideltoro/wrong@sha256:" + ("f" * 64)}
                    if index == 0
                    else dict(item)
                    for index, item in enumerate(running_label_sets)
                ],
                "unknown service": [
                    {**item, "com.nutsnews.service": "unknown"}
                    if index == 0
                    else dict(item)
                    for index, item in enumerate(running_label_sets)
                ],
                "mismatched replica": [
                    *[dict(item) for item in running_label_sets],
                    {**running_label_sets[1], "com.nutsnews.revision": "f" * 40},
                ],
                "too many replicas": [
                    *[dict(item) for item in running_label_sets],
                    *[dict(running_label_sets[0]) for _ in range(3)],
                ],
                "missing service": [dict(item) for item in running_label_sets[:-1]],
            }
            for case, observed in invalid_running_cases.items():
                with self.subTest(case=case):
                    unavailable = "\n".join(
                        metrics.worker_uplift_deployed_identity_metric_lines(path, observed)
                    )
                    self.assertIn(
                        "nutsnews_backend_worker_uplift_deployed_identity_available 0",
                        unavailable,
                    )
                    self.assertNotIn(
                        "nutsnews_backend_worker_uplift_deployed_service_info{",
                        unavailable,
                    )

            manifest["services"][0]["build_revision"] = "unknown"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            invalid = "\n".join(
                metrics.worker_uplift_deployed_identity_metric_lines(path, running_label_sets)
            )
        self.assertIn("nutsnews_backend_worker_uplift_deployed_identity_available 0", invalid)
        self.assertNotIn("nutsnews_backend_worker_uplift_deployed_service_info{", invalid)

    def test_grafana_dashboard_spec_passes_guardrails(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        self.assertEqual(provision_grafana_metrics.validate_spec(spec), [])
        self.assertEqual(spec["folder"]["uid"], "nutsnews-backend-ops")
        self.assertGreaterEqual(len(spec["dashboards"]), 9)
        self.assertGreaterEqual(len(spec["alerts"]), 8)

        logs_dashboard = next(item for item in spec["dashboards"] if item["uid"] == "nutsnews-backend-logs")
        self.assertTrue(logs_dashboard["panels"])
        self.assertTrue(all(panel.get("datasource") == "loki" for panel in logs_dashboard["panels"]))
        self.assertTrue(any(alert.get("datasource") == "loki" for alert in spec["alerts"]))

    def test_backend_grafana_cli_apply_is_retired(self):
        with mock.patch("sys.argv", ["provision_grafana_metrics.py", "--apply"]):
            with mock.patch("sys.stderr") as stderr:
                self.assertEqual(provision_grafana_metrics.main(), 2)
        stderr_text = "".join(call.args[0] for call in stderr.write.call_args_list if call.args)
        self.assertIn("ramideltoro/nutsnews-infra", stderr_text)

    def test_grafana_dashboard_spec_rejects_high_cardinality_log_labels(self):
        spec = {
            "folder": {"uid": "test", "title": "Test"},
            "guardrails": {"max_dashboards": 10, "max_panels_per_dashboard": 12, "max_total_panels": 80, "max_unique_queries": 100},
            "dashboards": [
                {
                    "uid": "bad",
                    "title": "Bad",
                    "panels": [{"title": "Bad", "datasource": "loki", "expr": "{host=\"backend.nutsnews.com\",request_id=\"abc\"}"}],
                }
            ],
        }
        errors = provision_grafana_metrics.validate_spec(spec)
        self.assertIn("high-cardinality label request_id", "\n".join(errors))

    def test_grafana_alert_model_uses_expression_condition(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        alert = next(item for item in spec["alerts"] if item["uid"] == "nn-backend-root-disk-warning")
        model = provision_grafana_metrics.alert_rule_model(
            alert,
            spec["folder"]["uid"],
            spec["alert_group"]["name"],
            {"prometheus": "prometheus-uid", "loki": "loki-uid"},
        )
        self.assertEqual(model["folderUID"], "nutsnews-backend-ops")
        self.assertEqual(model["ruleGroup"], "NutsNews Backend Guardrails")
        self.assertEqual(model["condition"], "B")
        self.assertEqual(model["data"][0]["datasourceUid"], "prometheus-uid")
        self.assertEqual(model["data"][1]["datasourceUid"], "-100")
        self.assertEqual(model["labels"]["severity"], "warning")

    def test_grafana_alert_uids_fit_api_limit(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        for alert in spec["alerts"]:
            self.assertLessEqual(len(alert["uid"]), provision_grafana_metrics.MAX_GRAFANA_ALERT_UID_LENGTH)

    def test_abuse_detection_alerts_are_report_only_and_low_cardinality(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        alerts = {alert["uid"]: alert for alert in spec["alerts"]}
        for uid in ("nn-backend-ssh-auth-spike", "nn-backend-fail2ban-ban-events"):
            alert = alerts[uid]
            self.assertEqual(alert["datasource"], "loki")
            self.assertEqual(alert["service"], "security")
            self.assertEqual(alert["severity"], "warning")
            self.assertEqual(alert["no_data_state"], "OK")
            self.assertIn("runbooks/ABUSE_PROTECTION.md", alert["runbook_url"])
            for label in provision_grafana_metrics.HIGH_CARDINALITY_LABELS:
                self.assertNotIn(f"{label}=", alert["expr"])
                self.assertNotIn(f"{label}=~", alert["expr"])

    def test_logs_dashboard_surfaces_abuse_detection_without_ip_labels(self):
        spec = provision_grafana_metrics.load_spec(GRAFANA_SPEC)
        logs_dashboard = next(item for item in spec["dashboards"] if item["uid"] == "nutsnews-backend-logs")
        titles = {panel["title"]: panel for panel in logs_dashboard["panels"]}
        self.assertIn("SSH Auth Failure Volume", titles)
        self.assertIn("Fail2ban Ban Events", titles)
        for title in ("SSH Auth Failure Volume", "Fail2ban Ban Events"):
            expr = titles[title]["expr"]
            self.assertIn('service="security"', expr)
            self.assertNotIn("ip=", expr)
            self.assertNotIn("path=", expr)
            self.assertNotIn("user_id=", expr)

    def test_loki_remote_write_url_is_normalized_for_query_datasource(self):
        self.assertEqual(
            provision_grafana_metrics.loki_query_url("https://logs.example.net/loki/api/v1/push"),
            "https://logs.example.net",
        )

    def test_alert_history_loki_datasource_is_not_selected_for_backend_logs(self):
        self.assertTrue(provision_grafana_metrics.datasource_is_alert_history({"uid": "grafanacloud-alert-state-history"}))
        self.assertFalse(provision_grafana_metrics.datasource_is_alert_history({"uid": "grafanacloud-loki", "name": "Backend Logs"}))

    def test_textfile_metric_label_escaping(self):
        metrics = load_metrics_module()
        rendered = metrics.metric("nutsnews_test", 1, {"unit": 'a"b\\c'})
        self.assertEqual(rendered, 'nutsnews_test{unit="a\\"b\\\\c"} 1')

    def test_metrics_tasks_skip_alloy_package_when_repo_is_new_in_check_mode(self):
        task = METRICS_TASKS.read_text(encoding="utf-8")
        self.assertIn("backend_metrics_alloy_manageable", task)
        self.assertIn("when: backend_metrics_alloy_manageable | bool", task)

    def test_alloy_loki_template_has_redaction_and_cardinality_guardrails(self):
        template = ALLOY_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('loki.write "grafana_cloud_loki"', template)
        self.assertIn('sys.env("GRAFANA_CLOUD_LOKI_PASSWORD")', template)
        self.assertIn('stage.drop', template)
        self.assertIn('stage.replace', template)
        self.assertIn('stage.truncate', template)
        self.assertIn('stage.label_keep', template)
        self.assertIn("request_id", template)
        self.assertIn("stage.structured_metadata", template)
        label_keep = template.split("stage.label_keep", 1)[1].split("}", 1)[0]
        for label in ("deployment_environment", "service", "service_version", "host", "source", "severity"):
            self.assertIn(f'"{label}"', label_keep)
        for label in ("request_id", "message_id", "correlation_id", "trace_id", "article_id", "feed_id", "pipeline_run_id", "idempotency_key"):
            self.assertNotIn(f'"{label}"', label_keep)
        self.assertNotIn("loki.source.docker", template)
        self.assertNotIn("/var/run/docker.sock", template)

    def test_alloy_rabbitmq_metrics_are_bounded_and_private(self):
        defaults = Path("ansible/roles/backend_baseline/defaults/main.yml").read_text(encoding="utf-8")
        template = ALLOY_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("backend_metrics_rabbitmq_enabled: false", defaults)
        self.assertIn("backend_metrics_rabbitmq_target: 127.0.0.1:15692", defaults)
        self.assertIn("backend_metrics_rabbitmq_sample_limit: 1200", defaults)
        self.assertIn("queue_coarse_metrics", defaults)
        self.assertIn("queue_consumer_count", defaults)
        self.assertIn("queue_delivery_metrics", defaults)
        self.assertIn('prometheus.exporter.self "alloy"', template)
        self.assertIn('prometheus.scrape "rabbitmq_aggregate"', template)
        self.assertIn('prometheus.scrape "rabbitmq_detailed"', template)
        self.assertIn('metrics_path    = "/metrics/detailed"', template)
        self.assertIn('"queue"  = [{{ backend_metrics_rabbitmq_queue_regex | to_json }}]', template)
        self.assertIn('source_labels = ["queue"]', template)
        self.assertIn("regex         = {{ backend_metrics_rabbitmq_queue_regex | to_json }}", template)
        self.assertIn("backend_metrics_rabbitmq_queue_regex", template)
        self.assertIn("sample_limit", template)
        self.assertIn("label_limit", template)
        self.assertIn("labeldrop", template)
        self.assertIn("labelkeep", template)
        self.assertIn("service_namespace", template)
        rabbitmq_metrics = template.split('prometheus.relabel "rabbitmq_aggregate"', 1)[1].split("{% endif %}", 1)[0]
        self.assertNotIn("request_id", rabbitmq_metrics)

    def test_alloy_log_access_is_least_privilege(self):
        task = METRICS_TASKS.read_text(encoding="utf-8")
        self.assertIn("Allow Alloy to read journal and adm-owned log files", task)
        self.assertIn("- adm", task)
        self.assertIn("- systemd-journal", task)
        self.assertNotIn("chmod 666", task)
        self.assertNotIn("become_user: root", task)

    def test_protected_apply_wires_loki_secret_names(self):
        workflow = PROTECTED_APPLY_WORKFLOW.read_text(encoding="utf-8")
        for name in ("GRAFANA_CLOUD_LOKI_URL", "GRAFANA_CLOUD_LOKI_USERNAME", "GRAFANA_CLOUD_LOKI_PASSWORD"):
            self.assertIn(name, workflow)
        self.assertIn('"backend_logs_enabled"] = True', workflow)

    def test_worker_uplift_container_logs_are_journald_and_bounded(self):
        defaults = Path("ansible/roles/backend_baseline/defaults/main.yml").read_text(encoding="utf-8")
        template = ALLOY_TEMPLATE.read_text(encoding="utf-8")
        rabbitmq_compose = RABBITMQ_COMPOSE.read_text(encoding="utf-8")
        worker_compose = WORKER_COMPOSE.read_text(encoding="utf-8")

        self.assertIn("backend_logs_line_drop_max_size: 8KiB", defaults)
        self.assertIn("backend_logs_worker_uplift_traces_enabled: false", defaults)
        for service in ("rabbitmq", "scheduler", "fetcher", "canonicalizer", "enrichment", "approval", "translation", "persistence", "publication"):
            self.assertIn(f"service: {service}", defaults)
        self.assertIn("nutsnews-rabbitmq-canary.timer", load_metrics_module().SERVICES)
        self.assertIn("CONTAINER_TAG={{ source.tag }}", template)
        self.assertIn('source                 = "container"', template)
        self.assertIn("stage.json", template)
        self.assertIn("stage.structured_metadata", template)
        self.assertIn("correlationId", template)
        self.assertIn("idempotencyKey", template)
        self.assertIn("traceparent", template)
        self.assertIn('drop_counter_reason = "debug_trace_log_level"', template)
        label_keep = template.split("stage.label_keep", 1)[1].split("}", 1)[0]
        for allowed in ("deployment_environment", "service", "service_version", "host", "source", "severity"):
            self.assertIn(f'"{allowed}"', label_keep)
        for metadata in (
            "queue",
            "outcome",
            "revision",
            "image_digest",
            "request_id",
            "message_id",
            "idempotency_key",
            "trace_id",
            "article_id",
            "feed_id",
            "pipeline_run_id",
        ):
            self.assertIn(f"{metadata}", template.split("stage.structured_metadata", 1)[1])
        for forbidden in ("message_id", "idempotency", "trace_id", "correlation_id", "token", "secret", "prompt", "model_output"):
            self.assertNotIn(f'"{forbidden}"', label_keep)
        self.assertNotIn("loki.source.docker", template)
        self.assertNotIn("/var/run/docker.sock", template)
        self.assertNotIn("otelcol.receiver.otlp", template)
        self.assertIn("driver: journald", rabbitmq_compose)
        self.assertIn('tag: "nutsnews-worker-uplift-rabbitmq"', rabbitmq_compose)
        self.assertIn("driver: journald", worker_compose)
        self.assertIn('tag: "nutsnews-worker-uplift-{{ service.name }}"', worker_compose)
        for identity_label in (
            "com.nutsnews.service_version",
            "com.nutsnews.revision",
            "com.nutsnews.image_digest",
        ):
            self.assertIn(identity_label, worker_compose)
        self.assertNotIn("com.nutsnews.version", worker_compose)

    def test_worker_uplift_logs_check_workflow_is_read_only_and_safe(self):
        workflow = WORKER_LOGS_WORKFLOW.read_text(encoding="utf-8")
        script = WORKER_LOGS_CHECK.read_text(encoding="utf-8")

        self.assertIn("Backend Worker-Uplift Logs Check", workflow)
        self.assertIn("environment: production-backend", workflow)
        self.assertIn("require_loki_data", workflow)
        self.assertIn("GRAFANA_CLOUD_LOKI_URL", workflow)
        self.assertIn("scripts/backend_worker_uplift_logs_check.py", workflow)
        self.assertIn("safe_metadata_only", script)
        self.assertIn("loki_rabbitmq_query", script)
        self.assertIn("trace_export_deferred", script)
        self.assertIn("credential_error", script)
        self.assertNotIn("docker logs", script)
        self.assertNotIn("journalctl -o cat", script)

    def test_worker_uplift_loki_query_url_is_derived_from_push_endpoint(self):
        logs_check = load_worker_logs_check_module()
        self.assertEqual(
            logs_check.derive_loki_query_range_url("https://logs.example.net/loki/api/v1/push"),
            "https://logs.example.net/loki/api/v1/query_range",
        )

    def test_worker_uplift_required_loki_check_requires_all_eight_identity_streams(self):
        logs_check = load_worker_logs_check_module()

        class Args:
            loki_url = "https://logs.example.net/loki/api/v1/push"
            loki_username = "123"
            loki_password = "secret"
            query_hours = 6
            timeout = 5
            require_loki_data = True

        def query_result(_url, _username, _password, query, **_kwargs):
            if 'service="translation"' in query:
                return {
                    "status": "critical",
                    "summary": "stream_count=0, line_count=0",
                    "credential_error": False,
                }
            return {
                "status": "healthy",
                "summary": "stream_count=1, line_count=1",
                "credential_error": False,
            }

        with (
            mock.patch.object(logs_check, "local_checks", return_value=[]),
            mock.patch.object(logs_check, "loki_query_range", side_effect=query_result),
        ):
            report = logs_check.build_report(Args())
        self.assertEqual(report["status"], "fail")
        worker_check = next(
            item for item in report["checks"]
            if item["name"] == "loki_worker_service_query"
        )
        self.assertEqual(worker_check["status"], "critical")
        self.assertEqual(
            worker_check["details"]["missing_or_invalid_identity_services"],
            ["translation"],
        )
        self.assertEqual(len(report["queries"]["worker_service_identity"]), 8)
        for query in report["queries"]["worker_service_identity"].values():
            self.assertIn('deployment_environment="production"', query)
            self.assertIn("service_version=~", query)
            self.assertIn("revision=~", query)
            self.assertIn("image_digest=~", query)

    def test_worker_uplift_logs_check_quotes_remote_shell_command(self):
        logs_check = load_worker_logs_check_module()

        class Args:
            ssh_host = "65.75.201.18"
            ssh_user = "rami"
            ssh_key = Path("/tmp/key")
            known_hosts = Path("/tmp/known_hosts")

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv

            class Completed:
                returncode = 0
                stdout = "active\n"
                stderr = ""

            return Completed()

        original_run = logs_check.subprocess.run
        try:
            logs_check.subprocess.run = fake_run
            result = logs_check.run_ssh(Args, "systemctl is-active alloy 2>/dev/null || true")
        finally:
            logs_check.subprocess.run = original_run

        self.assertEqual(result["stdout"], "active")
        self.assertEqual(captured["argv"][-3:], ["bash", "-lc", "'systemctl is-active alloy 2>/dev/null || true'"])

    def test_new_relic_job_metrics_are_host_managed_and_secret_safe(self):
        defaults = Path("ansible/roles/backend_baseline/defaults/main.yml").read_text(encoding="utf-8")
        tasks = NEW_RELIC_METRICS_TASKS.read_text(encoding="utf-8")
        workflow = PROTECTED_APPLY_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("nutsnews-newrelic-job-metrics.service", tasks)
        self.assertIn("nutsnews-newrelic-job-metrics.timer", tasks)
        self.assertIn("metric-api.env", defaults)
        self.assertIn("no_log: true", tasks)
        self.assertIn("name: nutsnews-newrelic-job-metrics.service", tasks)
        self.assertNotIn("--jobs-config {{ backend_newrelic_metric_jobs_path }}", tasks.split("- name: Publish New Relic background job metrics once", 1)[1])
        self.assertIn('"backend_newrelic_metrics_enabled"] = True', workflow)
        self.assertIn('"backend_newrelic_metric_license_key"] = new_relic_license_key', workflow)

    def test_new_relic_job_metric_payload_is_low_cardinality(self):
        metrics = load_newrelic_metrics_module()
        jobs = [{"name": "nutsnews-backup", "service_unit": "nutsnews-backup.service", "timer_unit": "nutsnews-backup.timer"}]
        systemd_state = {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "Result": "success",
            "ExecMainStatus": "0",
            "ExecMainStartTimestampMonotonic": "1000000",
            "ExecMainExitTimestampMonotonic": "2500000",
            "NRestarts": "0",
        }
        with (
            mock.patch.object(metrics, "run_systemctl_show", return_value=systemd_state),
            mock.patch.dict(
                "os.environ",
                {
                    "NUTSNEWS_BACKEND_ENVIRONMENT": "production",
                    "NEW_RELIC_SERVICE_NAME": "nutsnews-backend-production",
                    "NUTSNEWS_BACKEND_HOST": "backend.nutsnews.com",
                    "NEW_RELIC_LICENSE_KEY": "secret-not-in-payload",
                },
                clear=True,
            ),
        ):
            payload = metrics.build_payload(jobs, now=123, now_monotonic_usec=3000000)
        text = json.dumps(payload)
        self.assertIn("Custom/NutsNews/job/durationMs", text)
        self.assertIn('"job.name": "nutsnews-backup"', text)
        self.assertIn('"systemd.unit": "nutsnews-backup.service"', text)
        self.assertIn('"status": "success"', text)
        self.assertIn('"value": 1500', text)
        self.assertNotIn("secret-not-in-payload", text)

    def test_textfile_exporter_writes_backup_metrics_without_secret_content(self):
        metrics = load_metrics_module()
        collected_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "backups"
            state_dir.mkdir()
            postgres_dir = Path(tmpdir) / "postgres"
            postgres_dir.mkdir()
            rabbitmq_dir = Path(tmpdir) / "rabbitmq-recovery"
            rabbitmq_dir.mkdir()
            relay_dir = Path(tmpdir) / "supabase-sync-relay"
            relay_dir.mkdir()
            (state_dir / "last-backup.json").write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "freshness_status": "healthy",
                        "latest_snapshot_verified_at_utc": "2026-07-17T00:16:22Z",
                        "quota": {"status": "not_configured"},
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "last-verification.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            (state_dir / "last-restore-verification.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            (postgres_dir / "status.json").write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "last_restore_drill": {"status": "healthy"},
                        "replication": {"lag_status": "not_configured"},
                    }
                ),
                encoding="utf-8",
            )
            (postgres_dir / "replication-health.json").write_text(
                json.dumps(
                    {
                        "checked_at_utc": collected_at,
                        "replication": {
                            "lag_status": "healthy",
                            "max_lag_seconds": 12,
                            "validation_stale_threshold_seconds": 900,
                            "blockers": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (rabbitmq_dir / "last-definition-export.json").write_text(
                json.dumps({"status": "healthy", "finished_at_utc": "2026-07-17T00:16:22Z"}),
                encoding="utf-8",
            )
            (rabbitmq_dir / "last-clean-rebuild-drill.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            (rabbitmq_dir / "last-stopped-volume-restore-drill.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
            relay_path = relay_dir / "last-run.json"
            relay_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "safe_metadata_only": True,
                        "checked_at_utc": collected_at,
                        "completed_at_utc": collected_at,
                        "last_applied_at_utc": "2026-07-17T00:16:22Z",
                        "post_sync": {
                            "checks": [
                                {"id": "table.public.articles", "status": "pass"},
                                {"id": "table.public.rss_feeds", "status": "pass"},
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(metrics, "BACKUP_STATE_DIR", state_dir),
                mock.patch.object(metrics, "RABBITMQ_RECOVERY_STATE_DIR", rabbitmq_dir),
                mock.patch.object(metrics, "POSTGRES_STATE_DIR", postgres_dir),
                mock.patch.object(metrics, "POSTGRES_REPLICATION_HEALTH_PATH", postgres_dir / "replication-health.json"),
                mock.patch.object(metrics, "SUPABASE_SYNC_RELAY_STATE_PATH", relay_path),
                mock.patch.object(metrics, "shell", return_value="0"),
                mock.patch.object(metrics, "service_active", return_value=1),
                mock.patch.object(metrics, "postgres_json_query", return_value=None),
                mock.patch.object(metrics, "fetch_json_url", return_value=None),
            ):
                lines = metrics.collect()
        output = "\n".join(lines)
        self.assertIn('nutsnews_backend_backup_stage_healthy{stage="backup"} 1', output)
        self.assertIn("nutsnews_backend_backup_latest_snapshot_verified 1", output)
        self.assertIn("nutsnews_backend_postgres_failover_ready 1", output)
        self.assertIn('nutsnews_backend_postgres_replication_lag_configured{status="healthy"} 1', output)
        self.assertIn("nutsnews_backend_postgres_replication_blockers 0", output)
        self.assertIn("nutsnews_backend_postgres_replication_max_lag_seconds 12", output)
        self.assertIn("nutsnews_backend_metric_exporter_available 1", output)
        self.assertIn("nutsnews_backend_sync_relay_available 1", output)
        self.assertIn("nutsnews_backend_sync_relay_healthy 1", output)
        self.assertIn("nutsnews_backend_sync_relay_failed_table_count 0", output)
        self.assertIn("nutsnews_backend_sync_relay_last_success_timestamp_seconds", output)
        self.assertIn('nutsnews_backend_rabbitmq_recovery_stage_healthy{stage="definition_export"} 1', output)
        self.assertIn("nutsnews_backend_rabbitmq_definition_export_age_seconds", output)
        self.assertNotIn("password", output.lower())

    def test_textfile_exporter_overwrites_stale_output_with_unavailable_state(self):
        metrics = load_metrics_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "nutsnews.prom"
            output.write_text("nutsnews_stale_success 1\n", encoding="utf-8")
            with mock.patch.object(metrics, "collect", side_effect=RuntimeError("collector failed")), mock.patch(
                "sys.argv", ["nutsnews_metrics_textfile.py", "--output", str(output)]
            ):
                exit_code = metrics.main()
            rendered = output.read_text(encoding="utf-8")

        self.assertEqual(1, exit_code)
        self.assertIn("nutsnews_backend_metric_exporter_available 0", rendered)
        self.assertIn("nutsnews_backend_metric_exporter_error 1", rendered)
        self.assertIn("nutsnews_alloy_readiness_probe_success 0", rendered)
        self.assertIn("nutsnews_alloy_ready 0", rendered)
        self.assertIn("nutsnews_backend_worker_uplift_expected_active 0", rendered)
        self.assertNotIn("nutsnews_stale_success", rendered)
        self.assertNotIn("collector failed", rendered)

    def test_alloy_readiness_metrics_are_bounded_and_explicit(self):
        metrics = load_metrics_module()

        ready = "\n".join(metrics.alloy_readiness_metric_lines(1))
        unavailable = "\n".join(metrics.alloy_readiness_metric_lines(0))

        self.assertIn("nutsnews_alloy_readiness_probe_success 1", ready)
        self.assertIn("nutsnews_alloy_ready 1", ready)
        self.assertIn("nutsnews_alloy_readiness_probe_success 0", unavailable)
        self.assertIn("nutsnews_alloy_ready 0", unavailable)

    def test_backup_history_metrics_separate_failed_run_from_verified_success(self):
        metrics = load_metrics_module()
        now = metrics.timestamp_seconds("2026-08-01T02:00:00Z")
        self.assertIsNotNone(now)
        state = {
            "schema_version": 1,
            "action": "backup",
            "status": "critical",
            "last_run_at_utc": "2026-08-01T01:59:00Z",
            "last_success_at_utc": "2026-07-31T02:00:00Z",
        }

        output = "\n".join(metrics.backup_history_metric_lines(now, state))

        self.assertIn("nutsnews_backend_backup_status_available 1", output)
        self.assertIn("nutsnews_backend_backup_last_run_age_seconds 60", output)
        self.assertIn("nutsnews_backend_backup_last_success_age_seconds 86400", output)
        self.assertIn("nutsnews_backend_backup_last_success_fresh 1", output)
        self.assertIn("nutsnews_backend_backup_stale_after_seconds 108000", output)

    def test_backup_history_metrics_make_first_run_and_corrupt_state_explicit(self):
        metrics = load_metrics_module()
        now = metrics.timestamp_seconds("2026-08-01T02:00:00Z")
        self.assertIsNotNone(now)
        first_failure = {
            "schema_version": 1,
            "action": "backup",
            "status": "critical",
            "last_run_at_utc": "2026-08-01T01:59:00Z",
            "last_success_at_utc": None,
        }

        first_output = "\n".join(metrics.backup_history_metric_lines(now, first_failure))
        corrupt_output = "\n".join(
            metrics.backup_history_metric_lines(now, {"status": "unknown"})
        )

        self.assertIn("nutsnews_backend_backup_status_available 1", first_output)
        self.assertIn("nutsnews_backend_backup_last_success_timestamp_seconds 0", first_output)
        self.assertIn("nutsnews_backend_backup_last_success_age_seconds -1", first_output)
        self.assertIn("nutsnews_backend_backup_last_success_fresh -1", first_output)
        self.assertIn("nutsnews_backend_backup_status_available 0", corrupt_output)
        self.assertIn("nutsnews_backend_backup_last_run_age_seconds -1", corrupt_output)
        self.assertIn("nutsnews_backend_backup_last_success_age_seconds -1", corrupt_output)

    def test_stale_replication_and_failed_relay_cannot_look_fresh(self):
        metrics = load_metrics_module()
        collected_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            postgres_dir = root / "postgres"
            postgres_dir.mkdir()
            (postgres_dir / "status.json").write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "last_restore_drill": {"status": "healthy"},
                    }
                ),
                encoding="utf-8",
            )
            (postgres_dir / "replication-health.json").write_text(
                json.dumps(
                    {
                        "checked_at_utc": "2020-01-01T00:00:00Z",
                        "replication": {
                            "lag_status": "healthy",
                            "max_lag_seconds": 1,
                            "validation_stale_threshold_seconds": 900,
                            "blockers": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            relay_path = root / "last-relay.json"
            relay_path.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "safe_metadata_only": True,
                        "checked_at_utc": collected_at,
                        "completed_at_utc": collected_at,
                        "post_sync": {"checks": [{"id": "table.public.articles", "status": "fail"}]},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(metrics, "BACKUP_STATE_DIR", root / "backups"),
                mock.patch.object(metrics, "RABBITMQ_RECOVERY_STATE_DIR", root / "rabbitmq"),
                mock.patch.object(metrics, "POSTGRES_STATE_DIR", postgres_dir),
                mock.patch.object(
                    metrics,
                    "POSTGRES_REPLICATION_HEALTH_PATH",
                    postgres_dir / "replication-health.json",
                ),
                mock.patch.object(metrics, "SUPABASE_SYNC_RELAY_STATE_PATH", relay_path),
                mock.patch.object(metrics, "shell", return_value="0"),
                mock.patch.object(metrics, "service_active", return_value=1),
                mock.patch.object(metrics, "postgres_json_query", return_value=None),
                mock.patch.object(metrics, "fetch_json_url", return_value=None),
            ):
                output = "\n".join(metrics.collect())

        self.assertIn("nutsnews_backend_postgres_failover_ready 0", output)
        self.assertIn("nutsnews_backend_postgres_replication_telemetry_fresh 0", output)
        self.assertIn("nutsnews_backend_postgres_replication_max_lag_seconds -1", output)
        self.assertIn("nutsnews_backend_sync_relay_collector_fresh 1", output)
        self.assertIn("nutsnews_backend_sync_relay_lag_seconds -1", output)
        self.assertIn("nutsnews_backend_sync_relay_last_success_timestamp_seconds 0", output)
        self.assertIn("nutsnews_backend_sync_relay_last_success_age_seconds -1", output)
        self.assertIn("nutsnews_backend_sync_relay_healthy 0", output)

    def test_durable_content_metrics_are_real_bounded_aggregates(self):
        metrics = load_metrics_module()
        now = metrics.timestamp_seconds("2026-08-01T04:00:00Z")
        self.assertIsNotNone(now)
        query_results = {
            metrics.LEGACY_WORKER_METRICS_QUERY: {
                "last_run_at": "2026-08-01T03:59:00Z",
                "last_success_at": "2026-08-01T03:59:00Z",
                "last_run_success": True,
                "last_scheduled_run_at": "2026-08-01T03:40:00Z",
                "last_scheduled_success_at": "2026-08-01T03:40:00Z",
                "last_scheduled_run_success": True,
                "runs_24h": 10,
                "successful_runs_24h": 9,
                "scheduled_runs_24h": 8,
                "successful_scheduled_runs_24h": 7,
            },
            metrics.FEED_HEALTH_METRICS_QUERY: {
                "active_count": 11,
                "healthy_count": 7,
                "warning_count": 1,
                "failed_count": 1,
                "stale_count": 1,
                "untracked_count": 1,
                "unhealthy_count": 4,
                "oldest_checked_at": "2026-08-01T02:00:00Z",
                "latest_checked_at": "2026-08-01T03:59:00Z",
                "oldest_success_at": "2026-08-01T01:00:00Z",
                "latest_success_at": "2026-08-01T03:58:00Z",
            },
            metrics.CONTENT_COVERAGE_METRICS_QUERY: {
                "snapshot_rows": 120,
                "latest_published_at": "2026-08-01T03:50:00Z",
                "recent_sample_rows": 60,
                "recent_image_rows": 45,
                "recent_translated_pairs": 240,
                "translated_fr": 48,
                "translated_ja": 48,
                "translated_de_ch": 48,
                "translated_de": 48,
                "translated_el": 48,
            },
            metrics.AI_USAGE_METRICS_QUERY: {
                "runs_24h": 5,
                "last_run_at": "2026-08-01T03:45:00Z",
                "local_calls_24h": 20,
                "openai_calls_24h": 2,
                "local_tokens_24h": 2000,
                "openai_tokens_24h": 200,
                "openai_estimated_cost_usd_24h": 0.25,
                "cost_protection_events_24h": 0,
                "spike_warning_events_24h": 1,
            },
            metrics.DATABASE_GROWTH_METRICS_QUERY: {
                "database_size_bytes": 123456,
                "articles_rows": 1000,
                "article_summaries_rows": 5000,
                "worker_runs_rows": 250,
                "ai_usage_runs_rows": 200,
            },
            metrics.WORKER_UPLIFT_OUTBOX_METRICS_QUERY: {
                stage: {"oldest_age_seconds": index * 10, "pending_count": index}
                for index, stage in enumerate(metrics.WORKER_UPLIFT_STAGES)
            },
        }
        public_snapshot = {
            "ready": True,
            "status": "hit",
            "refreshedAt": "2026-08-01T03:55:00Z",
            "ageSeconds": 300,
            "articleCount": 120,
            "maxArticles": 120,
        }
        with (
            mock.patch.object(
                metrics,
                "postgres_json_query",
                side_effect=lambda query: query_results[query],
            ),
            mock.patch.object(metrics, "fetch_json_url", return_value=public_snapshot),
        ):
            output = "\n".join(metrics.durable_content_metric_lines(now))

        self.assertIn("nutsnews_backend_legacy_worker_available 1", output)
        self.assertIn("nutsnews_backend_legacy_worker_last_scheduled_success_age_seconds 1200", output)
        self.assertIn("nutsnews_backend_legacy_worker_fresh_within_15_minutes 0", output)
        self.assertIn("nutsnews_backend_feed_healthy_count 7", output)
        self.assertIn("nutsnews_backend_feed_warning_count 1", output)
        self.assertIn("nutsnews_backend_feed_oldest_check_age_seconds 7200", output)
        self.assertIn("nutsnews_backend_public_feed_snapshot_newest_content_age_seconds 600", output)
        self.assertIn("nutsnews_backend_recent_published_image_coverage_ratio 0.75", output)
        self.assertIn("nutsnews_backend_recent_published_translation_coverage_ratio 0.8", output)
        self.assertIn(
            'nutsnews_backend_recent_published_language_coverage_ratio{language="de-CH"} 0.8',
            output,
        )
        self.assertIn('nutsnews_backend_ai_calls_24h{provider="local"} 20', output)
        self.assertIn('nutsnews_backend_ai_calls_24h{provider="openai"} 2', output)
        self.assertIn("nutsnews_backend_database_size_bytes 123456", output)
        self.assertIn("nutsnews_backend_public_feed_edge_snapshot_age_seconds 300", output)
        self.assertIn('nutsnews_backend_public_feed_edge_snapshot_status{status="hit"} 1', output)
        self.assertIn(
            'nutsnews_backend_worker_uplift_oldest_unconfirmed_outbox_age_seconds{stage="publication"} 70',
            output,
        )
        self.assertNotIn("original_url", output)
        self.assertNotIn("feed_url", output)

    def test_durable_content_metrics_emit_unavailable_instead_of_stale_success(self):
        metrics = load_metrics_module()
        with (
            mock.patch.object(metrics, "postgres_json_query", return_value=None),
            mock.patch.object(metrics, "fetch_json_url", return_value=None),
        ):
            output = "\n".join(metrics.durable_content_metric_lines(1_800_000_000))

        for metric_name in (
            "nutsnews_backend_legacy_worker_available",
            "nutsnews_backend_feed_health_available",
            "nutsnews_backend_content_coverage_available",
            "nutsnews_backend_ai_usage_available",
            "nutsnews_backend_database_growth_available",
            "nutsnews_backend_public_feed_edge_snapshot_available",
        ):
            self.assertIn(f"{metric_name} 0", output)
        self.assertIn("nutsnews_backend_legacy_worker_last_success_age_seconds -1", output)
        self.assertIn("nutsnews_backend_recent_published_image_coverage_ratio -1", output)
        self.assertIn("nutsnews_backend_public_feed_edge_snapshot_age_seconds -1", output)
        self.assertIn(
            'nutsnews_backend_worker_uplift_outbox_available{stage="scheduler"} 0',
            output,
        )
        self.assertIn(
            'nutsnews_backend_worker_uplift_oldest_unconfirmed_outbox_age_seconds{stage="scheduler"} -1',
            output,
        )

    def test_database_and_public_snapshot_collectors_reject_unsafe_targets(self):
        metrics = load_metrics_module()
        with mock.patch.object(metrics.subprocess, "run") as run:
            self.assertIsNone(metrics.postgres_json_query("select '{}'::json", database="bad;drop"))
            self.assertIsNone(metrics.fetch_json_url("http://127.0.0.1/private"))
        run.assert_not_called()

    def test_metrics_service_runs_durable_signals_every_five_minutes(self):
        defaults = Path("ansible/roles/backend_baseline/defaults/main.yml").read_text(encoding="utf-8")
        tasks = METRICS_TASKS.read_text(encoding="utf-8")
        self.assertIn('backend_metrics_textfile_calendar: "*:0/5:00"', defaults)
        self.assertIn("NUTSNEWS_METRICS_POSTGRES_DATABASE={{ backend_postgres_primary_shadow_database }}", tasks)
        self.assertIn("NUTSNEWS_PUBLIC_FEED_STATUS_URL={{ backend_metrics_public_feed_status_url }}", tasks)
        self.assertIn("NUTSNEWS_HEALTH_AUDIT_STATE_PATH={{ backend_metrics_health_audit_state_path }}", tasks)
        self.assertIn("NUTSNEWS_BACKUP_STALE_AFTER_HOURS={{ backend_backup_stale_after_hours }}", tasks)
        self.assertIn("NUTSNEWS_WORKER_UPLIFT_EXPECTED_ACTIVE={{ backend_metrics_worker_uplift_expected_active }}", tasks)
        self.assertIn("NUTSNEWS_WORKER_UPLIFT_DEPLOYMENT_MODE={{ backend_metrics_worker_uplift_deployment_mode }}", tasks)

    def test_health_audit_state_is_recomputed_into_scrapeable_age_metrics(self):
        metrics = load_metrics_module()
        now = metrics.timestamp_seconds("2026-08-01T13:00:00Z")
        self.assertIsNotNone(now)
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "last-run.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "safe_metadata_only": True,
                        "source": "github_actions",
                        "available": True,
                        "conclusion": "failure",
                        "last_run_at_utc": "2026-08-01T12:00:00Z",
                        "last_success_at_utc": "2026-07-31T12:00:00Z",
                        "consecutive_failures": 2,
                        "critical_checks": 3,
                        "expected_interval_seconds": 86400,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(metrics, "HEALTH_AUDIT_STATE_PATH", state_path):
                output = "\n".join(metrics.health_audit_metric_lines(now))

        self.assertIn("nutsnews_backend_health_audit_available 1", output)
        self.assertIn('nutsnews_backend_health_audit_conclusion{conclusion="failure"} 1', output)
        self.assertIn("nutsnews_backend_health_audit_last_run_age_seconds 3600", output)
        self.assertIn("nutsnews_backend_health_audit_last_success_age_seconds 90000", output)
        self.assertIn("nutsnews_backend_health_audit_consecutive_failures 2", output)
        self.assertIn("nutsnews_backend_health_audit_critical_checks 3", output)
        self.assertIn("nutsnews_backend_health_audit_expected_interval_seconds 86400", output)

    def test_invalid_health_audit_state_is_explicitly_unavailable(self):
        metrics = load_metrics_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "last-run.json"
            state_path.write_text('{"conclusion":"totally-arbitrary"}', encoding="utf-8")
            with mock.patch.object(metrics, "HEALTH_AUDIT_STATE_PATH", state_path):
                output = "\n".join(metrics.health_audit_metric_lines(1_800_000_000))

        self.assertIn("nutsnews_backend_health_audit_available 0", output)
        self.assertIn('nutsnews_backend_health_audit_conclusion{conclusion="unknown"} 1', output)
        self.assertIn("nutsnews_backend_health_audit_last_run_timestamp_seconds 0", output)
        self.assertIn("nutsnews_backend_health_audit_last_run_age_seconds -1", output)
        self.assertIn("nutsnews_backend_health_audit_consecutive_failures -1", output)


if __name__ == "__main__":
    unittest.main()
