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

    def test_committed_go_passes(self):
        self.assertEqual(self.validate(), [])

    def test_no_go_or_open_issue_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["decision"] = "no_go"
        decision["tracking_issue_state_required"] = "open"

        errors = self.validate(decision=decision)

        self.assertIn("committed #125 decision must be GO", errors)
        self.assertIn("passing #125 GO must require the tracking issue closed", errors)

    def test_missing_or_wrong_approver_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["named_approver"] = {}

        errors = self.validate(decision=decision)

        self.assertTrue(any("named #125 approver" in error for error in errors))
        self.assertTrue(any("standing authorization" in error for error in errors))

    def test_approval_scope_expansion_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["named_approver"]["scope"] = "cutover"
        decision["named_approver"]["does_not_authorize"].remove(
            "final_cutover_execution_gate_approval"
        )

        errors = self.validate(decision=decision)

        self.assertTrue(any("#150 implementation only" in error for error in errors))
        self.assertTrue(any("exclude #166" in error for error in errors))

    def test_cutover_authority_expansion_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["decision_authority"]["go_authorizes_issue"] = (
            "ramideltoro/nutsnews-worker#127"
        )

        errors = self.validate(decision=decision)

        self.assertIn("#125 GO may authorize only the start of #150", errors)

    def test_downstream_dependency_order_drift_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["dependency_graph"]["ordered_control_and_execution_gates"][1][
            "depends_on"
        ] = ["ramideltoro/nutsnews-worker#125"]

        errors = self.validate(decision=decision)

        self.assertTrue(any("#125 -> #150 -> #126 -> #166 -> #127" in error for error in errors))

    def test_production_write_or_owner_change_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["safety_invariants"]["production_writes_enabled"] = True
        decision["safety_invariants"]["ingestion_ownership_changed"] = True

        errors = self.validate(decision=decision)

        self.assertIn("safety_invariants.production_writes_enabled must be false", errors)
        self.assertIn("safety_invariants.ingestion_ownership_changed must be false", errors)

    def test_risk_waiver_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["risk_waivers"] = [{"finding": "invented"}]

        errors = self.validate(decision=decision)

        self.assertIn("#125 GO must not introduce risk waivers", errors)

    def test_candidate_image_mismatch_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["comparison_and_soak_evidence"]["parity"][
            "candidate_image_mismatches"
        ] = ["scheduler"]

        errors = self.validate(decision=decision)

        self.assertTrue(any("no mismatch" in error for error in errors))

    def test_stale_parity_or_soak_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["comparison_and_soak_evidence"]["parity"]["status"] = "stale"
        decision["comparison_and_soak_evidence"]["soak"]["status"] = "stale"

        errors = self.validate(decision=decision)

        self.assertTrue(any("parity run 30684493608" in error for error in errors))
        self.assertTrue(any("soak run 30684679655" in error for error in errors))
        self.assertTrue(any("stale or blocked evidence" in error for error in errors))

    def test_incomplete_soak_window_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["comparison_and_soak_evidence"]["soak"]["observed_hours"] = 47.99

        errors = self.validate(decision=decision)

        self.assertTrue(any("at least 48 hours" in error for error in errors))

    def test_current_runtime_artifact_digest_is_required(self):
        decision = copy.deepcopy(self.decision)
        decision["runtime_and_recovery_evidence"]["current_candidate_status"][
            "artifact_digest"
        ] = "not-a-digest"

        errors = self.validate(decision=decision)

        self.assertTrue(any("artifact digest" in error for error in errors))

    def test_scheduler_test_adapters_are_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["runtime_and_recovery_evidence"]["scheduler_readiness"][
            "source_uses_local_test_dependencies"
        ] = True

        errors = self.validate(decision=decision)

        self.assertTrue(any("reject local test adapters" in error for error in errors))

    def test_unresolved_recovery_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        recovery = decision["runtime_and_recovery_evidence"]["empty_broker_recovery"]
        recovery["status"] = "stale"
        recovery["all_required_consumers_restored"] = False

        errors = self.validate(decision=decision)

        self.assertTrue(any("#159" in error for error in errors))
        self.assertTrue(any("all_required_consumers_restored" in error for error in errors))

    def test_missing_dependency_drill_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        evidence = decision["runtime_and_recovery_evidence"][
            "dependency_outage_and_backup_proof"
        ]
        evidence["missing"] = ["qwen"]
        evidence["qwen_detected_and_recovered"] = False

        errors = self.validate(decision=decision)

        self.assertTrue(any("complete and current-equivalent" in error for error in errors))
        self.assertTrue(any("qwen_detected_and_recovered" in error for error in errors))

    def test_downstream_controls_cannot_be_claimed_implemented(self):
        decision = copy.deepcopy(self.decision)
        rollback = decision["runtime_and_recovery_evidence"]["rollback_limits"]
        rollback["production_owner_rollback_workflow_exists"] = True
        rollback["cutover_watermark_exists"] = True

        errors = self.validate(decision=decision)

        self.assertTrue(any("must remain false before #126" in error for error in errors))

    def test_cloudflare_binding_absence_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["cloudflare_failover"]["failover_analytics_binding_present"] = False

        errors = self.validate(decision=decision)

        self.assertTrue(any("FAILOVER_ANALYTICS" in error for error in errors))

    def test_admin_mutating_or_missing_evidence_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        admin = decision["observability_and_admin_evidence"]["admin_portal"]
        admin["authorized_access_read_only"] = False
        admin["authenticated_live_projection_artifact_present"] = False

        errors = self.validate(decision=decision)

        self.assertTrue(any("authorized_access_read_only" in error for error in errors))
        self.assertTrue(any("authenticated_live_projection" in error for error in errors))

    def test_open_readiness_item_or_blocker_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["readiness_items"][0]["status"] = "block"
        decision["blockers"][0]["status"] = "open"

        errors = self.validate(decision=decision)

        self.assertTrue(any("readiness must pass" in error for error in errors))
        self.assertTrue(any("must be resolved" in error for error in errors))

    def test_source_hash_drift_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["source_control_hashes"][0]["sha256"] = "0" * 64

        errors = self.validate(decision=decision)

        self.assertTrue(any("source-control hash is stale" in error for error in errors))

    def test_value_bearing_field_is_rejected(self):
        decision = copy.deepcopy(self.decision)
        decision["named_approver"]["token"] = "not-a-real-secret"

        errors = self.validate(decision=decision)

        self.assertTrue(any("forbidden value-bearing keys" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
