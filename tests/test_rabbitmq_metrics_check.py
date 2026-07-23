#!/usr/bin/env python3
from __future__ import annotations

import unittest

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
                "alloy_config": {"stdout": "Config file is valid\n"},
            }
        }
        checks = {item["name"]: item for item in metrics_check.classify_local(evidence)}
        self.assertEqual(checks["rabbitmq_aggregate_endpoint"]["status"], "healthy")
        self.assertEqual(checks["rabbitmq_detailed_endpoint"]["status"], "healthy")
        self.assertEqual(checks["rabbitmq_prometheus_listener"]["status"], "healthy")
        self.assertEqual(checks["alloy_service"]["status"], "healthy")
        self.assertEqual(checks["alloy_config"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
