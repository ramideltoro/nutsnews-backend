from __future__ import annotations

import json
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
        self.assertIn("backend_healthz", names)
        self.assertIn("supabase_platform_status", names)
        self.assertTrue(all(check.url.startswith("https://") for check in checks))

    def test_previous_state_preserves_last_success_for_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "previous.json"
            path.write_text(
                json.dumps({"checks": [{"name": "backend_healthz", "last_success_at": "2026-07-17T00:00:00Z"}]}),
                encoding="utf-8",
            )
            self.assertEqual(backend_synthetic_monitor.load_previous(path), {"backend_healthz": "2026-07-17T00:00:00Z"})

    def test_failed_check_uses_previous_last_success(self):
        check = backend_synthetic_monitor.SyntheticCheck(
            name="backend_healthz",
            url="https://backend.nutsnews.com/healthz",
            expected_statuses=(200,),
            body_contains="ok",
            failure_class="backend_health",
        )
        with mock.patch.object(backend_synthetic_monitor, "opener_for", side_effect=TimeoutError("timeout")):
            result = backend_synthetic_monitor.run_check(check, "2026-07-17T00:00:00Z", "2026-07-17T01:00:00Z")
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["failure_class"], "timeout")
        self.assertEqual(result["last_success_at"], "2026-07-17T00:00:00Z")

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
                            "name": "backend_healthz",
                            "status": "critical",
                            "http_status": 500,
                            "failure_class": "backend_health",
                            "last_success_at": "2026-07-17T00:00:00Z",
                        }
                    ],
                },
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("\n| Endpoint | Status | HTTP | Failure class | Last success |\n", text)
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


if __name__ == "__main__":
    unittest.main()
