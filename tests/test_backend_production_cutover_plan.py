#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_production_cutover_plan
from scripts import validate_backend_production_cutover_plan


class BackendProductionCutoverPlanTests(unittest.TestCase):
    def test_cutover_plan_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_production_cutover_plan.main(), 0)

    def test_dry_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_production_cutover_plan.main_args(
                    ["--operation", "dry-run", "--output", str(output)]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(report["mutation_performed"])
        self.assertEqual(report["status"], "dry_run_ready")

    def test_mutating_operation_is_blocked_without_staging_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_production_cutover_plan.main_args(
                    [
                        "--operation",
                        "switch-provider",
                        "--confirmation",
                        "execute-production-db-cutover",
                        "--output",
                        str(output),
                    ]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("missing_staging_rehearsal_evidence", report["blockers"])
        self.assertIn("mutation_paths_blocked_in_current_scaffold", report["blockers"])


if __name__ == "__main__":
    unittest.main()
