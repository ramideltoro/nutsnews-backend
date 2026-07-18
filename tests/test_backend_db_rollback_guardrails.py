#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_db_rollback_guardrails_plan
from scripts import validate_backend_db_rollback_guardrails


class BackendDbRollbackGuardrailsTests(unittest.TestCase):
    def test_guardrail_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_db_rollback_guardrails.main(), 0)

    def test_supabase_primary_plan_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "guardrails.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_db_rollback_guardrails_plan.main_args(
                    ["--phase", "supabase_primary", "--output", str(output)]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(report["mutation_performed"])
        self.assertEqual(report["authoritative_writer"], "supabase")

    def test_final_catch_up_requires_live_pause_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "guardrails.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_db_rollback_guardrails_plan.main_args(
                    ["--phase", "final_catch_up", "--output", str(output)]
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("requires_live_writer_pause_evidence", report["blockers"])


if __name__ == "__main__":
    unittest.main()
