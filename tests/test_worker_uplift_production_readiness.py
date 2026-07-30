from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_production_readiness as readiness


class WorkerUpliftProductionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.decision = readiness.load_json(readiness.DEFAULT_DECISION_PATH)
        self.binding_proof = readiness.load_json(readiness.DEFAULT_BINDING_PATH)
        self.runtime_status_proof = readiness.load_json(
            readiness.DEFAULT_RUNTIME_STATUS_PATH
        )

    def validate(self, decision=None, binding_proof=None, runtime_status_proof=None):
        return readiness.validate_decision(
            decision or self.decision,
            binding_proof or self.binding_proof,
            runtime_status_proof or self.runtime_status_proof,
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

    def test_blocker_must_link_canonical_worker_tracking_issue(self):
        decision = copy.deepcopy(self.decision)
        decision["blockers"][0]["issue"] = "ramideltoro/nutsnews-infra#440"

        errors = self.validate(decision=decision)

        self.assertTrue(
            any("must link canonical tracking issue" in error for error in errors)
        )

    def test_downstream_control_implementation_cannot_block_issue_125(self):
        decision = copy.deepcopy(self.decision)
        decision["decision_authority"][
            "missing_downstream_control_implementation_blocks_this_gate"
        ] = True
        decision["runtime_and_recovery_evidence"]["rollback_limits"][
            "downstream_control_implementation_blocks_issue_125"
        ] = True

        errors = self.validate(decision=decision)

        self.assertIn("missing #150/#126 implementation must not block #125", errors)
        self.assertIn("downstream control implementation must not block #125", errors)

    def test_corrected_dependency_order_is_required(self):
        decision = copy.deepcopy(self.decision)
        decision["dependency_graph"]["ordered_control_and_execution_gates"][1][
            "depends_on"
        ] = ["ramideltoro/nutsnews-worker#125"]

        errors = self.validate(decision=decision)

        self.assertTrue(
            any("#125 -> #150 -> #126 -> #166 -> #127" in error for error in errors)
        )

    def test_protected_approval_bypass_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["runtime_and_recovery_evidence"]["fresh_status_dispatch"][
            "approval_bypassed"
        ] = True

        errors = self.validate(decision=decision)

        self.assertTrue(any("must not be bypassed" in error for error in errors))

    def test_completed_protected_status_requires_immutable_artifact(self):
        decision = copy.deepcopy(self.decision)
        fresh_status = decision["runtime_and_recovery_evidence"][
            "fresh_status_dispatch"
        ]
        fresh_status.pop("artifact_id")
        fresh_status.pop("artifact_digest")

        errors = self.validate(decision=decision)

        self.assertTrue(any("must record artifact 8770464087" in error for error in errors))
        self.assertTrue(
            any("immutable artifact digest" in error for error in errors)
        )

    def test_source_hash_drift_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["source_control_hashes"][0]["sha256"] = "0" * 64

        errors = self.validate(decision=decision)

        self.assertTrue(any("source-control hash is stale" in error for error in errors))

    def test_approval_free_status_cannot_enter_protected_path(self):
        decision = copy.deepcopy(self.decision)
        approval_free = decision["runtime_and_recovery_evidence"][
            "approval_free_status_dispatch"
        ]
        approval_free["pending_deployment_count_observed"] = 1
        approval_free["protected_job_status"] = "success"

        errors = self.validate(decision=decision)

        self.assertTrue(any("zero pending deployments" in error for error in errors))
        self.assertTrue(any("must skip the protected job" in error for error in errors))

    def test_scheduler_timestamp_defect_cannot_be_normalized(self):
        proof = copy.deepcopy(self.runtime_status_proof)
        proof["scheduler_readiness_discrepancy"]["readiness_checked_at_utc"] = (
            proof["protected_status"]["generated_at_utc"]
        )
        proof["scheduler_readiness_discrepancy"]["silently_normalized"] = True

        errors = self.validate(runtime_status_proof=proof)

        self.assertTrue(any("preserve the observed checkedAt" in error for error in errors))
        self.assertTrue(any("must not be silently normalized" in error for error in errors))

    def test_scheduler_defect_must_link_independent_blocker(self):
        decision = copy.deepcopy(self.decision)
        decision["runtime_and_recovery_evidence"]["scheduler_readiness"][
            "blocker_issue"
        ] = "ramideltoro/nutsnews-worker#125"

        errors = self.validate(decision=decision)

        self.assertTrue(any("must link blocker #168" in error for error in errors))

    def test_runtime_tracking_issue_85_must_remain_historical_provenance(self):
        proof = copy.deepcopy(self.runtime_status_proof)
        tracker = proof["report_tracking_issue_discrepancy"]
        tracker["reported_number"] = 125
        tracker["new_blocker_required"] = True

        errors = self.validate(runtime_status_proof=proof)

        self.assertTrue(any("must preserve 85" in error for error in errors))
        self.assertTrue(any("must not fabricate a blocker" in error for error in errors))

    def test_runtime_evidence_value_bearing_field_is_rejected(self):
        proof = copy.deepcopy(self.runtime_status_proof)
        proof["protected_status"]["value"] = "not-a-real-secret"

        errors = self.validate(runtime_status_proof=proof)

        self.assertTrue(any("forbidden value-bearing keys" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
