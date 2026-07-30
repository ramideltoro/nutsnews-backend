from __future__ import annotations

import unittest

from scripts import validate_backend_worker_runtime_operations_workflow as validator


class BackendWorkerRuntimeOperationsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = validator.WORKFLOW_PATH.read_text(encoding="utf-8")

    def validate(self, workflow: str | None = None) -> list[str]:
        return validator.validate_workflow_text(
            self.workflow if workflow is None else workflow
        )

    def test_committed_workflow_passes(self):
        self.assertEqual(self.validate(), [])

    def test_read_only_job_cannot_reference_environment(self):
        workflow = self.workflow.replace(
            "  read-only-runtime:\n",
            "  read-only-runtime:\n    environment: production-backend\n",
            1,
        )
        self.assertIn(
            "read-only job must not reference a GitHub environment",
            self.validate(workflow),
        )

    def test_protected_job_requires_environment(self):
        workflow = self.workflow.replace(
            "    environment: production-backend\n",
            "",
            1,
        )
        self.assertIn(
            "protected job must reference production-backend exactly once",
            self.validate(workflow),
        )

    def test_mutating_action_cannot_enter_read_only_partition(self):
        workflow = self.workflow.replace(
            '["check","status","logs","queue-inspect","dlq-inspect"]',
            '["check","status","logs","queue-inspect","dlq-inspect","deploy"]',
            1,
        )
        errors = self.validate(workflow)
        self.assertIn(
            "read-only job condition must contain exactly the five read-only actions",
            errors,
        )
        self.assertIn(
            "read-only and protected action partitions must be disjoint",
            errors,
        )

    def test_protected_action_partition_must_be_complete(self):
        workflow = self.workflow.replace(
            '["deploy","promote","restart","scale","rollback","dlq-replay","drain","reconciliation","smoke"]',
            '["deploy","promote","restart","scale","rollback","dlq-replay","drain","reconciliation"]',
            1,
        )
        errors = self.validate(workflow)
        self.assertIn(
            "protected job condition must contain exactly the nine protected actions",
            errors,
        )
        self.assertIn(
            "job action partitions must cover every workflow action",
            errors,
        )

    def test_read_only_job_requires_repository_ssh_secret_names(self):
        workflow = self.workflow.replace(
            "secrets.NUTSNEWS_BACKEND_SSH_PRIVATE_KEY",
            "secrets.WRONG_KEY",
            1,
        )
        self.assertTrue(
            any(
                "read-only job must source repository secret "
                "NUTSNEWS_BACKEND_SSH_PRIVATE_KEY" in error
                for error in self.validate(workflow)
            )
        )

    def test_protected_job_requires_typed_confirmation(self):
        workflow = self.workflow.replace(
            '[[ "$CONFIRM_TARGET" != "backend.nutsnews.com" ]]',
            '[[ -z "$CONFIRM_TARGET" ]]',
            2,
        )
        self.assertIn(
            "protected job must validate the exact confirmation target",
            self.validate(workflow),
        )

    def test_read_only_job_cannot_pass_confirmation_flag(self):
        workflow = self.workflow.replace(
            "          remote_args+=(--dry-run)\n",
            "          remote_args+=(--dry-run --confirm-action)\n",
            1,
        )
        self.assertIn(
            "read-only job must never pass --confirm-action",
            self.validate(workflow),
        )


if __name__ == "__main__":
    unittest.main()
