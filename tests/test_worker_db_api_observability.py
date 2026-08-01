#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "ansible/roles/backend_baseline/files/nutsnews_worker_db_api.py"

spec = importlib.util.spec_from_file_location("nutsnews_worker_db_api_observability", API_PATH)
worker_db_api = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker_db_api)


class ReadyStore:
    writes_enabled = False

    def fetch_one(self, query: str, params: tuple = ()) -> dict:
        if query.strip().lower() == "select 1 as ready":
            return {"ready": 1}
        return {"ok": True}


class UnreadyStore(ReadyStore):
    def fetch_one(self, query: str, params: tuple = ()) -> dict:
        raise RuntimeError("sensitive connection failure detail")


class RunningApi:
    def __init__(self, store: object) -> None:
        identity = {
            "service": "nutsnews-worker-db-api",
            "service_version": "test-version",
            "revision": "test-revision",
            "deployment_environment": "test",
            "host": "backend-test",
        }
        metrics = worker_db_api.ApiMetrics(identity)
        self.server = worker_db_api.WorkerDbApiServer(
            ("127.0.0.1", 0),
            worker_db_api.WorkerDbApiHandler,
            store=store,
            metrics=metrics,
        )
        self.server.identity = identity
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningApi":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        response_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, response_headers, body


class ApiMetricsTests(unittest.TestCase):
    def test_histogram_and_counters_use_only_bounded_request_labels(self) -> None:
        metrics = worker_db_api.ApiMetrics(
            {
                "service": "nutsnews-worker-db-api",
                "service_version": "1.2.3",
                "revision": "abc123",
                "deployment_environment": "production",
                "host": "backend-vps",
            }
        )
        metrics.observe_request("load-published-articles", "POST", 200, 0.02)
        metrics.observe_request("unknown_api_operation", "POST", 404, 0.8)

        rendered = metrics.render(writes_enabled=False)

        self.assertIn("nutsnews_backend_api_requests_total", rendered)
        self.assertIn("nutsnews_backend_api_request_errors_total", rendered)
        self.assertIn("nutsnews_backend_api_request_duration_seconds_bucket", rendered)
        self.assertIn('operation="load-published-articles"', rendered)
        self.assertIn('operation="unknown_api_operation"', rendered)
        self.assertIn('status_class="4xx"', rendered)
        self.assertIn('le="+Inf"', rendered)
        self.assertNotIn("article-id", rendered)
        self.assertNotIn("request_id", rendered)

    def test_unknown_operation_is_collapsed_to_constant_label(self) -> None:
        self.assertEqual(
            "unknown_api_operation",
            worker_db_api.bounded_request_operation("POST", "/api/worker/db/attacker-controlled-value"),
        )
        self.assertEqual("unknown_route", worker_db_api.bounded_request_operation("GET", "/arbitrary-value"))


class ApiEndpointTests(unittest.TestCase):
    @patch("builtins.print")
    def test_health_readiness_liveness_and_metrics_contract(self, _print) -> None:
        with RunningApi(ReadyStore()) as api:
            health_status, health_headers, health_body = api.request("GET", "/healthz")
            live_status, live_headers, live_body = api.request(
                "GET", "/livez", headers={"x-request-id": "request-test-1"}
            )
            ready_status, ready_headers, ready_body = api.request("GET", "/readyz")
            metrics_status, metrics_headers, metrics_body = api.request("GET", "/metrics")

        self.assertEqual(200, health_status)
        self.assertEqual({"status": "ok"}, json.loads(health_body))
        self.assertEqual("no-store", health_headers["cache-control"])
        self.assertEqual(200, live_status)
        self.assertEqual("request-test-1", live_headers["x-request-id"])
        self.assertTrue(json.loads(live_body)["live"])
        self.assertEqual(200, ready_status)
        self.assertTrue(json.loads(ready_body)["ready"])
        self.assertEqual("ready", json.loads(ready_body)["dependencies"]["postgresql"])
        self.assertEqual("no-store", ready_headers["cache-control"])
        self.assertEqual(200, metrics_status)
        self.assertTrue(metrics_headers["content-type"].startswith("text/plain; version=0.0.4"))
        rendered = metrics_body.decode("utf-8")
        self.assertIn('nutsnews_backend_api_dependency_ready{dependency="postgresql"} 1', rendered)
        self.assertIn('operation="readyz"', rendered)
        self.assertNotIn('operation="metrics"', rendered, "the current scrape is observed after its response is rendered")

    @patch("builtins.print")
    def test_readiness_fails_closed_without_leaking_database_error(self, _print) -> None:
        with RunningApi(UnreadyStore()) as api:
            status, headers, body = api.request("GET", "/readyz")
            _, _, metrics_body = api.request("GET", "/metrics")

        payload = json.loads(body)
        self.assertEqual(503, status)
        self.assertFalse(payload["ready"])
        self.assertEqual("unavailable", payload["dependencies"]["postgresql"])
        self.assertNotIn("sensitive", body.decode("utf-8"))
        self.assertEqual("no-store", headers["cache-control"])
        self.assertIn('nutsnews_backend_api_dependency_ready{dependency="postgresql"} 0', metrics_body.decode("utf-8"))

    @patch("builtins.print")
    def test_unknown_routes_are_counted_without_path_cardinality(self, _print) -> None:
        with RunningApi(ReadyStore()) as api:
            status, _, _ = api.request("POST", "/api/worker/db/arbitrary-customer-value")
            _, _, metrics_body = api.request("GET", "/metrics")

        rendered = metrics_body.decode("utf-8")
        self.assertEqual(503, status)
        self.assertIn('operation="unknown_api_operation"', rendered)
        self.assertNotIn("arbitrary-customer-value", rendered)


if __name__ == "__main__":
    unittest.main()
