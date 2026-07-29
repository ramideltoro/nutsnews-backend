from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import validate_backend_github_actions_security as actions_security
from scripts import validate_worker_uplift_security_review as security_review


class WorkerUpliftSecurityReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = security_review.load_review(security_review.DEFAULT_REVIEW_PATH)

    def test_committed_review_passes(self):
        self.assertEqual(security_review.validate_review(self.review), [])

    def test_unresolved_high_finding_fails(self):
        review = copy.deepcopy(self.review)
        review["findings"].append(
            {
                "id": "SEC-124-TEST",
                "severity": "high",
                "status": "accepted_residual_risk",
            }
        )

        errors = security_review.validate_review(review)

        self.assertTrue(any("critical/high finding" in error for error in errors))

    def test_production_write_or_legacy_change_fails(self):
        review = copy.deepcopy(self.review)
        review["safety_invariants"]["production_writes_enabled"] = True
        review["safety_invariants"]["legacy_worker_modified"] = True

        errors = security_review.validate_review(review)

        self.assertIn("safety_invariants.production_writes_enabled must be false", errors)
        self.assertIn("safety_invariants.legacy_worker_modified must be false", errors)

    def test_incomplete_residual_risk_acceptance_fails(self):
        review = copy.deepcopy(self.review)
        review["findings"][1]["risk_acceptance"].pop("required_follow_up")

        errors = security_review.validate_review(review)

        self.assertTrue(any("must record required_follow_up" in error for error in errors))

    def test_unpinned_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_dir = Path(tmpdir)
            (workflow_dir / "test.yml").write_text(
                "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )

            errors = actions_security.validate_workflows(workflow_dir)

        self.assertTrue(any("immutable 40-character commit SHA" in error for error in errors))

    def test_direct_dispatch_input_in_shell_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_dir = Path(tmpdir)
            (workflow_dir / "test.yml").write_text(
                "jobs:\n"
                "  test:\n"
                "    steps:\n"
                "      - run: |\n"
                "          printf '%s\\n' \"${{ inputs.confirmation }}\"\n",
                encoding="utf-8",
            )

            errors = actions_security.validate_workflows(workflow_dir)

        self.assertTrue(any("must enter shell through step env" in error for error in errors))

    def test_review_artifact_does_not_contain_connection_strings(self):
        text = json.dumps(self.review).lower()
        for forbidden in (
            "postgres://",
            "postgresql://",
            "amqp://",
            "amqps://",
            "authorization: bearer ",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIsNone(
            re.search(r'"(?:password|token|secret_value|private_key)"\s*:', text)
        )


if __name__ == "__main__":
    unittest.main()
