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
        self.assertIn("missing_supabase_standby_promotion_decision", plan["blockers"])

    def test_production_supabase_switch_without_go_decision_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = backend_database_provider_switch_plan.main_args(
                    ["--mode", "supabase_primary", "--environment", "production", "--output", str(output)]
                )
            plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertTrue(plan["promotion_decision_required"])
        self.assertIn("missing_supabase_standby_promotion_decision", plan["blockers"])

    def test_production_switch_with_expired_go_decision_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            decision = {
                "decision": "GO",
                "status": "GO",
                "decision_id": "sha256:" + "1" * 24,
                "failover_attempt_id": "failover-20260727T050000Z",
                "candidate_application_revision": "a" * 40,
                "fence_epoch": "epoch-20260727T050000Z",
                "expires_at_utc": "2026-07-27T05:00:00Z",
                "target_is_existing_production_supabase": True,
                "create_new_supabase_project": False,
                "create_nutsnews_standby_database": False,
                "app_worker_writes_to_supabase_before_failover": False,
                "single_use": True,
                "consumed": False,
                "safe_metadata_only": True,
            }
            decision_path = tmp / "decision.json"
            output = tmp / "plan.json"
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            with redirect_stdout(StringIO()):
                exit_code = backend_database_provider_switch_plan.main_args(
                    [
                        "--mode",
                        "supabase_primary",
                        "--environment",
                        "production",
                        "--promotion-decision",
                        str(decision_path),
                        "--failover-attempt-id",
                        "failover-20260727T050000Z",
                        "--candidate-application-revision",
                        "a" * 40,
                        "--fence-epoch",
                        "epoch-20260727T050000Z",
                        "--now-utc",
                        "2026-07-27T05:01:00Z",
                        "--output",
                        str(output),
                    ]
                )
            plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertIn("supabase_standby_promotion_decision_expired", plan["blockers"])


if __name__ == "__main__":
    unittest.main()
