from __future__ import annotations

import copy
import json
import unittest

from scripts import validate_worker_uplift_cutover_controls as validator


class WorkerUpliftCutoverValidatorTests(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads(validator.CONTRACT_PATH.read_text())

    def test_repository_controls_validate(self):
        self.assertEqual([], validator.validate_repository())

    def test_authorization_scope_change_fails_closed_even_with_new_embedded_digest(self):
        changed = copy.deepcopy(self.contract)
        changed["standing_authorization"]["authorized_operations"].append("apply")
        changed["standing_authorization"]["scope_sha256"] = validator.canonical_sha256(
            validator.scope_payload(changed)
        )
        errors = validator.validate_contract(changed)
        self.assertTrue(any("operation set changed" in error for error in errors))
        self.assertTrue(any("scope fingerprint changed" in error for error in errors))

    def test_final_execution_and_risk_acceptance_remain_excluded(self):
        exclusions = set(self.contract["standing_authorization"]["excluded_authorities"])
        self.assertIn("issue-166-go", exclusions)
        self.assertIn("issue-127-execution", exclusions)
        self.assertIn("risk-acceptance", exclusions)

    def test_safe_current_state_cannot_enable_uplift_writes(self):
        changed = copy.deepcopy(self.contract)
        changed["current_required_state"]["uplift_production_writes_enabled"] = True
        errors = validator.validate_contract(changed)
        self.assertTrue(any("safe shadow" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
