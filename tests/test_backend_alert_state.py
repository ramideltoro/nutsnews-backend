from __future__ import annotations

import unittest

from scripts import backend_alert_state


class BackendAlertStateTests(unittest.TestCase):
    def test_fingerprint_ignores_volatile_numbers_times_and_ids(self):
        base = {
            "source": "health",
            "service": "package_updates",
            "severity": "warning",
            "failure_class": "maintenance",
            "message": "upgradable_packages=2 at 2026-07-17T01:00:00Z snapshot=1a999564872b8b31",
        }
        changed = dict(base)
        changed["message"] = "upgradable_packages=86 at 2026-07-18T02:00:00Z snapshot=abcdef1299990000"
        self.assertEqual(backend_alert_state.alert_fingerprint(base), backend_alert_state.alert_fingerprint(changed))
        self.assertEqual(
            backend_alert_state.normalize_message(changed["message"]),
            "upgradable_packages=<num> at <time> snapshot=<id>",
        )

    def test_different_failure_classes_remain_distinct(self):
        first = {"source": "synthetic", "service": "backend", "severity": "critical", "failure_class": "dns", "message": "backend failed"}
        second = dict(first)
        second["failure_class"] = "tls"
        self.assertNotEqual(backend_alert_state.alert_fingerprint(first), backend_alert_state.alert_fingerprint(second))

    def test_repeated_alert_is_suppressed_inside_cooldown(self):
        alert = {"source": "health", "service": "disk", "severity": "warning", "failure_class": "disk_pressure", "message": "root_disk_used_percent=82%"}
        first = backend_alert_state.evaluate_alerts(None, [alert], "2026-07-17T01:00:00Z", cooldown_seconds=3600)
        second = backend_alert_state.evaluate_alerts(
            {"alert_state": first["state"]},
            [alert],
            "2026-07-17T01:10:00Z",
            cooldown_seconds=3600,
        )
        self.assertEqual(second["summary"]["notification_count"], 0)
        self.assertEqual(second["summary"]["suppressed_count"], 1)
        self.assertEqual(second["summary"]["suppressed_total_count"], 1)
        self.assertEqual(second["suppressed"][0]["notification_reason"], "cooldown")
        self.assertEqual(second["summary"]["last_error"], "root_disk_used_percent=82%")

    def test_recovery_notification_is_emitted(self):
        alert = {"source": "synthetic", "service": "backend_healthz", "severity": "critical", "failure_class": "backend_health", "message": "expected 200 observed 500"}
        first = backend_alert_state.evaluate_alerts(None, [alert], "2026-07-17T01:00:00Z", cooldown_seconds=3600)
        recovered = backend_alert_state.evaluate_alerts(
            {"alert_state": first["state"]},
            [],
            "2026-07-17T01:30:00Z",
            cooldown_seconds=3600,
        )
        self.assertEqual(recovered["summary"]["recovered_count"], 1)
        self.assertEqual(recovered["notifications"][0]["status"], "recovered")

    def test_last_error_uses_current_alert_order_not_state_order(self):
        first = {"source": "health", "service": "a", "severity": "warning", "failure_class": "maintenance", "message": "first"}
        second = {"source": "health", "service": "b", "severity": "warning", "failure_class": "maintenance", "message": "second"}
        previous = backend_alert_state.evaluate_alerts(None, [second, first], "2026-07-17T01:00:00Z", cooldown_seconds=3600)
        current = backend_alert_state.evaluate_alerts(
            {"alert_state": previous["state"]},
            [first, second],
            "2026-07-17T01:10:00Z",
            cooldown_seconds=3600,
        )
        self.assertEqual(current["summary"]["last_error"], "second")

    def test_recurring_alert_after_recovery_resets_first_seen_and_suppression(self):
        alert = {"source": "health", "service": "package_updates", "severity": "warning", "failure_class": "maintenance", "message": "upgradable_packages=1"}
        first = backend_alert_state.evaluate_alerts(None, [alert], "2026-07-17T01:00:00Z", cooldown_seconds=3600)
        recovered = backend_alert_state.evaluate_alerts(
            {"alert_state": first["state"]},
            [],
            "2026-07-17T01:20:00Z",
            cooldown_seconds=3600,
        )
        recurring = backend_alert_state.evaluate_alerts(
            {"alert_state": recovered["state"]},
            [alert],
            "2026-07-17T02:00:00Z",
            cooldown_seconds=3600,
        )
        notification = recurring["notifications"][0]
        self.assertEqual(notification["notification_reason"], "new")
        self.assertEqual(notification["first_seen_at"], "2026-07-17T02:00:00Z")
        self.assertEqual(notification["suppressed_count"], 0)


if __name__ == "__main__":
    unittest.main()
