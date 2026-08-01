from __future__ import annotations

import copy
import json
import unittest
from datetime import date

from scripts import validate_worker_uplift_security_dispositions as dispositions


class WorkerUpliftSecurityDispositionTests(unittest.TestCase):
    def setUp(self):
        self.document = dispositions.load_json(dispositions.DEFAULT_DISPOSITIONS_PATH)
        self.today = date(2026, 7, 31)

    def validate(self, document=None, *, enforce_closure=False, backend_checks_text=None):
        return dispositions.validate_dispositions(
            document or self.document,
            today=self.today,
            enforce_closure=enforce_closure,
            backend_checks_text=backend_checks_text,
        )

    def sync_closure(self, document):
        pending = [item["id"] for item in document["findings"] if item["status"] == "pending"]
        accepted = [
            {
                "finding": item["id"],
                "owner": item["risk_acceptance"]["authorized_owner_login"],
            }
            for item in document["findings"]
            if item["status"] == "accepted_residual_risk"
        ]
        ready = not pending
        document["closure_gate"].update(
            {
                "ready": ready,
                "status": "pass" if ready else "blocked",
                "unresolved_findings": pending,
                "named_owner_decisions": accepted,
                "issue_closure_authorized": ready,
                "owner_action_required": bool(pending),
            }
        )
        document["decision"] = "complete" if ready else "blocked_pending_dispositions"

    def make_remediated(self, finding):
        finding["status"] = "remediated"
        finding["owner_action_required"] = False
        finding["risk_acceptance"] = None
        finding["remediation"] = {
            "summary": "Removed the affected condition through a separately reviewed change.",
            "verification": "Focused and repository validation passed.",
            "rollback": "Revert the immutable merge through a new pull request.",
            "deployment_required": False,
            "immutable_evidence": [
                {
                    "kind": "pull_request_merge",
                    "url": "https://github.com/ramideltoro/example/pull/1",
                    "head_commit": "1" * 40,
                },
                {
                    "kind": "workflow_run",
                    "url": "https://github.com/ramideltoro/example/actions/runs/1",
                    "head_commit": "1" * 40,
                    "run_id": 1,
                    "conclusion": "success",
                },
            ],
        }

    def make_accepted(self, finding):
        finding["status"] = "accepted_residual_risk"
        finding["owner_action_required"] = False
        finding["remediation"] = None
        finding["risk_acceptance"] = {
            "authorized_owner_login": "ramideltoro",
            "decided_at_utc": "2026-07-31T14:00:00Z",
            "scope": "Exact affected repositories while uplift stays shadow-only.",
            "rationale": "The bounded residual remains proportionate for the recorded interval.",
            "compensating_controls": ["production writes remain disabled"],
            "review_on": "2026-11-30",
            "expires_on": "2026-12-31",
            "reopen_trigger": "Any recorded scope or compensating control changes.",
            "acceptance_evidence": {
                "kind": "issue_comment",
                "url": "https://github.com/ramideltoro/nutsnews-worker/issues/164#issuecomment-123456789",
                "author_login": "ramideltoro",
                "authored_at_utc": "2026-07-31T14:00:00Z",
            },
        }

    def test_committed_complete_record_is_structurally_valid(self):
        self.assertEqual(self.validate(), [])

    def test_committed_complete_record_passes_closure(self):
        self.assertEqual(self.validate(enforce_closure=True), [])

    def test_closure_mode_rejects_a_pending_finding(self):
        document = copy.deepcopy(self.document)
        finding = next(item for item in document["findings"] if item["id"] == "SEC-124-007")
        finding["status"] = "pending"
        finding["owner_action_required"] = True
        finding["risk_acceptance"] = None
        self.sync_closure(document)
        document["standing_authorization"]["scope_sha256"] = (
            dispositions.standing_scope_sha256(document)
        )

        errors = self.validate(document, enforce_closure=True)

        self.assertTrue(any("closure enforcement failed" in error for error in errors))

    def test_missing_finding_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["findings"] = document["findings"][:-1]
        document["closure_gate"]["unresolved_findings"] = document["closure_gate"][
            "unresolved_findings"
        ][:-1]

        errors = self.validate(document)

        self.assertTrue(any("finding scope mismatch" in error for error in errors))

    def test_generic_or_anonymous_owner_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["accountable_owner"]["login"] = "owner"

        errors = self.validate(document)

        self.assertTrue(any("anonymous, generic, or automation" in error for error in errors))

    def test_expired_residual_acceptance_is_rejected(self):
        document = copy.deepcopy(self.document)
        self.make_accepted(document["findings"][0])
        document["findings"][0]["risk_acceptance"]["expires_on"] = "2026-07-30"
        document["findings"][0]["risk_acceptance"]["review_on"] = "2026-07-30"
        self.sync_closure(document)

        errors = self.validate(document)

        self.assertTrue(any("risk acceptance is expired" in error for error in errors))
        self.assertTrue(any("review date is expired" in error for error in errors))

    def test_issue_closure_or_automation_cannot_supply_acceptance(self):
        document = copy.deepcopy(self.document)
        self.make_accepted(document["findings"][0])
        evidence = document["findings"][0]["risk_acceptance"]["acceptance_evidence"]
        evidence["url"] = "https://github.com/ramideltoro/nutsnews-worker/issues/164"
        evidence["author_login"] = "github-actions[bot]"
        self.sync_closure(document)

        errors = self.validate(document)

        self.assertTrue(any("explicit #164 issue comment" in error for error in errors))
        self.assertTrue(any("anonymous, generic, or automation" in error for error in errors))

    def test_incomplete_remediation_evidence_is_rejected(self):
        document = copy.deepcopy(self.document)
        self.make_remediated(document["findings"][0])
        document["findings"][0]["remediation"]["immutable_evidence"] = []
        self.sync_closure(document)

        errors = self.validate(document)

        self.assertTrue(any("must record immutable evidence" in error for error in errors))

    def test_runtime_remediation_requires_deployment_proof(self):
        document = copy.deepcopy(self.document)
        self.make_remediated(document["findings"][2])
        document["findings"][2]["remediation"]["deployment_required"] = True
        self.sync_closure(document)

        errors = self.validate(document)

        self.assertTrue(any("requires deployment proof" in error for error in errors))

    def test_all_remediated_fixture_still_has_no_pending_dispositions(self):
        document = copy.deepcopy(self.document)
        for finding in document["findings"]:
            self.make_remediated(finding)
        self.sync_closure(document)

        self.assertEqual(document["closure_gate"]["unresolved_findings"], [])
        self.assertTrue(document["closure_gate"]["issue_closure_authorized"])

    def test_standing_authorization_survives_release_revision_only(self):
        document = copy.deepcopy(self.document)
        finding = next(item for item in document["findings"] if item["id"] == "SEC-124-007")
        finding["current_evidence"]["source_heads"][0]["commit"] = "f" * 40

        self.assertEqual(self.validate(document, enforce_closure=True), [])

    def test_scope_drift_is_rejected_even_if_fingerprint_is_recomputed(self):
        document = copy.deepcopy(self.document)
        finding = next(item for item in document["findings"] if item["id"] == "SEC-124-007")
        finding["risk_acceptance"]["compensating_controls"].append(
            "a newly claimed control"
        )
        document["standing_authorization"]["scope_sha256"] = (
            dispositions.standing_scope_sha256(document)
        )

        errors = self.validate(document, enforce_closure=True)

        self.assertTrue(any("not owner-authorized" in error for error in errors))

    def test_standing_authorization_removes_per_release_and_first_run_approval(self):
        authorization = self.document["standing_authorization"]

        self.assertFalse(authorization["per_release_owner_approval_required"])
        self.assertFalse(authorization["first_run_owner_approval_required"])
        self.assertFalse(authorization["review_refresh_requires_new_owner_approval"])
        self.assertEqual(self.validate(enforce_closure=True), [])

    def test_standing_authorization_cannot_cover_readiness_or_cutover(self):
        document = copy.deepcopy(self.document)
        document["disposition_policy"][
            "final_readiness_or_cutover_approval_is_covered"
        ] = True

        errors = self.validate(document)

        self.assertTrue(any("must be false" in error for error in errors))

    def test_standing_authorization_still_expires_fail_closed(self):
        errors = dispositions.validate_dispositions(
            self.document,
            today=date(2026, 10, 1),
            enforce_closure=True,
        )

        self.assertTrue(any("risk acceptance is expired" in error for error in errors))

    def test_standing_owner_evidence_cannot_be_replaced_by_automation(self):
        document = copy.deepcopy(self.document)
        document["standing_authorization"]["owner_evidence"]["author_login"] = (
            "github-actions[bot]"
        )

        errors = self.validate(document)

        self.assertTrue(any("anonymous, generic, or automation" in error for error in errors))

    def test_standing_owner_comment_digest_is_pinned(self):
        document = copy.deepcopy(self.document)
        document["standing_authorization"]["owner_evidence"]["body_sha256"] = "0" * 64

        errors = self.validate(document)

        self.assertTrue(any("comment digest is not authorized" in error for error in errors))

    def test_backend_checks_must_enforce_closure(self):
        workflow = dispositions.BACKEND_CHECKS_PATH.read_text(encoding="utf-8")
        workflow = workflow.replace(
            "run: python3 scripts/validate_worker_uplift_security_dispositions.py --enforce-closure",
            "run: python3 scripts/validate_worker_uplift_security_dispositions.py",
        )

        errors = self.validate(backend_checks_text=workflow)

        self.assertIn("Backend Checks must enforce security disposition closure", errors)

    def test_production_write_or_value_bearing_evidence_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["safety_invariants"]["production_writes_enabled"] = True
        document["findings"][0]["current_evidence"]["password"] = "not-a-real-secret"

        errors = self.validate(document)

        self.assertIn("safety_invariants.production_writes_enabled must be false", errors)
        self.assertTrue(any("forbidden value-bearing key" in error for error in errors))
        self.assertNotIn("not-a-real-secret", json.dumps(errors))


if __name__ == "__main__":
    unittest.main()
