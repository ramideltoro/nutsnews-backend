#!/usr/bin/env python3
"""Tests for value-free worker-uplift AI credential readiness."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_backend_credential_readiness.py"
FIXTURE_SECRET = "synthetic-qwen-readiness-value"


def run_readiness(*, source_secret: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LOCAL_AI_API_KEY", None)
    if source_secret is not None:
        env["LOCAL_AI_API_KEY"] = source_secret
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--group",
            "worker_uplift_ai",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


class BackendCredentialReadinessTests(unittest.TestCase):
    def test_worker_uplift_ai_group_is_ready_without_printing_value(self) -> None:
        result = run_readiness(source_secret=FIXTURE_SECRET)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checked_names"], ["LOCAL_AI_API_KEY"])
        self.assertNotIn(FIXTURE_SECRET, result.stdout)
        self.assertNotIn(FIXTURE_SECRET, result.stderr)

    def test_worker_uplift_ai_group_fails_closed_when_source_is_absent(self) -> None:
        result = run_readiness(source_secret=None)
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["missing_required"], ["LOCAL_AI_API_KEY"])


if __name__ == "__main__":
    unittest.main()
