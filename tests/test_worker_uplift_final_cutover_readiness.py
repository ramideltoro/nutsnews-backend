from __future__ import annotations

import copy
import contextlib
import io
import unittest

from scripts import validate_worker_uplift_final_cutover_readiness as readiness


class WorkerUpliftFinalCutoverReadinessTests(unittest.TestCase):
    def setUp(self):
        self.authorization = readiness.load_json(readiness.AUTHORIZATION_PATH)
        self.decision = readiness.load_json(readiness.DECISION_PATH)
        self.candidate = readiness.load_json(readiness.CANDIDATE_PATH)
        self.receipt = readiness.load_json(readiness.EXECUTION_RECEIPT_PATH)

    def test_committed_go_is_valid_and_bound_to_exact_candidate(self):
        self.assertEqual(
            readiness.validate_decision(
                self.decision,
                self.authorization,
                candidate=self.candidate,
                require_go=True,
            ),
            [],
        )
        self.assertEqual(
            self.decision["candidate_sha256"],
            self.candidate["manifest_sha256"],
        )
        self.assertEqual(
            self.decision["decision_scope"]["authorizes_issue"],
            "ramideltoro/nutsnews-worker#127",
        )
        self.assertFalse(self.decision["decision_scope"]["performs_cutover"])

    def test_standing_authorization_removes_recurring_owner_prompt(self):
        errors = readiness.validate_authorization(self.authorization)

        self.assertEqual(errors, [])
        standing = self.authorization["authorization"]
        self.assertFalse(standing["per_release_owner_approval_required"])
        self.assertFalse(standing["first_run_owner_approval_required"])
        self.assertFalse(standing["routine_environment_wait_owner_response_required"])
        self.assertTrue(standing["exact_environment_wait_api_approval_allowed"])
        self.assertTrue(standing["machine_validated_go_allowed"])
        self.assertFalse(standing["risk_waiver"])

    def test_scope_drift_invalidates_standing_authorization(self):
        contract = copy.deepcopy(self.authorization)
        contract["scope_fingerprint"]["allowed_operations"].append("arbitrary")

        errors = readiness.validate_authorization(contract)

        self.assertTrue(any("scope changed" in error for error in errors))
        self.assertTrue(any("scope digest changed" in error for error in errors))

    def test_scope_expansion_is_not_a_risk_waiver(self):
        contract = copy.deepcopy(self.authorization)
        contract["forbidden_authorities"].remove("risk_acceptance")

        errors = readiness.validate_authorization(contract)

        self.assertIn("standing authorization exclusions changed", errors)

    def test_go_cannot_use_reviewer_or_automation_metadata_instead_of_contract(self):
        decision = copy.deepcopy(self.decision)
        decision["authorization"]["kind"] = "advisory_environment_approval"
        decision["authorized_for_execution"] = True
        decision["decision"] = "GO"
        decision["blockers"] = []

        errors = readiness.validate_decision(
            decision,
            self.authorization,
            candidate=self.candidate,
            require_go=True,
        )

        self.assertIn(
            "decision is not bound to the exact standing authorization",
            errors,
        )

    def test_no_go_cannot_freeze_partial_execution_inputs(self):
        decision = copy.deepcopy(self.decision)
        decision["decision"] = "NO-GO"
        decision["authorized_for_execution"] = False
        decision["blockers"] = ["test blocker"]
        decision["candidate_sha256"] = "a" * 64
        for field in (
            "watermark_sha256",
            "rollback_deadline_utc",
            "observation_start_utc",
            "observation_end_utc",
            "control_commit",
        ):
            decision[field] = None

        errors = readiness.validate_decision(decision, self.authorization)

        self.assertIn("NO-GO must not freeze candidate_sha256", errors)

    def test_threshold_digest_drift_fails_closed(self):
        decision = copy.deepcopy(self.decision)
        decision["thresholds"]["sha256"] = "a" * 64

        errors = readiness.validate_decision(
            decision,
            self.authorization,
            candidate=self.candidate,
            require_go=True,
        )

        self.assertIn(
            "threshold digest does not match the source-controlled plan",
            errors,
        )

    def test_value_bearing_keys_are_rejected_without_echoing_values(self):
        contract = copy.deepcopy(self.authorization)
        contract["secret_value"] = "not-a-real-secret"

        errors = readiness.validate_authorization(contract)

        self.assertTrue(any("forbidden value-bearing key" in error for error in errors))
        self.assertNotIn("not-a-real-secret", "\n".join(errors))

    def test_execution_receipt_consumes_apply_and_rollback_authority(self):
        self.assertEqual(
            readiness.validate_execution_receipt(self.receipt, self.decision),
            [],
        )
        self.assertEqual(readiness.validate_repository(), [])
        self.assertIn(
            "exact #166 GO apply authority has already been consumed",
            readiness.validate_repository(require_go=True),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(readiness.main([]), 0)
        self.assertIn("historical_decision=GO", output.getvalue())
        self.assertIn("execution_status=rolled_back", output.getvalue())
        self.assertIn("apply_authorized=false", output.getvalue())
        self.assertIn("rollback_authorized=false", output.getvalue())
        self.assertNotIn("valid; decision=GO", output.getvalue())

    def test_execution_receipt_drift_fails_closed(self):
        receipt = copy.deepcopy(self.receipt)
        receipt["rollback_progress"]["post_finalize_reconfirmation"][
            "controller_status"
        ]["artifact_id"] += 1

        errors = readiness.validate_execution_receipt(receipt, self.decision)

        self.assertIn("completed rollback evidence drifted", errors)

    def test_legacy_transition_chain_drift_fails_closed(self):
        receipt = copy.deepcopy(self.receipt)
        receipt["legacy_scheduling_evidence"]["transition_chain"][0][
            "workflow_run"
        ] += 1

        errors = readiness.validate_execution_receipt(receipt, self.decision)

        self.assertIn("legacy scheduling evidence drifted", errors)


if __name__ == "__main__":
    unittest.main()
