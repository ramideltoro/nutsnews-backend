from __future__ import annotations

import copy
import unittest

from scripts import validate_worker_uplift_security_remediation_builds as builds


class WorkerUpliftSecurityRemediationBuildTests(unittest.TestCase):
    def setUp(self):
        self.document = builds.load_json()

    def test_committed_evidence_matches_runtime_candidate(self):
        self.assertEqual(builds.validate(self.document), [])

    def test_missing_service_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["service_images"] = document["service_images"][:-1]

        self.assertTrue(any("all eight stages" in error for error in builds.validate(document)))

    def test_mutable_or_missing_image_proof_is_rejected(self):
        document = copy.deepcopy(self.document)
        image = document["service_images"][0]
        image["image"] = image["image"].split("@", maxsplit=1)[0] + ":latest"
        image["sbom_digest"] = "missing"
        image["signed"] = False

        errors = builds.validate(document)

        self.assertTrue(any("image must be a SHA-256 digest" in error for error in errors))
        self.assertTrue(any("sbom_digest must be a SHA-256 digest" in error for error in errors))
        self.assertTrue(any("image must be signed" in error for error in errors))

    def test_runtime_candidate_drift_is_rejected(self):
        defaults = builds.DEFAULT_RUNTIME_DEFAULTS_PATH.read_text(encoding="utf-8")
        defaults = defaults.replace(self.document["service_images"][0]["source_commit"], "0" * 40)

        self.assertTrue(any("source commit" in error for error in builds.validate(self.document, defaults)))

    def test_fetcher_dns_binding_proof_is_required(self):
        document = copy.deepcopy(self.document)
        fetcher = next(item for item in document["service_images"] if item["stage"] == "fetcher")
        fetcher["dns_resolution_bound_to_connect"] = False
        fetcher["dns_binding_fail_closed_tested"] = False

        errors = builds.validate(document)

        self.assertIn("fetcher must prove DNS resolution-to-connect binding", errors)
        self.assertIn("fetcher must prove fail-closed DNS binding tests", errors)

    def test_production_writes_or_cutover_cannot_be_claimed(self):
        document = copy.deepcopy(self.document)
        document["safety"]["production_writes_enabled"] = True
        document["safety"]["cutover_authorized"] = True

        errors = builds.validate(document)

        self.assertIn("safety.production_writes_enabled must be false", errors)
        self.assertIn("safety.cutover_authorized must be false", errors)


if __name__ == "__main__":
    unittest.main()
