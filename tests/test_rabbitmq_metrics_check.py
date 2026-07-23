#!/usr/bin/env python3
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import backend_rabbitmq_metrics_check as metrics_check


class RabbitMQMetricsCheckTests(unittest.TestCase):
    def test_prometheus_query_url_is_derived_from_remote_write_url(self):
        self.assertEqual(
            metrics_check.derive_prometheus_query_url("https://prom.example/api/prom/push"),
            "https://prom.example/api/prom/api/v1/query",
        )
        self.assertEqual(
            metrics_check.derive_prometheus_query_url("https://prom.example/api/v1/push"),
            "https://prom.example/api/v1/query",
        )

    def test_local_classification_requires_private_endpoints_and_alloy(self):
        evidence = {
            "commands": {
                "rabbitmq_aggregate_metrics": {"stdout": "rabbitmq_up 1\n"},
                "rabbitmq_detailed_metrics": {"stdout": 'rabbitmq_detailed_queue_messages{queue="nutsnews.worker.fetch.v1"} 0\n'},
                "rabbitmq_listener": {"stdout": "LISTEN 0 4096 127.0.0.1:15692 0.0.0.0:*\n"},
                "alloy_active": {"stdout": "active\n"},
                "alloy_config": {"rc": 0, "stdout": ""},
            }
        }
        checks = {item["name"]: item for item in metrics_check.classify_local(evidence)}
        self.assertEqual(checks["rabbitmq_aggregate_endpoint"]["status"], "healthy")
        self.assertEqual(checks["rabbitmq_detailed_endpoint"]["status"], "healthy")
        self.assertEqual(checks["rabbitmq_prometheus_listener"]["status"], "healthy")
        self.assertEqual(checks["alloy_service"]["status"], "healthy")
        self.assertEqual(checks["alloy_config"]["status"], "healthy")

    def test_optional_grafana_query_does_not_fail_local_metrics_proof(self):
        evidence = {
            "commands": {
                "rabbitmq_aggregate_metrics": {"stdout": "rabbitmq_up 1\n"},
                "rabbitmq_detailed_metrics": {"stdout": 'rabbitmq_detailed_queue_messages{queue="nutsnews.worker.fetch.v1"} 0\n'},
                "rabbitmq_listener": {"stdout": "LISTEN 0 4096 127.0.0.1:15692 0.0.0.0:*\n"},
                "alloy_active": {"stdout": "active\n"},
                "alloy_config": {"rc": 0, "stdout": ""},
            }
        }
        args = SimpleNamespace(
            grafana_prometheus_url="https://prom.example/api/prom/push",
            grafana_prometheus_username="user",
            grafana_prometheus_password="password",
            require_grafana_data=False,
            timeout=1,
        )
        with mock.patch.object(metrics_check, "grafana_query", return_value={"status": "critical", "summary": "Grafana query failed: HTTP 403"}):
            report = metrics_check.build_report(args, evidence)
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(report["status"], "pass")
        self.assertEqual(checks["grafana_rabbitmq_query"]["status"], "not_configured")

    def test_required_grafana_query_fails_metrics_proof(self):
        evidence = {
            "commands": {
                "rabbitmq_aggregate_metrics": {"stdout": "rabbitmq_up 1\n"},
                "rabbitmq_detailed_metrics": {"stdout": 'rabbitmq_detailed_queue_messages{queue="nutsnews.worker.fetch.v1"} 0\n'},
                "rabbitmq_listener": {"stdout": "LISTEN 0 4096 127.0.0.1:15692 0.0.0.0:*\n"},
                "alloy_active": {"stdout": "active\n"},
                "alloy_config": {"rc": 0, "stdout": ""},
            }
        }
        args = SimpleNamespace(
            grafana_prometheus_url="https://prom.example/api/prom/push",
            grafana_prometheus_username="user",
            grafana_prometheus_password="password",
            require_grafana_data=True,
            timeout=1,
        )
        with mock.patch.object(metrics_check, "grafana_query", return_value={"status": "critical", "summary": "Grafana query failed: HTTP 403"}):
            report = metrics_check.build_report(args, evidence)
        self.assertEqual(report["status"], "fail")


if __name__ == "__main__":
    unittest.main()
