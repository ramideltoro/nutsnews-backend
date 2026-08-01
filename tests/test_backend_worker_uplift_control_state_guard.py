from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import backend_worker_uplift_control_state_guard as guard


ROOT = Path(__file__).resolve().parents[1]

def row_for(state: str) -> dict:
    owner, legacy, scheduler, writes, publication = guard.EXPECTED_STATES[state]
    return {
        "state": state,
        "active_ingestion_owner": owner,
        "legacy_dispatch_enabled": legacy,
        "uplift_scheduler_enabled": scheduler,
        "uplift_production_writes_enabled": writes,
        "publication_write_mode": publication,
        "candidate_sha256": "a" * 64 if state != "shadow" else None,
        "watermark_sha256": "b" * 64 if state != "shadow" else None,
        "rollback_deadline_utc": "2026-08-03T21:00:00Z" if state != "shadow" else None,
    }


class BackendWorkerUpliftControlStateGuardTests(unittest.TestCase):
    def test_all_four_exact_state_tuples_are_recognized(self):
        for state in guard.EXPECTED_STATES:
            with self.subTest(state=state):
                report = guard.evaluate(row_for(state))
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["state"], state)
                self.assertEqual(report["generic_mutation_safe"], state == "shadow")

    def test_malformed_tuple_fails_closed(self):
        row = row_for("cutover_active")
        row["uplift_production_writes_enabled"] = False

        report = guard.evaluate(row)

        self.assertEqual(report["status"], "error")
        self.assertFalse(report["generic_mutation_safe"])
        self.assertIn("control_state_tuple_mismatch", report["errors"])

    def test_unknown_state_fails_closed(self):
        row = row_for("shadow")
        row["state"] = "invented"

        report = guard.evaluate(row)

        self.assertEqual(report["status"], "error")
        self.assertIn("unknown_control_state", report["errors"])

    def test_unavailable_database_has_no_static_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.env"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = guard.main(
                    ["--db-env-file", str(missing), "--require-maintenance-safe"]
                )

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["state"], "unknown")
        self.assertFalse(report["generic_mutation_safe"])
        self.assertEqual(report["errors"][0], "control_state_unavailable_or_invalid")

    def test_active_cutover_blocks_generic_mutation(self):
        output = io.StringIO()
        with mock.patch.object(
            guard, "read_control_row", return_value=row_for("cutover_active")
        ), contextlib.redirect_stdout(output):
            exit_code = guard.main(["--require-maintenance-safe"])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "block")
        self.assertIn("generic_mutation_blocked_by_cutover_state", report["errors"])

    def test_database_error_details_are_not_emitted(self):
        output = io.StringIO()
        with mock.patch.object(
            guard,
            "read_control_row",
            side_effect=Exception("postgresql://user:secret@example.invalid/db"),
        ), contextlib.redirect_stdout(output):
            exit_code = guard.main(["--require-maintenance-safe"])

        self.assertEqual(exit_code, 1)
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["errors"][0],
            "control_state_unavailable_or_invalid",
        )

    def test_generic_mutation_workflows_are_guarded_before_mutation(self):
        ansible = (
            ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
        ).read_text(encoding="utf-8")
        runtime = (
            ROOT / ".github/workflows/backend-worker-runtime-operations.yml"
        ).read_text(encoding="utf-8")

        ansible_guard = ansible.index(
            "- name: Block generic Ansible mutation outside exact shadow state"
        )
        ansible_guard_end = ansible.index(
            "- name: Check RabbitMQ credential readiness", ansible_guard
        )
        ansible_guard_block = ansible[ansible_guard:ansible_guard_end]
        self.assertIn("if: inputs.run_mode == 'apply'", ansible_guard_block)
        self.assertNotIn("deployment_scope", ansible_guard_block)
        self.assertLess(
            ansible_guard,
            ansible.index("- name: Reset fixed one-shot failure state"),
        )
        self.assertLess(ansible_guard, ansible.index("ansible-playbook"))

        runtime_guard = runtime.index(
            "- name: Block generic runtime mutation during an active cutover"
        )
        self.assertLess(
            runtime_guard,
            runtime.index("- name: Run fixed protected worker runtime operation"),
        )
        for workflow in (ansible, runtime):
            self.assertIn(
                "sudo -n /usr/bin/python3 - --require-maintenance-safe",
                workflow,
            )
            self.assertIn(
                "< scripts/backend_worker_uplift_control_state_guard.py",
                workflow,
            )


if __name__ == "__main__":
    unittest.main()
