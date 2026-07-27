from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_recovery_boundaries as recovery
from scripts import validate_backend_supabase_standby_recovery_boundaries


def evidence(evidence_id: str) -> dict:
    payload = {"status": "PASS", "safe_metadata_only": True}
    if evidence_id in {
        "backend_rebuild_or_reconciliation_from_supabase",
        "supabase_to_backend_parity",
    }:
        payload.update(
            {
                "source_label": recovery.SUPABASE_STANDBY,
                "target_label": "backend_postgres_rebuilt_candidate",
            }
        )
    if evidence_id == "backend_sequence_safety":
        payload["target_label"] = "backend_postgres_rebuilt_candidate"
    if evidence_id == "no_split_brain_fence":
        payload.update(
            {
                "write_eligible_provider_count": 1,
                "eligible_provider": "backend_postgres_rebuilt_candidate",
            }
        )
    return payload


def run_recovery(args: list[str]):
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / "recovery.json"
        summary = Path(tmpdir) / "summary.md"
        with redirect_stdout(StringIO()):
            exit_code = recovery.main_args([*args, "--output", str(output), "--summary", str(summary)])
        return exit_code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


class BackendSupabaseStandbyRecoveryBoundariesTests(unittest.TestCase):
    def test_validator_passes(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(validate_backend_supabase_standby_recovery_boundaries.main(), 0)

    def test_post_failover_forward_recovery_blocks_backend_reuse(self):
        exit_code, report, summary = run_recovery(
            [
                "--boundary",
                "post_supabase_failover_forward_recovery",
                "--provider-switch-performed",
                "true",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "dry_run_ready")
        self.assertEqual(report["authoritative_provider"], recovery.SUPABASE_STANDBY)
        self.assertFalse(report["backend_postgres_reuse_allowed"])
        self.assertIn("Status: `dry_run_ready`", summary)

    def test_switch_back_without_evidence_fails_closed(self):
        exit_code, report, _ = run_recovery(
            [
                "--boundary",
                "switch_back_to_backend_postgres",
                "--provider-switch-performed",
                "true",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_supabase_to_backend_parity_evidence", report["blockers"])
        self.assertIn("missing_backend_sequence_safety_evidence", report["blockers"])
        self.assertIn("missing_no_split_brain_fence_evidence", report["blockers"])
        self.assertIn("not_all_switch_back_gates_passed", report["blockers"])

    def test_switch_back_with_safe_evidence_is_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = [
                "--boundary",
                "switch_back_to_backend_postgres",
                "--provider-switch-performed",
                "true",
            ]
            for evidence_id, argument in recovery.SWITCH_BACK_EVIDENCE.items():
                path = root / f"{evidence_id}.json"
                path.write_text(json.dumps(evidence(evidence_id)), encoding="utf-8")
                args.extend([f"--{argument.replace('_', '-')}", str(path)])
            output = root / "recovery.json"
            with redirect_stdout(StringIO()):
                exit_code = recovery.main_args([*args, "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "dry_run_ready")
        self.assertEqual(report["passed_switch_back_gate_count"], report["required_switch_back_gate_count"])
        self.assertFalse(report["mutation_performed"])

    def test_pre_switch_abort_blocks_after_provider_switch(self):
        _, report, _ = run_recovery(
            [
                "--boundary",
                "pre_switch_abort",
                "--provider-switch-performed",
                "true",
            ]
        )

        self.assertEqual(report["status"], "blocked")
        self.assertIn("pre_switch_abort_not_allowed_after_provider_switch", report["blockers"])

    def test_enforce_returns_nonzero_for_blocked_switch_back(self):
        exit_code, report, _ = run_recovery(
            [
                "--boundary",
                "switch_back_to_backend_postgres",
                "--provider-switch-performed",
                "true",
                "--enforce",
            ]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "blocked")

    def test_artifact_is_safe_metadata_only(self):
        _, report, summary = run_recovery(
            [
                "--boundary",
                "switch_back_to_backend_postgres",
                "--provider-switch-performed",
                "true",
            ]
        )
        text = json.dumps(report).lower() + summary.lower()

        self.assertTrue(report["safe_metadata_only"])
        for forbidden in ("postgres://", "postgresql://", "password", "service_role", "select ", "insert ", "update ", "delete ", "row_data"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
