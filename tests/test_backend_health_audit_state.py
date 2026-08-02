#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STATE_WRITER_PATH = (
    ROOT
    / "ansible"
    / "roles"
    / "backend_baseline"
    / "files"
    / "nutsnews_health_audit_state.py"
)
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-health-report.yml"
METRICS_TASKS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "metrics.yml"
DEFAULTS_PATH = ROOT / "ansible" / "roles" / "backend_baseline" / "defaults" / "main.yml"


def load_state_writer():
    spec = importlib.util.spec_from_file_location("nutsnews_health_audit_state", STATE_WRITER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load health-audit state writer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def event(*, run_at: str = "2026-08-01T12:00:00Z", conclusion: str = "success", critical: int = 0):
    return {
        "schema_version": 1,
        "safe_metadata_only": True,
        "source": "github_actions",
        "available": True,
        "conclusion": conclusion,
        "last_run_at_utc": run_at,
        "critical_checks": critical,
        "expected_interval_seconds": 86400,
    }


def report(*, conclusion: str = "success", critical: int = 0):
    return {
        "version": 1,
        "last_report_run_at": "2026-08-01T12:00:00Z",
        "last_report_success_at": "2026-08-01T12:00:00Z" if conclusion == "success" else None,
        "conclusion": conclusion,
        "last_error": None,
        "summary": {"critical": critical, "warning": 0, "healthy": 1},
        "delivery": {"status": "skipped", "detail": "send_email=false"},
        "ssh": {"commands": {"recent_errors": {"stdout": "token-that-must-not-escape"}}},
    }


class BackendHealthAuditStateTests(unittest.TestCase):
    def setUp(self):
        self.writer = load_state_writer()
        self.now = moment("2026-08-01T12:05:00Z")

    def test_report_producer_emits_only_the_closed_safe_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.json"
            event_path = Path(tmpdir) / "event.json"
            report_path.write_text(json.dumps(report()), encoding="utf-8")

            produced = self.writer.produce_event(report_path, now=self.now)
            event_path.write_text(json.dumps(produced), encoding="utf-8")
            persisted = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(set(produced), self.writer.EVENT_FIELDS)
        self.assertEqual(persisted, produced)
        self.assertEqual(produced["conclusion"], "success")
        self.assertEqual(produced["critical_checks"], 0)
        self.assertNotIn("ssh", persisted)
        self.assertNotIn("token-that-must-not-escape", json.dumps(persisted))

    def test_critical_report_produces_failure_event(self):
        value = report(conclusion="failure", critical=3)
        value["last_report_success_at"] = "2026-07-31T12:00:00Z"
        produced = self.writer.report_to_event(value, now=self.now)
        self.assertEqual(produced["conclusion"], "failure")
        self.assertEqual(produced["critical_checks"], 3)

    def test_report_producer_rejects_inconsistent_or_unbounded_values(self):
        boolean_version = report()
        boolean_version["version"] = True
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.report_to_event(boolean_version, now=self.now)

        inconsistent = report(conclusion="success", critical=1)
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.report_to_event(inconsistent, now=self.now)

        boolean_count = report()
        boolean_count["summary"]["critical"] = True
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.report_to_event(boolean_count, now=self.now)

        too_many = report(conclusion="failure", critical=self.writer.MAX_CRITICAL_CHECKS + 1)
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.report_to_event(too_many, now=self.now)

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "oversized-report.json"
            report_path.write_bytes(b" " * (self.writer.MAX_REPORT_BYTES + 1))
            with self.assertRaises(self.writer.StateValidationError):
                self.writer.produce_event(report_path, now=self.now)

    def test_strict_json_rejects_duplicates_non_finite_values_and_large_inputs(self):
        duplicate = b'{"schema_version":1,"schema_version":1}'
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.parse_json_object(duplicate, limit=100, context="event")
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.parse_json_object(b'{"value":NaN}', limit=100, context="event")
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.parse_json_object(b"{" + (b"x" * 100), limit=16, context="event")

    def test_event_validation_rejects_extra_fields_stale_time_and_future_time(self):
        boolean_schema = {**event(), "schema_version": True}
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.validate_event(boolean_schema, now=self.now)

        with_extra = {**event(), "request_id": "unbounded"}
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.validate_event(with_extra, now=self.now)

        with self.assertRaises(self.writer.StateValidationError):
            self.writer.validate_event(event(run_at="2026-08-01T05:00:00Z"), now=self.now)
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.validate_event(event(run_at="2026-08-01T12:11:00Z"), now=self.now)

    def test_failure_merge_preserves_last_success_and_counts_consecutive_failures(self):
        first, changed = self.writer.merge_event(event(), None, now=self.now)
        self.assertTrue(changed)
        self.assertEqual(first["last_success_at_utc"], "2026-08-01T12:00:00Z")
        self.assertEqual(first["consecutive_failures"], 0)

        failed_once, changed = self.writer.merge_event(
            event(run_at="2026-08-01T12:01:00Z", conclusion="failure", critical=2),
            first,
            now=self.now,
        )
        self.assertTrue(changed)
        self.assertEqual(failed_once["last_success_at_utc"], "2026-08-01T12:00:00Z")
        self.assertEqual(failed_once["consecutive_failures"], 1)

        failed_twice, _ = self.writer.merge_event(
            event(run_at="2026-08-01T12:02:00Z", conclusion="failure", critical=1),
            failed_once,
            now=self.now,
        )
        self.assertEqual(failed_twice["last_success_at_utc"], "2026-08-01T12:00:00Z")
        self.assertEqual(failed_twice["consecutive_failures"], 2)

        recovered, _ = self.writer.merge_event(
            event(run_at="2026-08-01T12:03:00Z"),
            failed_twice,
            now=self.now,
        )
        self.assertEqual(recovered["last_success_at_utc"], "2026-08-01T12:03:00Z")
        self.assertEqual(recovered["consecutive_failures"], 0)

    def test_replay_is_idempotent_but_stale_and_conflicting_events_are_rejected(self):
        previous, _ = self.writer.merge_event(event(), None, now=self.now)
        replayed, changed = self.writer.merge_event(event(), previous, now=self.now)
        self.assertFalse(changed)
        self.assertEqual(replayed, previous)

        with self.assertRaises(self.writer.StateValidationError):
            self.writer.merge_event(
                event(run_at="2026-08-01T11:59:59Z"),
                previous,
                now=self.now,
            )
        with self.assertRaises(self.writer.StateValidationError):
            self.writer.merge_event(
                event(run_at="2026-08-01T12:00:00Z", conclusion="failure", critical=1),
                previous,
                now=self.now,
            )

    def test_writer_uses_atomic_regular_file_and_rejects_invalid_prior_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "last-run.json"
            lock_path = root / ".last-run.lock"
            persisted, changed = self.writer.write_state(
                event(), state_path=state_path, lock_path=lock_path, now=self.now
            )
            self.assertTrue(changed)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), persisted)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(list(root.glob(".last-run.json.*")), [])

            original = state_path.read_bytes()
            invalid = {**persisted, "unexpected": "field"}
            state_path.write_text(json.dumps(invalid), encoding="utf-8")
            invalid_bytes = state_path.read_bytes()
            with self.assertRaises(self.writer.StateValidationError):
                self.writer.write_state(
                    event(run_at="2026-08-01T12:01:00Z"),
                    state_path=state_path,
                    lock_path=lock_path,
                    now=self.now,
                )
            self.assertEqual(state_path.read_bytes(), invalid_bytes)
            self.assertNotEqual(invalid_bytes, original)

    def test_oversized_existing_state_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "last-run.json"
            lock_path = root / ".last-run.lock"
            oversized = b"x" * (self.writer.MAX_STATE_BYTES + 1)
            state_path.write_bytes(oversized)
            with self.assertRaises(self.writer.StateValidationError):
                self.writer.write_state(
                    event(), state_path=state_path, lock_path=lock_path, now=self.now
                )
            self.assertEqual(state_path.read_bytes(), oversized)

    def test_symbolic_link_state_target_is_rejected_without_touching_victim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            victim = root / "victim.json"
            state_path = root / "last-run.json"
            lock_path = root / ".last-run.lock"
            victim.write_text("victim\n", encoding="utf-8")
            state_path.symlink_to(victim)
            with self.assertRaises(self.writer.StateValidationError):
                self.writer.write_state(
                    event(), state_path=state_path, lock_path=lock_path, now=self.now
                )
            self.assertEqual(victim.read_text(encoding="utf-8"), "victim\n")

    def test_workflow_and_ansible_keep_publication_fixed_and_default_off(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        tasks = METRICS_TASKS_PATH.read_text(encoding="utf-8")
        defaults = DEFAULTS_PATH.read_text(encoding="utf-8")
        writer_source = STATE_WRITER_PATH.read_text(encoding="utf-8")

        self.assertIn("vars.NUTSNEWS_HEALTH_AUDIT_REMOTE_PUBLISH_ENABLED == 'true'", workflow)
        self.assertIn("sudo -n /usr/local/sbin/nutsnews-health-audit-state write", workflow)
        self.assertIn('NUTSNEWS_BACKEND_HOST\" != \"65.75.201.18', workflow)
        self.assertNotIn("secrets.NUTSNEWS_BACKEND_ANSIBLE_USER", workflow)
        self.assertEqual(workflow.count('ssh_user="rami"'), 2)
        self.assertIn('--ssh-user "rami"', workflow)
        self.assertIn("python3 ansible/roles/backend_baseline/files/nutsnews_health_audit_state.py event", workflow)
        self.assertIn("report_exit=0", workflow)
        self.assertIn("exit \"$report_exit\"", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("src: nutsnews_health_audit_state.py", tasks)
        self.assertIn("Validate fixed bounded health-audit state paths", tasks)
        self.assertIn("dest: \"{{ backend_metrics_health_audit_state_writer_path }}\"", tasks)
        self.assertIn("owner: root", tasks)
        self.assertIn("group: root", tasks)
        self.assertIn('mode: "0755"', tasks)
        self.assertIn("validate: /usr/sbin/visudo -cf %s", tasks)
        self.assertIn(
            "rami ALL=(root) NOPASSWD: /usr/local/sbin/nutsnews-health-audit-state write",
            tasks,
        )
        self.assertIn(
            "backend_metrics_health_audit_state_writer_path: /usr/local/sbin/nutsnews-health-audit-state",
            defaults,
        )
        self.assertNotIn("NUTSNEWS_HEALTH_AUDIT_REMOTE_PUBLISH_ENABLED:", workflow)
        self.assertTrue(writer_source.startswith("#!/usr/bin/python3\n"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.writer.parse_args(["write", "--state-path", "/tmp/redirected.json"])

        with patch.object(self.writer.os, "geteuid", return_value=0), redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.writer.main(["event", "--report", "/root/arbitrary.json"]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
