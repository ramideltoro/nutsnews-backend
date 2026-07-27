from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_staging_failover_drill as drill
from scripts import validate_backend_supabase_standby_staging_failover_drill


ATTEMPT = "failover-20260727T070000Z-issue503"
REVISION = "b" * 40
EPOCH = "epoch-20260727T070000Z-issue503"


def failover_plan(**extra) -> dict:
    plan = {
        "status": "blocked",
        "operation": "dry-run",
        "workflow_id": "backend-supabase-standby-failover",
        "failover_attempt_id": ATTEMPT,
        "candidate_application_revision": REVISION,
        "fence_epoch": EPOCH,
        "target_after_failover": "existing_production_supabase_standby",
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "mutation_performed": False,
        "provider_switch_performed_by_this_workflow": False,
        "safe_metadata_only": True,
        "blockers": ["missing_supabase_standby_promotion_decision"],
    }
    plan.update(extra)
    return plan


def apply_args() -> list[str]:
    return [
        "--operation",
        "staging-apply",
        "--confirmation",
        drill.STAGING_APPLY_CONFIRMATION,
        "--failover-attempt-id",
        ATTEMPT,
        "--candidate-application-revision",
        REVISION,
        "--fence-epoch",
        EPOCH,
    ]


def run_drill(args: list[str], plan: dict | None = None) -> tuple[int, dict, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        output = root / "drill.json"
        summary = root / "summary.md"
        final_args = [*args, "--output", str(output), "--summary", str(summary)]
        if plan is not None:
            plan_path = root / "failover-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            final_args.extend(["--failover-plan", str(plan_path)])
        with redirect_stdout(StringIO()):
            exit_code = drill.main_args(final_args)
        return exit_code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


class BackendSupabaseStandbyStagingFailoverDrillTests(unittest.TestCase):
    def test_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_supabase_standby_staging_failover_drill.main(), 0)

    def test_fixture_staging_apply_passes_with_safe_metadata(self):
        exit_code, report, summary = run_drill([*apply_args(), "--fixture-pass"], failover_plan())

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["staging_apply_performed"])
        self.assertEqual(report["provider_mode"], "supabase_primary")
        self.assertTrue(report["production_writes_paused"])
        self.assertEqual(report["backend_postgres_write_delta_after_failover"], 0)
        self.assertEqual(report["write_eligible_provider_count"], 1)
        self.assertEqual(report["eligible_provider"], "existing_production_supabase_standby")
        self.assertEqual(report["blockers"], [])
        self.assertIn("Status: `PASS`", summary)
        self.assertTrue(report["target_is_existing_production_supabase"])
        self.assertFalse(report["create_new_supabase_project"])
        self.assertFalse(report["create_nutsnews_standby_database"])
        self.assertFalse(report["app_worker_writes_to_supabase_before_approved_failover"])
        self.assertFalse(report["mutation_performed"])
        self.assertFalse(report["production_mutation_performed"])

    def test_dry_run_passes_without_staging_apply(self):
        exit_code, report, _ = run_drill(
            [
                "--operation",
                "dry-run",
                "--confirmation",
                drill.DRY_RUN_CONFIRMATION,
                "--failover-attempt-id",
                ATTEMPT,
                "--candidate-application-revision",
                REVISION,
                "--fence-epoch",
                EPOCH,
                "--fixture-pass",
            ],
            failover_plan(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "dry_run_ready")
        self.assertFalse(report["staging_apply_performed"])
        self.assertEqual(report["smoke_results"], [])

    def test_missing_failover_plan_blocks_staging_apply(self):
        _, report, _ = run_drill([*apply_args(), "--fixture-pass"])

        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_protected_failover_dry_run", report["blockers"])

    def test_mismatched_failover_plan_blocks_staging_apply(self):
        _, report, _ = run_drill(
            [*apply_args(), "--fixture-pass"],
            failover_plan(failover_attempt_id="failover-other"),
        )

        self.assertEqual(report["status"], "blocked")
        self.assertIn("protected_failover_dry_run_attempt_mismatch", report["blockers"])

    def test_wrong_confirmation_blocks_staging_apply(self):
        args = apply_args()
        args[3] = "wrong-confirmation"
        _, report, _ = run_drill([*args, "--fixture-pass"], failover_plan())

        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_staging_failover_drill_confirmation", report["blockers"])

    def test_backend_postgres_write_delta_blocks(self):
        _, report, _ = run_drill(
            [
                *apply_args(),
                "--backend-postgres-available",
                "false",
                "--provider-mode",
                "supabase_primary",
                "--production-writes-paused",
                "true",
                "--failover-dry-run-status",
                "PASS",
                "--staging-apply-status",
                "PASS",
                "--public-read-status",
                "PASS",
                "--controlled-write-status",
                "PASS",
                "--backend-postgres-write-delta",
                "1",
                "--supabase-controlled-write-count",
                "1",
                "--write-eligible-provider-count",
                "1",
                "--eligible-provider",
                "existing_production_supabase_standby",
                "--write-pause-status",
                "PASS",
                "--split-brain-status",
                "PASS",
                "--negative-path-status",
                "PASS",
            ],
            failover_plan(),
        )

        self.assertEqual(report["status"], "blocked")
        self.assertIn("backend_postgres_received_writes_after_failover", report["blockers"])

    def test_provider_mode_mismatch_blocks(self):
        _, report, _ = run_drill(
            [
                *apply_args(),
                "--backend-postgres-available",
                "false",
                "--provider-mode",
                "backend_postgres_primary",
                "--production-writes-paused",
                "true",
                "--failover-dry-run-status",
                "PASS",
                "--staging-apply-status",
                "PASS",
                "--public-read-status",
                "PASS",
                "--controlled-write-status",
                "PASS",
                "--backend-postgres-write-delta",
                "0",
                "--supabase-controlled-write-count",
                "1",
                "--write-eligible-provider-count",
                "1",
                "--eligible-provider",
                "existing_production_supabase_standby",
                "--write-pause-status",
                "PASS",
                "--split-brain-status",
                "PASS",
                "--negative-path-status",
                "PASS",
            ],
            failover_plan(),
        )

        self.assertEqual(report["status"], "blocked")
        self.assertIn("provider_mode_not_supabase_primary", report["blockers"])

    def test_enforce_returns_nonzero_when_blocked(self):
        exit_code, report, _ = run_drill([*apply_args(), "--fixture-pass", "--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "blocked")

    def test_artifact_omits_secrets_and_raw_data_markers(self):
        _, report, summary = run_drill([*apply_args(), "--fixture-pass"], failover_plan())
        text = json.dumps(report).lower() + summary.lower()

        self.assertTrue(report["safe_metadata_only"])
        for forbidden in (
            "postgres://",
            "postgresql://",
            "password",
            "service_role",
            "select ",
            "insert ",
            "update ",
            "delete ",
            "row_data",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
