#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_database_provider_switch_plan
from scripts import validate_backend_database_provider_switch


class BackendDatabaseProviderSwitchTests(unittest.TestCase):
    def test_contract_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_database_provider_switch.main(), 0)

    def test_safe_default_plan_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_database_provider_switch_plan.main_args(
                    ["--mode", "supabase_primary", "--output", str(output)]
                )
            plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(plan["mutation_performed"])
        self.assertEqual(plan["writer"], "supabase")

    def test_production_primary_without_confirmation_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_database_provider_switch_plan.main_args(
                    ["--mode", "backend_postgres_primary", "--environment", "production", "--output", str(output)]
                )
            plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("missing_backend_postgres_primary_confirmation", plan["blockers"])
        self.assertIn("production_switch_requires_protected_cutover_workflow", plan["blockers"])


if __name__ == "__main__":
    unittest.main()
