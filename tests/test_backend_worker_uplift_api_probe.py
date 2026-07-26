from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backend_worker_uplift_api_probe.py"
WORKFLOW = ROOT / ".github" / "workflows" / "backend-worker-uplift-api-probe.yml"


def load_module():
    spec = importlib.util.spec_from_file_location("backend_worker_uplift_api_probe", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load backend worker-uplift API probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackendWorkerUpliftApiProbeTests(unittest.TestCase):
    def test_build_command_is_shadow_only_and_scoped(self):
        module = load_module()
        command = module.build_command("abc123", github_run_id="301", occurred_at="2026-07-26T00:00:00.000Z")

        self.assertEqual(command["operation"], "uplift-record-shadow-aggregate")
        self.assertEqual(command["providerMode"], "backend_postgres_shadow")
        self.assertEqual(command["actorService"], "worker-uplift-persistence")
        self.assertEqual(command["schemaVersion"], 1)
        self.assertEqual(command["expectedArticleVersion"], 1)
        aggregate = command["shadowAggregate"]
        self.assertEqual(aggregate["articleIdentityHash"], "worker-api-probe-abc123")
        self.assertEqual(aggregate["publicationStatus"], "ready")
        self.assertEqual(aggregate["translationLanguages"], ["fr", "ja", "de-CH", "de", "el"])
        self.assertTrue(aggregate["payloadDigest"].startswith("sha256:"))
        self.assertTrue(aggregate["diagnosticMetadata"]["safeMetadataOnly"])
        self.assertNotIn("token", json.dumps(command).lower())

    def test_offline_mode_outputs_safe_skipped_report(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--offline", "--enforce", "--probe-id", "offline"],
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(report["safe_metadata_only"])
        self.assertEqual(report["checks"][0]["status"], "skipped_with_reason")

    def test_live_mode_missing_token_fails_when_enforced(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--enforce",
                "--probe-id",
                "missing-token",
                "--token-env",
                "NUTSNEWS_BACKEND_WORKER_UPLIFT_MISSING_TEST_TOKEN",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "fail")
        self.assertIn("missing_worker_uplift_persistence_token", report["errors"])

    def test_workflow_uses_protected_environment_and_scoped_secret(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("environment: production-backend", workflow)
        self.assertIn("NUTSNEWS_BACKEND_WORKER_UPLIFT_PERSISTENCE_TOKEN", workflow)
        self.assertIn("backend_worker_uplift_api_probe.py", workflow)
        self.assertIn("backend-worker-uplift-api-probe", workflow)


if __name__ == "__main__":
    unittest.main()
