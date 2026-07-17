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

    def test_email_is_skipped_when_healthy(self):
        report = {"checks": [{"status": "healthy"}], "generated_at_utc": "2026-07-17T01:00:00Z", "source": {"provider": "github_actions", "location": "runner"}}
        config = backend_synthetic_monitor.SmtpConfig("smtp.example.com", 587, True, "user", "pass", "from@example.com", ["to@example.com"])
        self.assertEqual(backend_synthetic_monitor.send_failure_email(config, report), {"status": "skipped", "detail": "no failures"})


if __name__ == "__main__":
    unittest.main()
