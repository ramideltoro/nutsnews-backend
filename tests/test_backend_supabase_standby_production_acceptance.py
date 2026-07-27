from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_production_acceptance as acceptance
from scripts import validate_backend_supabase_standby_production_acceptance


ATTEMPT = "failover-20260727T080000Z-issue505"
REVISION = "c" * 40
EPOCH = "epoch-20260727T080000Z-issue505"


def soak_report(**extra) -> dict:
    report = {
        "status": "PASS",
        "observed_window_hours": 24,
        "relay_health_status": "healthy",
        "max_observed_lag_seconds": 30,
        "critical_backend_health_count": 0,
        "critical_standby_failure_count": 0,
        "parity_status": "PASS",
        "target": "existing_production_supabase_standby",
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_approved_failover": False,
        "safe_metadata_only": True,
    }
    report.update(extra)
    return report


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


def staging_drill(**extra) -> dict:
    drill = {
        "status": "PASS",
        "operation": "staging-apply",
        "drill_id": "backend-supabase-standby-staging-failover-drill",
        "workflow_id": "backend-supabase-standby-staging-failover-drill",
        "failover_attempt_id": ATTEMPT,
        "candidate_application_revision": REVISION,
        "fence_epoch": EPOCH,
        "provider_mode": "supabase_primary",
        "production_writes_paused": True,
        "backend_postgres_write_delta_after_failover": 0,
        "write_eligible_provider_count": 1,
        "eligible_provider": "existing_production_supabase_standby",
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "production_mutation_performed": False,
        "mutation_performed": False,
        "safe_metadata_only": True,
        "blockers": [],
    }
    drill.update(extra)
    return drill


def acceptance_args(owner_decision: str = "GO") -> list[str]:
    return [
        "--operation",
        "acceptance",
        "--confirmation",
        acceptance.ACCEPTANCE_CONFIRMATION,
        "--owner-decision",
        owner_decision,
        "--failover-attempt-id",
        ATTEMPT,
        "--candidate-application-revision",
        REVISION,
        "--fence-epoch",
        EPOCH,
    ]


def run_acceptance(
    args: list[str],
    *,
    soak: dict | None = None,
    failover: dict | None = None,
    staging: dict | None = None,
) -> tuple[int, dict, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        output = root / "acceptance.json"
        summary = root / "summary.md"
        final_args = [*args, "--output", str(output), "--summary", str(summary)]
        if soak is not None:
            soak_path = root / "soak.json"
            soak_path.write_text(json.dumps(soak), encoding="utf-8")
            final_args.extend(["--soak-report", str(soak_path)])
        if failover is not None:
            failover_path = root / "failover-plan.json"
            failover_path.write_text(json.dumps(failover), encoding="utf-8")
            final_args.extend(["--failover-plan", str(failover_path)])
        if staging is not None:
            staging_path = root / "staging-drill.json"
            staging_path.write_text(json.dumps(staging), encoding="utf-8")
            final_args.extend(["--staging-drill", str(staging_path)])
        with redirect_stdout(StringIO()):
            exit_code = acceptance.main_args(final_args)
        return exit_code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


class BackendSupabaseStandbyProductionAcceptanceTests(unittest.TestCase):
    def test_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_supabase_standby_production_acceptance.main(), 0)

    def test_fixture_acceptance_go_passes(self):
        exit_code, report, summary = run_acceptance([*acceptance_args(), "--fixture-pass", "--enforce"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "GO")
        self.assertEqual(report["decision"], "GO")
        self.assertTrue(report["official_backup_accepted"])
        self.assertTrue(report["production_soak_accepted"])
        self.assertTrue(report["production_failover_dry_run_accepted"])
        self.assertTrue(report["staging_failover_drill_accepted"])
        self.assertEqual(report["soak_report"]["observed_window_hours"], 24)
        self.assertEqual(report["soak_report"]["max_observed_lag_seconds"], 30)
        self.assertEqual(report["soak_report"]["relay_health_status"], "healthy")
        self.assertEqual(report["soak_report"]["parity_status"], "PASS")
        self.assertEqual(report["blockers"], [])
        self.assertIn("Decision: `GO`", summary)
        self.assertTrue(report["target_is_existing_production_supabase"])
        self.assertFalse(report["create_new_supabase_project"])
        self.assertFalse(report["create_nutsnews_standby_database"])
        self.assertFalse(report["app_worker_writes_to_supabase_before_approved_failover"])
        self.assertFalse(report["provider_switch_performed"])
        self.assertFalse(report["production_mutation_performed"])
        self.assertTrue(report["requires_fresh_528_go_for_failover"])

    def test_missing_soak_report_blocks_without_fixture(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            failover=failover_plan(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("missing_soak_report", report["blockers"])

    def test_soak_window_under_24_hours_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(observed_window_hours=23.9),
            failover=failover_plan(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("soak_window_incomplete", report["blockers"])

    def test_lag_over_30_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(max_observed_lag_seconds=31),
            failover=failover_plan(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("lag_exceeds_threshold", report["blockers"])

    def test_critical_backend_health_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(critical_backend_health_count=1),
            failover=failover_plan(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("critical_backend_health_present", report["blockers"])

    def test_parity_not_pass_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(parity_status="FAIL"),
            failover=failover_plan(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("parity_not_pass", report["blockers"])

    def test_missing_failover_plan_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("missing_protected_failover_dry_run", report["blockers"])

    def test_mutating_failover_plan_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(),
            failover=failover_plan(mutation_performed=True),
            staging=staging_drill(),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("protected_failover_dry_run_mutated", report["blockers"])

    def test_staging_drill_not_pass_blocks(self):
        _, report, _ = run_acceptance(
            acceptance_args(),
            soak=soak_report(),
            failover=failover_plan(),
            staging=staging_drill(status="FAIL", blockers=["public_read_smoke_failed"]),
        )

        self.assertEqual(report["status"], "NO-GO")
        self.assertIn("staging_failover_drill_not_pass", report["blockers"])

    def test_owner_no_go_blocks(self):
        _, report, _ = run_acceptance([*acceptance_args(owner_decision="NO-GO"), "--fixture-pass"])

        self.assertEqual(report["status"], "NO-GO")
        self.assertEqual(report["decision"], "NO-GO")
        self.assertFalse(report["official_backup_accepted"])
        self.assertIn("owner_recorded_no_go", report["blockers"])

    def test_enforce_returns_nonzero_on_no_go(self):
        exit_code, report, _ = run_acceptance([*acceptance_args(owner_decision="NO-GO"), "--fixture-pass", "--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "NO-GO")

    def test_artifact_omits_secrets_and_raw_data_markers(self):
        _, report, summary = run_acceptance([*acceptance_args(), "--fixture-pass"])
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
