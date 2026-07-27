from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_failover_plan as failover
from scripts import validate_backend_supabase_standby_failover


ATTEMPT = "failover-20260727T060000Z"
REVISION = "a" * 40
EPOCH = "epoch-20260727T060000Z"
NOW = "2026-07-27T06:00:30Z"


def go_decision(**extra) -> dict:
    decision = {
        "decision": "GO",
        "status": "GO",
        "decision_id": "sha256:" + "1" * 24,
        "failover_attempt_id": ATTEMPT,
        "candidate_application_revision": REVISION,
        "fence_epoch": EPOCH,
        "expires_at_utc": "2026-07-27T06:05:00Z",
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "single_use": True,
        "consumed": False,
        "safe_metadata_only": True,
    }
    decision.update(extra)
    return decision


def run_failover(args: list[str], decision: dict | None = None):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        output = root / "failover.json"
        summary = root / "summary.md"
        final_args = [*args, "--now-utc", NOW, "--output", str(output), "--summary", str(summary)]
        if decision is not None:
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            final_args.extend(["--promotion-decision", str(decision_path)])
        with redirect_stdout(StringIO()):
            exit_code = failover.main_args(final_args)
        return exit_code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


def apply_args() -> list[str]:
    return [
        "--operation",
        "apply",
        "--confirmation",
        failover.APPLY_CONFIRMATION,
        "--failover-attempt-id",
        ATTEMPT,
        "--candidate-application-revision",
        REVISION,
        "--fence-epoch",
        EPOCH,
    ]


class BackendSupabaseStandbyFailoverTests(unittest.TestCase):
    def test_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_supabase_standby_failover.main(), 0)

    def test_missing_promotion_decision_blocks_dry_run(self):
        exit_code, report, summary = run_failover(["--operation", "dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_supabase_standby_promotion_decision", report["blockers"])
        self.assertIn("Status: `blocked`", summary)

    def test_no_go_decision_blocks_apply(self):
        decision = go_decision(decision="NO-GO", status="NO-GO")
        _, report, _ = run_failover(apply_args(), decision)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("supabase_standby_promotion_decision_not_go", report["blockers"])
        self.assertFalse(report["would_consume_promotion_decision"])

    def test_expired_go_decision_blocks_apply(self):
        decision = go_decision(expires_at_utc="2026-07-27T05:59:00Z")
        _, report, _ = run_failover(apply_args(), decision)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("supabase_standby_promotion_decision_expired", report["blockers"])

    def test_go_apply_would_consume_single_use_decision(self):
        exit_code, report, _ = run_failover(apply_args(), go_decision())

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "apply_ready")
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["would_consume_promotion_decision"])
        self.assertEqual(report["consumption_record"]["decision_id"], "sha256:" + "1" * 24)
        self.assertFalse(report["provider_switch_performed_by_this_workflow"])
        self.assertEqual(
            {item["id"] for item in report["planned_actions"]},
            {"consume_promotion_decision", "app_provider_switch", "worker_provider_switch", "post_failover_smoke"},
        )

    def test_mismatched_attempt_blocks_apply(self):
        decision = go_decision(failover_attempt_id="failover-other")
        _, report, _ = run_failover(apply_args(), decision)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("supabase_standby_promotion_decision_attempt_mismatch", report["blockers"])

    def test_enforce_returns_nonzero_when_blocked(self):
        exit_code, report, _ = run_failover(["--operation", "dry-run", "--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "blocked")

    def test_artifact_is_safe_metadata_only(self):
        _, report, summary = run_failover(apply_args(), go_decision())
        text = json.dumps(report).lower() + summary.lower()

        self.assertTrue(report["safe_metadata_only"])
        for forbidden in ("postgres://", "postgresql://", "password", "service_role", "select ", "insert ", "update ", "delete ", "row_data"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
