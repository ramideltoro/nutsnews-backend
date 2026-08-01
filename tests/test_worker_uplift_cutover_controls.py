from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from scripts import worker_uplift_cutover_control as control


CANDIDATE = "a" * 64
WATERMARK = "b" * 64
DEADLINE = "2026-08-04T04:00:00Z"


class WorkerUpliftCutoverControlTests(unittest.TestCase):
    def setUp(self):
        self.contract = control.load_json(control.DEFAULT_CONTRACT)

    def transition(self, row, name):
        return control.transition_state(
            row,
            name,
            candidate_sha256=CANDIDATE,
            watermark_sha256=WATERMARK,
            rollback_deadline_utc=DEADLINE,
        )

    def test_committed_shadow_state_is_single_writer_safe(self):
        row = control.simulated_initial()

        self.assertEqual(control.validate_state(row), [])
        self.assertEqual(row["active_ingestion_owner"], "legacy_shards")
        self.assertTrue(row["legacy_dispatch_enabled"])
        self.assertFalse(row["uplift_production_writes_enabled"])

    def test_complete_state_machine_never_overlaps_writers(self):
        row = control.simulated_initial()
        observed = [row]
        for transition in ("fence", "activate", "rollback-prepare", "rollback-finalize"):
            row = self.transition(row, transition)
            observed.append(row)

        self.assertEqual([item["state"] for item in observed], [
            "shadow",
            "fenced",
            "cutover_active",
            "rollback_pending",
            "shadow",
        ])
        self.assertTrue(all(not control.validate_state(item) for item in observed))
        self.assertTrue(all(
            not (item["legacy_dispatch_enabled"] and item["uplift_production_writes_enabled"])
            for item in observed
        ))

    def test_activate_requires_fenced_state(self):
        with self.assertRaisesRegex(control.ControlError, "requires state fenced"):
            self.transition(control.simulated_initial(), "activate")

    def test_transition_rerun_is_idempotent(self):
        fenced = self.transition(control.simulated_initial(), "fence")
        rerun = self.transition(fenced, "fence")

        self.assertEqual(rerun, fenced)

    def test_invalid_dual_writer_state_is_rejected(self):
        row = control.simulated_initial()
        row["uplift_production_writes_enabled"] = True
        row["publication_write_mode"] = "production"

        errors = control.validate_state(row)

        self.assertTrue(any("worker_uplift ownership" in error for error in errors))
        self.assertTrue(any("legacy dispatch" in error for error in errors))

    def test_dry_run_lists_every_transition_without_mutation(self):
        report = control.build_dry_run(self.contract, CANDIDATE, WATERMARK, DEADLINE)

        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["mutation_performed"])
        self.assertTrue(report["unchanged_failover_resources"])
        self.assertTrue(report["dns_failover_unchanged"])
        self.assertTrue(report["single_writer_invariants_pass"])
        self.assertEqual(
            [item["transition"] for item in report["transitions"]],
            ["fence", "activate", "rollback-prepare", "rollback-finalize"],
        )
        self.assertTrue(all(item["single_writer_invariants_pass"] for item in report["transitions"]))

    def test_isolated_rehearsal_restores_shadow_within_target(self):
        report = control.build_rehearsal(self.contract, CANDIDATE, WATERMARK, DEADLINE)

        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["mutation_performed"])
        self.assertFalse(report["production_targets_reachable"])
        self.assertEqual(report["restored_state"], "shadow")
        self.assertTrue(report["within_target"])
        self.assertTrue(report["single_writer_invariants_pass"])
        self.assertTrue(report["dns_failover_unchanged"])

    def test_each_injected_partial_failure_remains_single_writer_safe(self):
        for transition in ("fence", "activate", "rollback-prepare", "rollback-finalize"):
            with self.subTest(transition=transition):
                report = control.build_rehearsal(
                    self.contract,
                    CANDIDATE,
                    WATERMARK,
                    DEADLINE,
                    injected_failure=transition,
                )
                self.assertEqual(report["status"], "pass")
                self.assertFalse(report["completed_full_cycle"])
                self.assertTrue(report["single_writer_invariants_pass"])
                self.assertTrue(report["safe_failure_states"][0]["single_writer_safe"])

    def test_legacy_status_requires_every_failover_surface(self):
        payload = {
            "schemaVersion": 1,
            "state": "enabled",
            "enabled": True,
            "configurationValid": True,
            "legacyProductionOwner": "ramideltoro/nutsnews-worker",
            "disabledEffects": {
                "failoverWakeEnabled": True,
                "failoverStatusEnabled": True,
                "failoverActionsEnabled": True,
                "durableObjectAlarmsEnabled": True,
                "dnsReadbackEnabled": True,
                "liveOriginReadinessEnabled": True,
                "failoverAlertsEnabled": True,
                "analyticsEventsEnabled": True,
            },
        }

        safe = control.validate_legacy_status(payload, expected_enabled=True)
        self.assertEqual(safe["retainedFailoverSurfaceCount"], 8)

        payload["disabledEffects"]["dnsReadbackEnabled"] = False
        with self.assertRaisesRegex(control.ControlError, "missing failover surface"):
            control.validate_legacy_status(payload, expected_enabled=True)

    def test_final_decision_requires_exact_standing_authorization(self):
        decision = {
            "decision": "GO",
            "authorized_for_execution": True,
            "authorization": {
                "kind": "standing_bounded_authorization",
                "contract": "docs/worker-uplift-final-cutover-authorization.json",
                "owner_login": "ramideltoro",
                "owner_comment_url": control.FINAL_AUTH_COMMENT_URL,
                "owner_comment_body_sha256": control.FINAL_AUTH_COMMENT_SHA256,
                "scope_sha256": control.FINAL_AUTH_SCOPE_SHA256,
                "recurring_owner_approval_required": False,
            },
            "tracking_issue": "ramideltoro/nutsnews-worker#166",
            "execution_issue": "ramideltoro/nutsnews-worker#127",
            "candidate_sha256": CANDIDATE,
            "watermark_sha256": WATERMARK,
            "rollback_deadline_utc": DEADLINE,
            "evaluated_at_utc": "2026-08-02T01:00:00Z",
            "control_commit": "c" * 40,
            "blockers": [],
            "decision_scope": {
                "authorizes_issue": "ramideltoro/nutsnews-worker#127",
                "performs_cutover": False,
                "enables_production_writes_now": False,
                "changes_ingestion_owner_now": False,
                "changes_dns_or_failover": False,
            },
            "execution_window": {
                "start_utc": "2026-08-02T02:00:00Z",
                "end_utc": "2026-08-02T03:00:00Z",
            },
        }

        control.validate_final_decision(
            decision,
            candidate=CANDIDATE,
            watermark=WATERMARK,
            deadline=DEADLINE,
        )

        decision["authorization"]["scope_sha256"] = "d" * 64
        with self.assertRaisesRegex(control.ControlError, "exact #166 GO"):
            control.validate_final_decision(
                decision,
                candidate=CANDIDATE,
                watermark=WATERMARK,
                deadline=DEADLINE,
            )

    def test_apply_requires_the_frozen_execution_window(self):
        decision = {
            "decision": "GO",
            "authorized_for_execution": True,
            "authorization": {
                "kind": "standing_bounded_authorization",
                "contract": "docs/worker-uplift-final-cutover-authorization.json",
                "owner_login": "ramideltoro",
                "owner_comment_url": control.FINAL_AUTH_COMMENT_URL,
                "owner_comment_body_sha256": control.FINAL_AUTH_COMMENT_SHA256,
                "scope_sha256": control.FINAL_AUTH_SCOPE_SHA256,
                "recurring_owner_approval_required": False,
            },
            "tracking_issue": "ramideltoro/nutsnews-worker#166",
            "execution_issue": "ramideltoro/nutsnews-worker#127",
            "candidate_sha256": CANDIDATE,
            "watermark_sha256": WATERMARK,
            "rollback_deadline_utc": DEADLINE,
            "evaluated_at_utc": "2026-08-02T01:00:00Z",
            "control_commit": "c" * 40,
            "blockers": [],
            "decision_scope": {
                "authorizes_issue": "ramideltoro/nutsnews-worker#127",
                "performs_cutover": False,
                "enables_production_writes_now": False,
                "changes_ingestion_owner_now": False,
                "changes_dns_or_failover": False,
            },
            "execution_window": {
                "start_utc": "2026-08-02T02:00:00Z",
                "end_utc": "2026-08-02T03:00:00Z",
            },
        }

        control.validate_final_decision(
            decision,
            candidate=CANDIDATE,
            watermark=WATERMARK,
            deadline=DEADLINE,
            require_execution_window=True,
            now=datetime(2026, 8, 2, 2, 30, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(control.ControlError, "outside"):
            control.validate_final_decision(
                decision,
                candidate=CANDIDATE,
                watermark=WATERMARK,
                deadline=DEADLINE,
                require_execution_window=True,
                now=datetime(2026, 8, 2, 1, 59, tzinfo=timezone.utc),
            )

    def test_current_committed_decision_fails_closed(self):
        decision = control.load_json(control.DEFAULT_DECISION)

        with self.assertRaisesRegex(control.ControlError, "exact #166 GO"):
            control.validate_final_decision(
                decision,
                candidate=CANDIDATE,
                watermark=WATERMARK,
                deadline=DEADLINE,
            )

    def test_production_files_contain_only_fixed_safe_keys(self):
        dropin = control.production_api_dropin(CANDIDATE, WATERMARK)
        override = control.production_compose_override(
            CANDIDATE,
            WATERMARK,
            "backend-protected-publication-cutover-approved",
        )

        self.assertIn("NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=true", dropin)
        self.assertIn("NUTSNEWS_WORKER_UPLIFT_EXPECTED_CANDIDATE_SHA256", dropin)
        self.assertIn("NUTSNEWS_PUBLICATION_WRITE_MODE: production", override)
        self.assertNotIn("password", (dropin + override).lower())
        self.assertNotIn("token", (dropin + override).lower())

    def test_digest_and_deadline_inputs_fail_closed(self):
        with self.assertRaisesRegex(control.ControlError, "SHA-256"):
            control.require_sha256("not-a-digest", "candidate")
        with self.assertRaisesRegex(control.ControlError, "absolute UTC"):
            control.parse_utc("48 hours later", "deadline")


if __name__ == "__main__":
    unittest.main()
