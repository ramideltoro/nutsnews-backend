from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import backend_synthetic_monitor


class BackendSyntheticMonitorTests(unittest.TestCase):
    def test_public_checks_are_safe_get_requests(self):
        checks = backend_synthetic_monitor.public_checks()
        self.assertGreaterEqual(len(checks), 5)
        names = {check.name for check in checks}
        self.assertIn("frontend_www_home", names)
        self.assertIn("backend_readyz", names)
        self.assertIn("supabase_platform_status", names)
        self.assertTrue(all(check.url.startswith("https://") for check in checks))

    def test_admin_backend_operations_are_required_read_only_posts(self):
        operations = backend_synthetic_monitor.admin_backend_operations()
        expected = {
            "load-admin-production-readiness",
            "load-admin-article-reviews",
            "load-admin-article-engagement",
            "load-admin-ai-usage",
            "load-admin-local-ai",
            "load-admin-translation-quality",
            "load-admin-guardrails",
            "load-admin-worker-shards",
            "load-admin-rss-feed-health",
            "load-admin-feed-management",
            "load-admin-audit-log",
            "load-admin-runtime-feature-flags",
        }
        self.assertEqual({operation.name for operation in operations}, expected)
        self.assertTrue(all(operation.name.startswith("load-admin-") for operation in operations))
        self.assertTrue(all(operation.body.get("providerMode") == "backend_postgres_primary" for operation in operations))
        self.assertNotIn("record-quota-usage-event", {operation.name for operation in operations})

    def test_missing_admin_backend_token_is_critical(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            checks = backend_synthetic_monitor.run_admin_backend_operations({}, "2026-07-17T01:00:00Z")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["name"], "admin_backend_operations_config")
        self.assertEqual(checks[0]["status"], "critical")
        self.assertEqual(checks[0]["failure_class"], "admin_backend_configuration")
        self.assertIn("NUTSNEWS_BACKEND_API_TOKEN", checks[0]["failure_detail"])

    def test_admin_operation_posts_token_but_redacts_report(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def getcode(self):
                return 200

            def read(self, _limit=-1):
                return b'{"rows":[]}'

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        operation = backend_synthetic_monitor.AdminBackendOperation(
            name="load-admin-ai-usage",
            body={"providerMode": "backend_postgres_primary", "limit": 1},
            timeout=7,
        )
        with mock.patch.object(backend_synthetic_monitor.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = backend_synthetic_monitor.run_admin_backend_operation(
                operation,
                "https://backend.nutsnews.com/api/app/db",
                "secret-admin-token",
                None,
                "2026-07-17T01:00:00Z",
            )

        self.assertEqual(captured["authorization"], "Bearer secret-admin-token")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["row_count"], 0)
        self.assertNotIn("secret-admin-token", json.dumps(result))

    def test_admin_operation_reads_full_success_json_response(self):
        filler = "x" * 40000
        payload = json.dumps({"rows": [{"filler": filler}]})

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def getcode(self):
                return 200

            def read(self, _limit=-1):
                if _limit not in (-1, None):
                    return payload[:_limit].encode()
                return payload.encode()

        operation = backend_synthetic_monitor.AdminBackendOperation(
            name="load-admin-ai-usage",
            body={"providerMode": "backend_postgres_primary", "limit": 1},
        )
        with mock.patch.object(backend_synthetic_monitor.urllib.request, "urlopen", return_value=FakeResponse()):
            result = backend_synthetic_monitor.run_admin_backend_operation(
                operation,
                "https://backend.nutsnews.com/api/app/db",
                "secret-admin-token",
                None,
                "2026-07-17T01:00:00Z",
            )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["row_count"], 1)
        self.assertNotIn(filler, json.dumps(result))

    def test_admin_article_reviews_requires_healthy_version_report(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def getcode(self):
                return 200

            def read(self, _limit=-1):
                return json.dumps(
                    {
                        "rows": [
                            {
                                "versionReportRows": [],
                                "versionReportError": "permission denied for table sensitive_internal_name",
                            }
                        ]
                    }
                ).encode()

        def fake_urlopen(_request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse()

        operation = backend_synthetic_monitor.AdminBackendOperation(
            name="load-admin-article-reviews",
            body={"providerMode": "backend_postgres_primary", "limit": 1},
        )
        with mock.patch.object(backend_synthetic_monitor.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = backend_synthetic_monitor.run_admin_backend_operation(
                operation,
                "https://backend.nutsnews.com/api/app/db",
                "secret-admin-token",
                "2026-07-17T00:00:00Z",
                "2026-07-17T01:00:00Z",
            )

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["failure_class"], "admin_backend_snapshot_error")
        self.assertIn("versionReportError_present=true", result["failure_detail"])
        self.assertEqual(result["last_success_at"], "2026-07-17T00:00:00Z")
        self.assertNotIn("permission denied", json.dumps(result))
        self.assertNotIn("secret-admin-token", json.dumps(result))

    def test_admin_operation_http_failure_names_operation_route_and_redacts_body(self):
        operation = backend_synthetic_monitor.AdminBackendOperation(
            name="load-admin-ai-usage",
            body={"providerMode": "backend_postgres_primary", "limit": 1},
        )
        route = "https://backend.nutsnews.com/api/app/db/load-admin-ai-usage"
        error = backend_synthetic_monitor.urllib.error.HTTPError(
            route,
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"rows":[{"secret":"sensitive row data"}]}'),
        )
        with mock.patch.object(backend_synthetic_monitor.urllib.request, "urlopen", side_effect=error):
            result = backend_synthetic_monitor.run_admin_backend_operation(
                operation,
                "https://backend.nutsnews.com/api/app/db",
                "secret-admin-token",
                "2026-07-17T00:00:00Z",
                "2026-07-17T01:00:00Z",
            )

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["failure_class"], "admin_backend_http")
        self.assertIn("load-admin-ai-usage", result["failure_detail"])
        self.assertIn(route, result["failure_detail"])
        self.assertIn("observed_status=503", result["failure_detail"])
        self.assertEqual(result["last_success_at"], "2026-07-17T00:00:00Z")
        self.assertNotIn("sensitive row data", json.dumps(result))
        self.assertNotIn("secret-admin-token", json.dumps(result))

    def test_previous_state_preserves_last_success_for_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "previous.json"
            path.write_text(
                json.dumps({"checks": [{"name": "backend_readyz", "last_success_at": "2026-07-17T00:00:00Z"}]}),
                encoding="utf-8",
            )
            self.assertEqual(backend_synthetic_monitor.load_previous(path), {"backend_readyz": "2026-07-17T00:00:00Z"})

    def test_failed_check_uses_previous_last_success(self):
        check = backend_synthetic_monitor.SyntheticCheck(
            name="backend_readyz",
            url="https://backend.nutsnews.com/readyz",
            expected_statuses=(200,),
            expected_json=(("status", "ready"), ("ready", True)),
            expected_header_contains=(("cache-control", "no-store"),),
            failure_class="backend_readiness",
        )
        with mock.patch.object(backend_synthetic_monitor, "opener_for", side_effect=TimeoutError("timeout")):
            result = backend_synthetic_monitor.run_check(check, "2026-07-17T00:00:00Z", "2026-07-17T01:00:00Z")
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["failure_class"], "timeout")
        self.assertEqual(result["last_success_at"], "2026-07-17T00:00:00Z")

    def test_backend_readiness_check_validates_body_identity_and_no_cache_headers(self):
        check = next(
            item for item in backend_synthetic_monitor.public_checks() if item.name == "backend_readyz"
        )

        class FakeResponse:
            def __init__(self, payload, headers):
                self.payload = payload
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def getcode(self):
                return 200

            def read(self, _limit=-1):
                return json.dumps(self.payload).encode()

        class FakeOpener:
            def __init__(self, response):
                self.response = response

            def open(self, _request, timeout):
                self.timeout = timeout
                return self.response

        ready_payload = {
            "status": "ready",
            "ready": True,
            "service": "nutsnews-worker-db-api",
            "serviceVersion": "1.1.0",
            "buildRevision": "abc123",
            "deploymentEnvironment": "production",
            "dependencies": {"postgresql": "ready"},
        }
        with mock.patch.object(
            backend_synthetic_monitor,
            "opener_for",
            return_value=FakeOpener(
                FakeResponse(ready_payload, {"cache-control": "no-store", "pragma": "no-cache"})
            ),
        ):
            result = backend_synthetic_monitor.run_check(
                check,
                None,
                "2026-07-17T01:00:00Z",
            )
        self.assertEqual("healthy", result["status"])

        with mock.patch.object(
            backend_synthetic_monitor,
            "opener_for",
            return_value=FakeOpener(
                FakeResponse(
                    {**ready_payload, "ready": False},
                    {"cache-control": "public, max-age=300", "pragma": "cache"},
                )
            ),
        ):
            result = backend_synthetic_monitor.run_check(
                check,
                "2026-07-17T00:00:00Z",
                "2026-07-17T01:00:00Z",
            )
        self.assertEqual("critical", result["status"])
        self.assertIn("json_match=false", result["failure_detail"])
        self.assertIn("header_match=false", result["failure_detail"])
        self.assertEqual("2026-07-17T00:00:00Z", result["last_success_at"])

    def test_redaction_masks_email_and_url_secret(self):
        redacted = backend_synthetic_monitor.redact("postgres://user:secret@example.com/db person@example.com")
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("person@example.com", redacted)

    def test_write_summary_uses_real_newlines_and_alert_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "summary.md"
            backend_synthetic_monitor.write_summary(
                path,
                {
                    "status": "critical",
                    "summary": {"healthy": 4, "critical": 1},
                    "source": {"provider": "github_actions", "location": "runner"},
                    "alerting": {
                        "summary": {
                            "active_alert_count": 1,
                            "notification_count": 0,
                            "suppressed_count": 1,
                            "recovered_count": 0,
                            "last_sent_at": "2026-07-17T01:00:00Z",
                            "last_error": "expected_status=200 observed_status=500",
                        }
                    },
                    "checks": [
                        {
                            "name": "backend_readyz",
                            "status": "critical",
                            "http_status": 500,
                            "failure_class": "backend_readiness",
                            "last_success_at": "2026-07-17T00:00:00Z",
                        }
                    ],
                },
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("\n| Check | Status | HTTP | Failure class | Last success |\n", text)
        self.assertIn("- suppressed: `1`", text)
        self.assertNotIn("\\n", text)

    def test_email_is_skipped_when_healthy(self):
        report = {
            "checks": [{"status": "healthy"}],
            "generated_at_utc": "2026-07-17T01:00:00Z",
            "source": {"provider": "github_actions", "location": "runner"},
            "alerting": {"summary": {"suppressed_count": 0}, "notifications": []},
        }
        config = backend_synthetic_monitor.SmtpConfig("smtp.example.com", 587, True, "user", "pass", "from@example.com", ["to@example.com"])
        self.assertEqual(backend_synthetic_monitor.send_failure_email(config, report), {"status": "skipped", "detail": "no unsuppressed notifications; suppressed=0"})

    def test_workflow_wires_admin_backend_token_to_scheduled_monitor(self):
        workflow = Path(".github/workflows/backend-synthetic-monitor.yml").read_text(encoding="utf-8")
        self.assertIn("NUTSNEWS_BACKEND_API_URL: ${{ vars.NUTSNEWS_BACKEND_API_URL || 'https://backend.nutsnews.com/api/app/db' }}", workflow)
        self.assertIn("NUTSNEWS_BACKEND_API_TOKEN: ${{ secrets.NUTSNEWS_BACKEND_API_TOKEN }}", workflow)
        self.assertNotIn("--skip-admin-backend", workflow)
        script = Path("scripts/backend_synthetic_monitor.py").read_text(encoding="utf-8")
        self.assertIn("https://backend.nutsnews.com/readyz", script)
        self.assertNotIn("https://backend.nutsnews.com/healthz", script)


if __name__ == "__main__":
    unittest.main()
