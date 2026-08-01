#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import types
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "ansible/roles/backend_baseline/files/nutsnews_worker_db_api.py"
DEFAULTS_PATH = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
ALLOY_PATH = ROOT / "ansible/roles/backend_baseline/templates/alloy-config.alloy.j2"

spec = importlib.util.spec_from_file_location("nutsnews_worker_db_api_observability", API_PATH)
worker_db_api = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker_db_api)


class ReadyStore:
    writes_enabled = False

    def check_readiness(self) -> bool:
        row = self.fetch_one("select 1 as ready")
        return bool(row and row.get("ready") == 1)

    def fetch_one(self, query: str, params: tuple = ()) -> dict:
        if query.strip().lower() == "select 1 as ready":
            return {"ready": 1}
        return {"ok": True}


class UnreadyStore(ReadyStore):
    def fetch_one(self, query: str, params: tuple = ()) -> dict:
        raise RuntimeError("sensitive connection failure detail")


class ManualClock:
    def __init__(self) -> None:
        self.monotonic_value = 10.0
        self.wall_value = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def time(self) -> float:
        return self.wall_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


class BlockingReadinessStore(ReadyStore):
    def __init__(self) -> None:
        self.calls = 0
        self.ready = True
        self.started = threading.Event()
        self.release = threading.Event()

    def check_readiness(self) -> bool:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test readiness probe was not released")
        return self.ready


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

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, bytes]:
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
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
        metrics.observe_request(worker_db_api.INVALID_ROUTE_OPERATION, "POST", 404, 0.8)
        metrics.observe_request("attacker-controlled-operation", "TRACE", 418, 0.1)

        rendered = metrics.render(writes_enabled=False)

        self.assertIn("nutsnews_backend_api_requests_total", rendered)
        self.assertIn("nutsnews_backend_api_request_errors_total", rendered)
        self.assertIn("nutsnews_backend_api_request_duration_seconds_bucket", rendered)
        self.assertIn('operation="load-published-articles"', rendered)
        self.assertIn('operation="invalid_route"', rendered)
        self.assertIn('operation="metric_key_overflow"', rendered)
        self.assertIn('status_class="4xx"', rendered)
        self.assertIn('le="+Inf"', rendered)
        self.assertNotIn("attacker-controlled-operation", rendered)
        self.assertNotIn("article-id", rendered)
        self.assertNotIn("request_id", rendered)

    def test_unknown_operation_is_collapsed_to_constant_label(self) -> None:
        self.assertEqual(
            worker_db_api.INVALID_ROUTE_OPERATION,
            worker_db_api.bounded_request_operation("POST", "/api/worker/db/attacker-controlled-value"),
        )
        self.assertEqual(
            worker_db_api.INVALID_ROUTE_OPERATION,
            worker_db_api.bounded_request_operation("GET", "/arbitrary-value"),
        )
        self.assertEqual(
            worker_db_api.INVALID_METHOD_OPERATION,
            worker_db_api.bounded_request_operation("GET", "/api/app/db/load-published-articles"),
        )

    def test_histogram_buckets_are_cumulative_and_inf_matches_count(self) -> None:
        metrics = worker_db_api.ApiMetrics(
            {
                "service": "nutsnews-worker-db-api",
                "service_version": "1.2.0",
                "revision": "a" * 40,
                "deployment_environment": "test",
                "host": "backend-test",
            }
        )
        metrics.observe_request("readyz", "GET", 200, 0.02)
        metrics.observe_request("readyz", "GET", 200, 0.8)
        rendered = metrics.render(writes_enabled=False)
        bucket_lines = [
            line
            for line in rendered.splitlines()
            if line.startswith("nutsnews_backend_api_request_duration_seconds_bucket")
            and 'operation="readyz"' in line
        ]
        counts = [int(line.rsplit(" ", 1)[1]) for line in bucket_lines]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(2, counts[-1])
        self.assertIn(
            'nutsnews_backend_api_request_duration_seconds_count{method="GET",operation="readyz",status_class="2xx"} 2',
            rendered,
        )

    def test_registry_budget_and_scrape_limit_cover_the_proven_worst_case(self) -> None:
        identity = {
            "service": "nutsnews-worker-db-api",
            "service_version": "1.2.0",
            "revision": "a" * 40,
            "deployment_environment": "test",
            "host": "backend-test",
        }
        empty_metrics = worker_db_api.ApiMetrics(identity)
        empty_samples = [
            line
            for line in empty_metrics.render(writes_enabled=False).splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(worker_db_api.FIXED_PROMETHEUS_SAMPLE_COUNT, len(empty_samples))

        metrics = worker_db_api.ApiMetrics(identity)
        rejection_inputs = (
            (worker_db_api.INVALID_METHOD_OPERATION, "GET", 404),
            (worker_db_api.INVALID_METHOD_OPERATION, "POST", 405),
            (worker_db_api.INVALID_METHOD_OPERATION, "PATCH", 405),
            (worker_db_api.INVALID_METHOD_OPERATION, "TRACE", 599),
            (worker_db_api.INVALID_ROUTE_OPERATION, "GET", 404),
            (worker_db_api.INVALID_ROUTE_OPERATION, "POST", 404),
            (worker_db_api.INVALID_ROUTE_OPERATION, "PATCH", 599),
            (worker_db_api.AUTHENTICATION_FAILED_OPERATION, "POST", 401),
            (worker_db_api.AUTHENTICATION_FAILED_OPERATION, "POST", 503),
            (worker_db_api.AUTHENTICATION_FAILED_OPERATION, "TRACE", 599),
        )
        # Flood the public rejection paths before legitimate traffic. Their
        # canonical four keys must not consume the reviewed application budget.
        for _ in range(100):
            for operation, method, status in rejection_inputs:
                metrics.observe_request(operation, method, status, 0.01)
        for operation in sorted(worker_db_api.API_POST_OPERATIONS):
            for status in (200, 400, 500):
                metrics.observe_request(operation, "POST", status, 0.01)
        for operation in sorted(worker_db_api.HEALTH_OPERATIONS):
            for status in (200, 500):
                metrics.observe_request(operation, "GET", status, 0.01)
        metrics.observe_request("untrusted-operation", "untrusted-method", 999, 0.01)

        proven = metrics.render(writes_enabled=False)
        proven_request_keys = [
            line for line in proven.splitlines() if line.startswith("nutsnews_backend_api_requests_total{")
        ]
        self.assertEqual(worker_db_api.PROVEN_REQUEST_METRIC_KEYS, len(proven_request_keys))
        self.assertLessEqual(
            worker_db_api.PROVEN_REQUEST_METRIC_KEYS,
            worker_db_api.MAX_REQUEST_METRIC_KEYS,
        )

        # Exercise unexpected status classes after the reviewed key space is
        # full. The defensive overflow key must keep the registry hard-bounded.
        for operation in sorted(worker_db_api.API_POST_OPERATIONS):
            metrics.observe_request(operation, "POST", 302, 0.01)
        rendered = metrics.render(writes_enabled=False)
        request_keys = [
            line for line in rendered.splitlines() if line.startswith("nutsnews_backend_api_requests_total{")
        ]
        samples = [line for line in rendered.splitlines() if line and not line.startswith("#")]
        self.assertEqual(worker_db_api.MAX_REQUEST_METRIC_KEYS, len(request_keys))
        self.assertIn('operation="metric_key_overflow"', rendered)
        self.assertLessEqual(len(samples), worker_db_api.MAX_RENDERED_PROMETHEUS_SAMPLES)

        # Construct the absolute renderer maximum from 200 distinct error
        # keys. This proves the sample bound against actual exposition lines,
        # rather than relying only on the per-key formula above.
        worst_case_metrics = worker_db_api.ApiMetrics(identity)
        for operation in sorted(worker_db_api.API_POST_OPERATIONS):
            for status in (400, 500, 999):
                worst_case_metrics.observe_request(operation, "POST", status, 0.01)
        for operation in sorted(worker_db_api.HEALTH_OPERATIONS):
            for status in (400, 500, 999):
                worst_case_metrics.observe_request(operation, "GET", status, 0.01)
        for operation, status in (
            (worker_db_api.INVALID_METHOD_OPERATION, 404),
            (worker_db_api.INVALID_ROUTE_OPERATION, 404),
            (worker_db_api.AUTHENTICATION_FAILED_OPERATION, 401),
            (worker_db_api.AUTHENTICATION_FAILED_OPERATION, 503),
        ):
            worst_case_metrics.observe_request(operation, "POST", status, 0.01)
        worst_case_metrics.observe_request("untrusted-operation", "TRACE", 999, 0.01)
        worst_case_rendered = worst_case_metrics.render(writes_enabled=False)
        worst_case_samples = [
            line for line in worst_case_rendered.splitlines() if line and not line.startswith("#")
        ]
        self.assertEqual(worker_db_api.MAX_REQUEST_METRIC_KEYS, len([
            line
            for line in worst_case_rendered.splitlines()
            if line.startswith("nutsnews_backend_api_requests_total{")
        ]))
        self.assertEqual(worker_db_api.MAX_RENDERED_PROMETHEUS_SAMPLES, len(worst_case_samples))

        defaults = DEFAULTS_PATH.read_text(encoding="utf-8")
        configured_limit_match = re.search(r"^backend_metrics_backend_api_sample_limit: (\d+)$", defaults, re.MULTILINE)
        self.assertIsNotNone(configured_limit_match)
        configured_limit = int(configured_limit_match.group(1))
        self.assertGreater(configured_limit, worker_db_api.MAX_RENDERED_PROMETHEUS_SAMPLES)
        self.assertLess(len(worst_case_samples), configured_limit)
        self.assertIn(
            "sample_limit    = {{ backend_metrics_backend_api_sample_limit }}",
            ALLOY_PATH.read_text(encoding="utf-8"),
        )


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
    def test_readiness_is_single_flight_cached_and_never_serves_expired_success(self, _print) -> None:
        store = BlockingReadinessStore()
        clock = ManualClock()
        first_response: list[tuple[int, dict, bytes]] = []
        with RunningApi(store) as api:
            api.server.readiness = worker_db_api.PostgresReadinessGate(
                store,
                monotonic_clock=clock.monotonic,
                wall_clock=clock.time,
            )
            first_thread = threading.Thread(
                target=lambda: first_response.append(api.request("GET", "/readyz")),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(store.started.wait(timeout=2))
            try:
                busy_status, _, busy_body = api.request("GET", "/readyz")
                self.assertEqual(503, busy_status)
                self.assertFalse(json.loads(busy_body)["ready"])
                self.assertEqual(1, store.calls)
            finally:
                store.release.set()
            first_thread.join(timeout=5)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual(200, first_response[0][0])

            cached_status, _, _ = api.request("GET", "/readyz")
            self.assertEqual(200, cached_status)
            self.assertEqual(1, store.calls)
            clock.advance(worker_db_api.READINESS_CACHE_TTL_SECONDS - 0.01)
            fresh_cache_status, _, _ = api.request("GET", "/readyz")
            self.assertEqual(200, fresh_cache_status)
            self.assertEqual(1, store.calls)

            store.ready = False
            clock.advance(0.02)
            expired_status, _, expired_body = api.request("GET", "/readyz")
            self.assertEqual(503, expired_status)
            self.assertFalse(json.loads(expired_body)["ready"])
            self.assertEqual(2, store.calls)
            cached_failure_status, _, _ = api.request("GET", "/readyz")
            self.assertEqual(503, cached_failure_status)
            self.assertEqual(2, store.calls)

    def test_postgresql_readiness_probe_uses_strict_connect_and_statement_timeouts(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def execute(self, query: str) -> None:
                self.queries.append(query)

            def fetchone(self) -> dict[str, int]:
                return {"ready": 1}

        class FakeConnection:
            def __init__(self, cursor: FakeCursor) -> None:
                self.test_cursor = cursor

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def cursor(self) -> FakeCursor:
                return self.test_cursor

        store = worker_db_api.PostgresStore()
        cursor = FakeCursor()
        with patch.object(store, "connect", return_value=FakeConnection(cursor)) as connect:
            self.assertTrue(store.check_readiness())

        connect.assert_called_once_with(
            connect_timeout_seconds=worker_db_api.READINESS_CONNECT_TIMEOUT_SECONDS,
            statement_timeout_ms=worker_db_api.READINESS_STATEMENT_TIMEOUT_MS,
        )
        self.assertEqual(["select 1 as ready"], cursor.queries)

        fake_psycopg2 = types.ModuleType("psycopg2")
        fake_psycopg2.__path__ = []  # type: ignore[attr-defined]
        fake_extras = types.ModuleType("psycopg2.extras")
        fake_extras.RealDictCursor = object  # type: ignore[attr-defined]
        fake_connect = Mock(return_value=object())
        fake_psycopg2.connect = fake_connect  # type: ignore[attr-defined]
        fake_psycopg2.extras = fake_extras  # type: ignore[attr-defined]
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extras": fake_extras},
        ):
            store.connect(
                connect_timeout_seconds=worker_db_api.READINESS_CONNECT_TIMEOUT_SECONDS,
                statement_timeout_ms=worker_db_api.READINESS_STATEMENT_TIMEOUT_MS,
            )

        connection_options = fake_connect.call_args.kwargs
        self.assertEqual(
            worker_db_api.READINESS_CONNECT_TIMEOUT_SECONDS,
            connection_options["connect_timeout"],
        )
        self.assertEqual(
            f"-c statement_timeout={worker_db_api.READINESS_STATEMENT_TIMEOUT_MS}",
            connection_options["options"],
        )

    @patch("builtins.print")
    def test_unauthenticated_routes_are_counted_without_operation_cardinality(self, _print) -> None:
        with RunningApi(ReadyStore()) as api:
            status, _, _ = api.request("POST", "/api/worker/db/arbitrary-customer-value")
            _, _, metrics_body = api.request("GET", "/metrics")

        rendered = metrics_body.decode("utf-8")
        self.assertEqual(503, status)
        self.assertIn('operation="authentication_failed"', rendered)
        self.assertNotIn("arbitrary-customer-value", rendered)

    @patch("builtins.print")
    def test_public_method_route_and_auth_rejections_use_only_fixed_operations(self, _print) -> None:
        routes = [
            *(f"/api/worker/db/{operation}" for operation in sorted(worker_db_api.WORKER_API_OPERATIONS)),
            *(f"/api/app/db/{operation}" for operation in sorted(worker_db_api.APP_API_OPERATIONS)),
        ]
        with RunningApi(ReadyStore()) as api:
            with patch.dict("os.environ", {}, clear=True):
                for route in routes:
                    get_status, _, _ = api.request("GET", route)
                    missing_auth_status, _, _ = api.request("POST", route, body=b"{}")
                    query_status, _, _ = api.request("POST", f"{route}?token=never-index", body=b"{}")
                    slash_status, _, _ = api.request("POST", f"{route}/", body=b"{}")
                    self.assertEqual((404, 503, 400, 404), (
                        get_status,
                        missing_auth_status,
                        query_status,
                        slash_status,
                    ))
            with patch.dict("os.environ", {"NUTSNEWS_BACKEND_API_TOKEN": "expected-token"}, clear=True):
                for route in routes:
                    invalid_auth_status, _, _ = api.request(
                        "POST",
                        route,
                        body=b"{}",
                        headers={"authorization": "Bearer invalid-token"},
                    )
                    self.assertEqual(401, invalid_auth_status)
            rendered = api.server.metrics.render(writes_enabled=False)

        request_lines = [
            line for line in rendered.splitlines() if line.startswith("nutsnews_backend_api_requests_total{")
        ]
        observed_operations = {
            re.search(r'operation="([^"]+)"', line).group(1)  # type: ignore[union-attr]
            for line in request_lines
        }
        self.assertEqual(
            {
                worker_db_api.INVALID_METHOD_OPERATION,
                worker_db_api.INVALID_ROUTE_OPERATION,
                worker_db_api.AUTHENTICATION_FAILED_OPERATION,
            },
            observed_operations,
        )
        observed_keys = {
            (
                re.search(r'method="([^"]+)"', line).group(1),  # type: ignore[union-attr]
                re.search(r'operation="([^"]+)"', line).group(1),  # type: ignore[union-attr]
                re.search(r'status_class="([^"]+)"', line).group(1),  # type: ignore[union-attr]
            )
            for line in request_lines
        }
        self.assertEqual(
            {
                ("OTHER", worker_db_api.INVALID_METHOD_OPERATION, "4xx"),
                ("OTHER", worker_db_api.INVALID_ROUTE_OPERATION, "4xx"),
                ("POST", worker_db_api.AUTHENTICATION_FAILED_OPERATION, "4xx"),
                ("POST", worker_db_api.AUTHENTICATION_FAILED_OPERATION, "5xx"),
            },
            observed_keys,
        )
        for operation in worker_db_api.API_POST_OPERATIONS:
            self.assertNotIn(f'operation="{operation}"', rendered)
        self.assertNotIn("never-index", rendered)

    @patch("builtins.print")
    def test_structured_completion_log_excludes_path_query_headers_body_and_error_text(self, output) -> None:
        traceparent = "00-" + ("a" * 32) + "-" + ("b" * 16) + "-01"
        with RunningApi(UnreadyStore()) as api:
            status, headers, _ = api.request(
                "GET",
                "/readyz/?token=private-token&email=person@example.com",
                headers={
                    "authorization": "Bearer private-token",
                    "x-request-id": "person@example.com",
                    "traceparent": traceparent,
                },
            )

        self.assertEqual(503, status)
        self.assertNotEqual("person@example.com", headers["x-request-id"])
        records = [json.loads(call.args[0]) for call in output.call_args_list]
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("readyz", record["operation"])
        self.assertEqual("5xx", record["status_class"])
        self.assertEqual(traceparent, record["traceparent"])
        self.assertEqual("RuntimeError", record["error_class"])
        self.assertTrue(
            set(record).issubset(
                {
                    "at",
                    "event",
                    "deployment_environment",
                    "service",
                    "service_version",
                    "host",
                    "source",
                    "severity",
                    "method",
                    "operation",
                    "status_class",
                    "status",
                    "duration_ms",
                    "request_id",
                    "traceparent",
                    "error_class",
                }
            )
        )
        rendered = json.dumps(record, sort_keys=True)
        for forbidden in (
            "private-token",
            "person@example.com",
            "sensitive connection failure detail",
            "/readyz",
            "authorization",
        ):
            self.assertNotIn(forbidden, rendered)

    @patch("builtins.print")
    def test_content_length_validation_is_bounded_before_body_read(self, _print) -> None:
        with patch.dict(
            "os.environ",
            {"NUTSNEWS_BACKEND_API_TOKEN": "test-token"},
            clear=False,
        ):
            with RunningApi(ReadyStore()) as api:
                invalid, _, _ = api.request(
                    "POST",
                    "/api/worker/db/load-feeds-for-shard",
                    headers={"authorization": "Bearer test-token", "content-length": "not-an-int"},
                )
                negative, _, _ = api.request(
                    "POST",
                    "/api/worker/db/load-feeds-for-shard",
                    headers={"authorization": "Bearer test-token", "content-length": "-1"},
                )
                oversized, _, _ = api.request(
                    "POST",
                    "/api/worker/db/load-feeds-for-shard",
                    headers={"authorization": "Bearer test-token", "content-length": "2000001"},
                )

        self.assertEqual(400, invalid)
        self.assertEqual(400, negative)
        self.assertEqual(413, oversized)

    @patch("builtins.print")
    def test_protected_post_routes_reject_query_suffixes_and_trailing_slashes(self, _print) -> None:
        with RunningApi(ReadyStore()) as api:
            query_status, _, query_body = api.request(
                "POST",
                "/api/worker/db/uplift-publish-articles-batch?token=must-not-be-accepted",
                body=b"{}",
                headers={"content-type": "application/json"},
            )
            slash_status, _, _ = api.request(
                "POST",
                "/api/worker/db/uplift-publish-articles-batch/",
                body=b"{}",
                headers={"content-type": "application/json"},
            )
            _, _, metrics_body = api.request("GET", "/metrics")

        self.assertEqual(400, query_status)
        self.assertEqual({"error": "query parameters are not supported"}, json.loads(query_body))
        self.assertEqual(404, slash_status)
        rendered = metrics_body.decode("utf-8")
        self.assertIn('operation="invalid_route"', rendered)
        self.assertNotIn('operation="uplift-publish-articles-batch"', rendered)
        self.assertNotIn("must-not-be-accepted", rendered)

    @patch("builtins.print")
    def test_liveness_and_compat_health_do_not_probe_postgresql(self, _print) -> None:
        class CountingStore(ReadyStore):
            def __init__(self) -> None:
                self.queries: list[str] = []

            def fetch_one(self, query: str, params: tuple = ()) -> dict:
                self.queries.append(query)
                return super().fetch_one(query, params)

        store = CountingStore()
        with RunningApi(store) as api:
            health_status, _, _ = api.request("GET", "/healthz?cache=bust")
            live_status, _, _ = api.request("GET", "/livez/")
            ready_status, _, _ = api.request("GET", "/readyz")

        self.assertEqual((200, 200, 200), (health_status, live_status, ready_status))
        self.assertEqual(["select 1 as ready"], store.queries)


if __name__ == "__main__":
    unittest.main()
