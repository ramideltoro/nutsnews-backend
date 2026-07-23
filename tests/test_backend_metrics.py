#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
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


class BackendMetricsTests(unittest.TestCase):
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
        self.assertNotIn("request_id", template)
        self.assertNotIn("loki.source.docker", template)
        self.assertNotIn("/var/run/docker.sock", template)

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
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "backups"
            state_dir.mkdir()
            postgres_dir = Path(tmpdir) / "postgres"
            postgres_dir.mkdir()
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
                json.dumps({"replication": {"lag_status": "healthy", "max_lag_seconds": 12, "blockers": []}}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(metrics, "BACKUP_STATE_DIR", state_dir),
                mock.patch.object(metrics, "POSTGRES_STATE_DIR", postgres_dir),
                mock.patch.object(metrics, "POSTGRES_REPLICATION_HEALTH_PATH", postgres_dir / "replication-health.json"),
                mock.patch.object(metrics, "shell", return_value="0"),
                mock.patch.object(metrics, "service_active", return_value=1),
            ):
                lines = metrics.collect()
        output = "\n".join(lines)
        self.assertIn('nutsnews_backend_backup_stage_healthy{stage="backup"} 1', output)
        self.assertIn("nutsnews_backend_backup_latest_snapshot_verified 1", output)
        self.assertIn("nutsnews_backend_postgres_failover_ready 1", output)
        self.assertIn('nutsnews_backend_postgres_replication_lag_configured{status="healthy"} 1', output)
        self.assertIn("nutsnews_backend_postgres_replication_blockers 0", output)
        self.assertIn("nutsnews_backend_postgres_replication_max_lag_seconds 12", output)
        self.assertNotIn("password", output.lower())


if __name__ == "__main__":
    unittest.main()
