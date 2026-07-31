from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_runtime_identities as identities


class WorkerUpliftRuntimeIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = identities.load_json(identities.IDENTITIES_PATH)
        cls.readiness = identities.load_json(identities.READINESS_PATH)
        cls.runtime_evidence = identities.load_json(identities.RUNTIME_EVIDENCE_PATH)
        cls.topology = identities.load_topology()
        cls.runtime_defaults = identities.load_runtime_defaults()
        cls.workflow_text = identities.PROTECTED_APPLY_PATH.read_text(encoding="utf-8")
        cls.compose_text = identities.RUNTIME_COMPOSE_PATH.read_text(encoding="utf-8")
        cls.security_review = identities.load_json(identities.SECURITY_REVIEW_PATH)

    def validate(self, inventory=None, workflow_text=None):
        return identities.validate_inventory(
            inventory if inventory is not None else copy.deepcopy(self.inventory),
            copy.deepcopy(self.readiness),
            copy.deepcopy(self.runtime_evidence),
            copy.deepcopy(self.topology),
            copy.deepcopy(self.runtime_defaults),
            workflow_text if workflow_text is not None else self.workflow_text,
            self.compose_text,
            copy.deepcopy(self.security_review),
        )

    def test_committed_inventory_passes(self):
        self.assertEqual(self.validate(), [])

    def test_missing_topology_identity_fails_exact_once_check(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["rabbitmq"]["topology_identities"].pop()

        errors = self.validate(inventory)

        self.assertTrue(any("topology identity set mismatch" in error for error in errors))

    def test_stale_main_queue_fails_source_and_deployed_checks(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["rabbitmq"]["service_bindings"][1]["main_queue"] = (
            "worker.uplift.feeds.fetch"
        )

        errors = self.validate(inventory)

        self.assertTrue(any("service main queue mismatch" in error for error in errors))
        self.assertTrue(any("runtime-default main queue mismatch" in error for error in errors))

    def test_wildcard_runtime_identity_is_rejected(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["rabbitmq"]["topology_identities"][3]["permissions"]["write"] = ".*"

        errors = self.validate(inventory)

        self.assertTrue(any("wildcard permission" in error for error in errors))

    def test_retained_publisher_injection_is_rejected(self):
        workflow = self.workflow_text.replace(
            'rabbitmq_url("RABBITMQ_FETCHER_CONSUMER_USERNAME", '
            '"RABBITMQ_FETCHER_CONSUMER_PASSWORD")',
            'rabbitmq_url("RABBITMQ_FETCHER_PUBLISHER_USERNAME", '
            '"RABBITMQ_FETCHER_PUBLISHER_PASSWORD")',
            1,
        )

        errors = self.validate(workflow_text=workflow)

        self.assertTrue(any("retained producer is unexpectedly injected" in error for error in errors))

    def test_shared_api_identity_cannot_be_reported_as_dedicated(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["api_identities"][0]["dedicated"] = True

        errors = self.validate(inventory)

        self.assertTrue(any("API identity mismatch: scheduler" in error for error in errors))

    def test_credential_value_field_is_rejected(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["credential_reference_dispositions"][0]["password_value"] = "redacted"

        errors = self.validate(inventory)

        self.assertTrue(any("credential value fields" in error for error in errors))

    def test_deployed_consumer_evidence_must_match_binding(self):
        evidence = copy.deepcopy(self.runtime_evidence)
        evidence["approval_free_status"]["queues"][0]["queue"] = "stale.queue"

        errors = identities.validate_inventory(
            copy.deepcopy(self.inventory),
            copy.deepcopy(self.readiness),
            evidence,
            copy.deepcopy(self.topology),
            copy.deepcopy(self.runtime_defaults),
            self.workflow_text,
            self.compose_text,
            copy.deepcopy(self.security_review),
        )

        self.assertTrue(any("deployed value-free consumer evidence" in error for error in errors))

    def test_container_accounts_cover_every_service(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["host_and_runtime_accounts"]["container_accounts"].pop()

        errors = self.validate(inventory)

        self.assertTrue(any("container account inventory" in error for error in errors))

    def test_projection_writer_scope_expansion_is_rejected(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["postgres"]["projection_writer"]["privileges"].append("DELETE")

        errors = self.validate(inventory)

        self.assertTrue(any("projection writer identity" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
