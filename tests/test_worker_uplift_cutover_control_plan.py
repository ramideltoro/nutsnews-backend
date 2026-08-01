from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_cutover_control_plan as cutover_plan


class WorkerUpliftCutoverControlPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = cutover_plan.load_json(cutover_plan.DEFAULT_PLAN_PATH)

    def validate(self, plan=None, *, backend_checks_text=None):
        return cutover_plan.validate_plan(
            plan or self.plan,
            backend_checks_text=backend_checks_text,
        )

    def test_committed_plan_is_valid_and_non_mutating(self):
        self.assertEqual(self.validate(), [])

    def test_placeholder_owner_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["ownership"]["domains"][0]["primary_owner_login"] = "TBD"

        errors = self.validate(plan)

        self.assertTrue(any("authorized GitHub owner" in error for error in errors))
        self.assertTrue(any("placeholder text" in error for error in errors))

    def test_relative_only_rollback_deadline_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["rollback"]["planned_absolute_deadline_utc"] = "48 hours after cutover"

        errors = self.validate(plan)

        self.assertTrue(any("absolute ISO-8601 timestamp" in error for error in errors))

    def test_rollback_deadline_must_match_absolute_calculation(self):
        plan = copy.deepcopy(self.plan)
        plan["rollback"]["planned_absolute_deadline_utc"] = "2026-08-04T05:00:00Z"

        errors = self.validate(plan)

        self.assertTrue(any("reference plus 48 hours" in error for error in errors))

    def test_missing_ownership_domain_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["ownership"]["domains"] = plan["ownership"]["domains"][:-1]

        errors = self.validate(plan)

        self.assertTrue(any("ownership domain set" in error for error in errors))

    def test_missing_fail_closed_backup_control_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["ownership"]["domains"][0]["backup_control_id"] = ""

        errors = self.validate(plan)

        self.assertTrue(any("fail-closed backup control" in error for error in errors))

    def test_independent_human_backup_cannot_be_fabricated(self):
        plan = copy.deepcopy(self.plan)
        plan["ownership"]["independent_human_backup_available"] = True
        plan["ownership"]["domains"][0]["backup_owner_login"] = "invented-backup"

        errors = self.validate(plan)

        self.assertTrue(any("must not fabricate" in error for error in errors))
        self.assertTrue(any("no human backup owner exists" in error for error in errors))

    def test_plan_cannot_imply_cutover_or_write_authority(self):
        plan = copy.deepcopy(self.plan)
        plan["authority_boundary"]["this_plan_authorizes_cutover"] = True
        plan["authority_boundary"]["this_plan_authorizes_production_writes"] = True

        errors = self.validate(plan)

        self.assertIn("authority_boundary.this_plan_authorizes_cutover must be false", errors)
        self.assertIn(
            "authority_boundary.this_plan_authorizes_production_writes must be false",
            errors,
        )

    def test_current_legacy_owner_and_shadow_write_policy_are_required(self):
        plan = copy.deepcopy(self.plan)
        plan["current_safety_state"]["active_production_ingestion_owner"] = (
            "worker_uplift"
        )
        plan["current_safety_state"]["uplift_production_writes_enabled"] = True

        errors = self.validate(plan)

        self.assertTrue(any("legacy_shards must remain" in error for error in errors))
        self.assertIn(
            "current_safety_state.uplift_production_writes_enabled must be false",
            errors,
        )

    def test_125_authority_is_limited_to_150(self):
        plan = copy.deepcopy(self.plan)
        plan["authority_boundary"]["issue_125"] = "GO authorizes cutover."

        errors = self.validate(plan)

        self.assertTrue(any("limited to beginning #150 only" in error for error in errors))

    def test_missing_watermark_value_source_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["cutover_watermark"]["value_sources"] = plan["cutover_watermark"][
            "value_sources"
        ][:-1]

        errors = self.validate(plan)

        self.assertTrue(any("every value source" in error for error in errors))

    def test_observation_duration_cannot_be_shortened(self):
        plan = copy.deepcopy(self.plan)
        plan["observation_window"]["duration_hours"] = 1

        errors = self.validate(plan)

        self.assertTrue(any("exactly 48 hours" in error for error in errors))

    def test_missing_threshold_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["thresholds"] = plan["thresholds"][:-1]

        errors = self.validate(plan)

        self.assertTrue(any("threshold set" in error for error in errors))

    def test_pre_switch_phase_cannot_enable_uplift_writes(self):
        plan = copy.deepcopy(self.plan)
        plan["single_writer_handoff"][1]["uplift_production_writes"] = True

        errors = self.validate(plan)

        self.assertTrue(any("pre-switch handoff phases" in error for error in errors))

    def test_final_gate_refresh_fields_cannot_be_omitted(self):
        plan = copy.deepcopy(self.plan)
        plan["final_gate_refresh"]["required_exact_fields"] = plan[
            "final_gate_refresh"
        ]["required_exact_fields"][:-1]

        errors = self.validate(plan)

        self.assertTrue(any("final refresh field set" in error for error in errors))

    def test_backend_checks_must_enforce_validator_and_tests(self):
        workflow = cutover_plan.BACKEND_CHECKS_PATH.read_text(encoding="utf-8")
        workflow = workflow.replace(
            "run: python3 scripts/validate_worker_uplift_cutover_control_plan.py",
            "run: python3 -c pass",
        )

        errors = self.validate(backend_checks_text=workflow)

        self.assertIn("Backend Checks must run the cutover-control plan validator", errors)

    def test_value_bearing_fields_are_rejected_without_echoing_contents(self):
        plan = copy.deepcopy(self.plan)
        plan["evidence_custody"]["secret_value"] = "not-a-real-secret"

        errors = self.validate(plan)

        self.assertTrue(any("forbidden value-bearing key" in error for error in errors))
        self.assertNotIn("not-a-real-secret", "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
