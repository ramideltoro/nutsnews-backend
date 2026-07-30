from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_production_readiness as readiness


class WorkerUpliftProductionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.decision = readiness.load_json(readiness.DEFAULT_DECISION_PATH)
        self.binding_proof = readiness.load_json(readiness.DEFAULT_BINDING_PATH)

    def validate(self, decision=None, binding_proof=None):
        return readiness.validate_decision(
            decision or self.decision,
            binding_proof or self.binding_proof,
        )

    def test_committed_no_go_passes(self):
        self.assertEqual(self.validate(), [])

    def test_go_or_closure_is_rejected_with_open_blockers(self):
        decision = copy.deepcopy(self.decision)
        decision["decision"] = "go"
        decision["issue_closure_authorized"] = True

        errors = self.validate(decision=decision)

        self.assertTrue(any("must remain NO-GO" in error for error in errors))
        self.assertTrue(any("must not authorize issue closure" in error for error in errors))

    def test_production_write_or_owner_change_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["safety_invariants"]["production_writes_enabled"] = True
        decision["safety_invariants"]["ingestion_ownership_changed"] = True

        errors = self.validate(decision=decision)

        self.assertIn("safety_invariants.production_writes_enabled must be false", errors)
        self.assertIn("safety_invariants.ingestion_ownership_changed must be false", errors)

    def test_fabricated_approver_or_waiver_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["named_approver"] = "invented-owner"
        decision["risk_waivers"] = [{"finding": "SEC-124-002"}]

        errors = self.validate(decision=decision)

        self.assertTrue(any("must not fabricate a named approver" in error for error in errors))
        self.assertTrue(any("must not fabricate risk waivers" in error for error in errors))

    def test_failover_analytics_cannot_be_inferred_from_documentation(self):
        proof = copy.deepcopy(self.binding_proof)
        proof["required_binding"]["present"] = True

        errors = self.validate(binding_proof=proof)

        self.assertTrue(any("must be recorded absent" in error for error in errors))

    def test_cloudflare_value_bearing_field_is_rejected(self):
        proof = copy.deepcopy(self.binding_proof)
        proof["bindings"][0]["value"] = "not-a-real-value"

        errors = self.validate(binding_proof=proof)

        self.assertTrue(any("forbidden value-bearing keys" in error for error in errors))

    def test_missing_blocker_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["blockers"] = [
            item
            for item in decision["blockers"]
            if item["id"] != "failover_analytics_binding"
        ]

        errors = self.validate(decision=decision)

        self.assertTrue(any("blocker scope mismatch" in error for error in errors))

    def test_protected_approval_bypass_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["runtime_and_recovery_evidence"]["fresh_status_dispatch"][
            "approval_bypassed"
        ] = True

        errors = self.validate(decision=decision)

        self.assertTrue(any("must not be bypassed" in error for error in errors))

    def test_source_hash_drift_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["source_control_hashes"][0]["sha256"] = "0" * 64

        errors = self.validate(decision=decision)

        self.assertTrue(any("source-control hash is stale" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
