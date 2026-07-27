from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import validate_backend_supabase_cleanup_retention_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs/backend-supabase-cleanup-retention-policy.json"
LOGICAL_PLAN = ROOT / "docs/backend-postgres-logical-replication-plan.json"
PROVIDER_SWITCH = ROOT / "docs/backend-database-provider-switch.json"
SOURCE_SCRIPT = ROOT / "scripts/backend_postgres_logical_replication_source.py"


class BackendSupabaseCleanupRetentionPolicyTests(unittest.TestCase):
    def test_policy_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_supabase_cleanup_retention_policy.main(), 0)

    def test_policy_forbids_blind_supabase_retirement(self):
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        production = policy["production_database_policy"]

        self.assertTrue(production["supabase_is_retained_as_hot_standby"])
        self.assertTrue(production["supabase_is_not_blindly_retired"])
        self.assertFalse(production["create_new_supabase_project"])
        self.assertFalse(production["create_nutsnews_standby_database"])
        self.assertIn("delete or blank NUTSNEWS_STANDBY_SUPABASE_* secrets", policy["forbidden_without_new_owner_approval"])
        self.assertIn("disable or uninstall nutsnews-supabase-sync-relay.service", policy["forbidden_without_new_owner_approval"])

    def test_logical_replication_cleanup_is_migration_only(self):
        plan = json.loads(LOGICAL_PLAN.read_text(encoding="utf-8"))
        teardown = plan["post_cutover_teardown"]

        self.assertEqual(plan["cleanup_retention_policy"], "docs/backend-supabase-cleanup-retention-policy.json")
        self.assertEqual(teardown["cleanup_policy_issue"], "ramideltoro/nutsnews#506")
        self.assertEqual(teardown["allowed_teardown_scope"], "obsolete_supabase_to_backend_migration_logical_replication_only")
        self.assertEqual(teardown["allowed_resource_prefix"], "nutsnews_backend_migration_")
        self.assertIn("backend-to-Supabase standby sync relay service, timer, environment file, contract, and reports", teardown["must_preserve"])
        self.assertNotIn("Supabase archive or retirement decision", json.dumps(plan))

    def test_provider_switch_retains_standby_after_cutover(self):
        provider = json.loads(PROVIDER_SWITCH.read_text(encoding="utf-8"))
        post_cutover = provider["post_cutover_status"]

        self.assertEqual(provider["status"], "production_primary_cutover_complete_supabase_hot_standby_retained")
        self.assertEqual(post_cutover["standby_retention_issue"], "ramideltoro/nutsnews#506")
        self.assertIn("existing production Supabase project and database", post_cutover["retained_standby"])
        self.assertIn("backend-to-Supabase standby sync relay resources", post_cutover["retained_standby"])
        self.assertNotIn("retirement_pending", post_cutover)

    def test_source_teardown_report_preserves_standby_resources(self):
        env = os.environ.copy()
        env.pop("NUTSNEWS_SOURCE_DB_URL", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(SOURCE_SCRIPT),
                "--operation",
                "teardown-dry-run",
                "--environment-name",
                "production",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_source_db_url", report["blockers"])
        self.assertEqual(report["cleanup_scope"], "obsolete_supabase_to_backend_migration_logical_replication_only")
        self.assertEqual(report["allowed_cleanup_resource_prefix"], "nutsnews_backend_migration_")
        self.assertIn("existing_production_supabase_standby", report["preserved_hot_standby_resources"])
        self.assertIn("backend_to_supabase_sync_relay_service_timer_env_contract_reports", report["preserved_hot_standby_resources"])
        self.assertTrue(report["safe_metadata_only"])


if __name__ == "__main__":
    unittest.main()
