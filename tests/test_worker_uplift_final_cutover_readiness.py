from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_final_cutover_readiness as readiness


class WorkerUpliftFinalCutoverReadinessTests(unittest.TestCase):
    def setUp(self):
        self.authorization = readiness.load_json(readiness.AUTHORIZATION_PATH)
        self.decision = readiness.load_json(readiness.DECISION_PATH)

    def test_committed_no_go_is_valid_and_fails_closed(self):
        self.assertEqual(
            readiness.validate_decision(self.decision, self.authorization),
            [],
        )
        errors = readiness.validate_decision(
            self.decision,
            self.authorization,
            require_go=True,
        )
        self.assertIn("exact #166 GO is absent", errors)

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
            require_go=True,
        )

        self.assertIn(
            "decision is not bound to the exact standing authorization",
            errors,
        )

    def test_no_go_cannot_freeze_partial_execution_inputs(self):
        decision = copy.deepcopy(self.decision)
        decision["candidate_sha256"] = "a" * 64

        errors = readiness.validate_decision(decision, self.authorization)

        self.assertIn("NO-GO must not freeze candidate_sha256", errors)

    def test_value_bearing_keys_are_rejected_without_echoing_values(self):
        contract = copy.deepcopy(self.authorization)
        contract["secret_value"] = "not-a-real-secret"

        errors = readiness.validate_authorization(contract)

        self.assertTrue(any("forbidden value-bearing key" in error for error in errors))
        self.assertNotIn("not-a-real-secret", "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
