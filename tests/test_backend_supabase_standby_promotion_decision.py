from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_promotion_decision as decision


ATTEMPT = "failover-20260727T050000Z"
EPOCH = "epoch-20260727T050000Z"
NOW = "2026-07-27T05:00:30Z"
MEASURED = "2026-07-27T05:00:10Z"
EXPIRES = "2026-07-27T05:05:10Z"
REVISION = "a" * 40
SOURCE_BINDING = decision.standby_binding_fingerprint("source", decision.EXPECTED_SOURCE_LABEL)
TARGET_BINDING = decision.standby_binding_fingerprint("target", decision.EXPECTED_TARGET_LABEL)


def base_gate(gate: str, issue: str, **extra) -> dict:
    report = {
        "status": "PASS",
        "gate": gate,
        "issue": issue,
        "epic": decision.EPIC,
        "failover_attempt_id": ATTEMPT,
        "measured_at_utc": MEASURED,
        "expires_at_utc": EXPIRES,
        "source_fingerprint": "sha256:" + "1" * 24,
        "target_fingerprint": "sha256:" + "2" * 24,
        "source_binding_fingerprint": SOURCE_BINDING,
        "target_binding_fingerprint": TARGET_BINDING,
        "blockers": [],
        "backend_postgresql_remains_primary": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }
    report.update(extra)
    return report


def happy_artifacts() -> dict[str, dict]:
    return {
        "lag": base_gate("supabase_standby_lag", "ramideltoro/nutsnews#522"),
        "parity": base_gate("supabase_standby_required_table_parity", "ramideltoro/nutsnews#523"),
        "schema": base_gate(
            "supabase_standby_schema_compatibility",
            "ramideltoro/nutsnews#524",
            candidate_application_revision=REVISION,
            repository_revision=REVISION,
        ),
        "sequence": base_gate(
            "supabase_standby_sequence_safety",
            "ramideltoro/nutsnews#525",
            repository_revision=REVISION,
        ),
        "writer_pause": base_gate(
            "supabase_standby_writer_pause_quiescence",
            "ramideltoro/nutsnews#526",
            repository_revision=REVISION,
        ),
        "split_brain_fence": base_gate(
            "supabase_standby_split_brain_fence",
            "ramideltoro/nutsnews#527",
            repository_revision=REVISION,
            fence_epoch=EPOCH,
            backend_postgresql_remains_primary_until_approved_failover=True,
            write_eligible_provider_count=1,
            eligible_provider=decision.EXPECTED_TARGET_LABEL,
            backend_postgresql_fenced=True,
            target_write_eligible_after_backend_fence=True,
        ),
    }


def run_decision(
    artifacts: dict[str, dict | str],
    *,
    now: str = NOW,
    extra_args: list[str] | None = None,
    missing: str | None = None,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        paths: dict[str, Path] = {}
        for key, payload in artifacts.items():
            path = root / f"{key}.json"
            if isinstance(payload, str):
                path.write_text(payload, encoding="utf-8")
            else:
                path.write_text(json.dumps(payload), encoding="utf-8")
            paths[key] = path
        if missing:
            paths[missing].unlink()
        output = root / "decision.json"
        summary = root / "summary.md"
        with redirect_stdout(StringIO()):
            exit_code = decision.main_args(
                [
                    "--lag-gate",
                    str(paths["lag"]),
                    "--parity-gate",
                    str(paths["parity"]),
                    "--schema-gate",
                    str(paths["schema"]),
                    "--sequence-gate",
                    str(paths["sequence"]),
                    "--writer-pause-gate",
                    str(paths["writer_pause"]),
                    "--split-brain-fence-gate",
                    str(paths["split_brain_fence"]),
                    "--failover-attempt-id",
                    ATTEMPT,
                    "--candidate-application-revision",
                    REVISION,
                    "--repository-revision",
                    REVISION,
                    "--fence-epoch",
                    EPOCH,
                    "--now-utc",
                    now,
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                    *(extra_args or []),
                ]
            )
        return exit_code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


class BackendSupabaseStandbyPromotionDecisionTests(unittest.TestCase):
    def test_happy_path_returns_go(self):
        exit_code, result, summary = run_decision(happy_artifacts())

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["decision"], "GO")
        self.assertEqual(result["status"], "GO")
        self.assertEqual(result["passed_gate_count"], 6)
        self.assertEqual(result["failed_gate_count"], 0)
        self.assertEqual(result["blockers"], [])
        self.assertTrue(result["single_use"])
        self.assertFalse(result["provider_switch_performed"])
        self.assertIn("Decision: `GO`", summary)

    def test_each_failing_gate_produces_no_go(self):
        for key in decision.REQUIRED_GATES:
            with self.subTest(key=key):
                artifacts = happy_artifacts()
                artifacts[key]["status"] = "FAIL"
                artifacts[key]["blockers"] = [f"{key}_fixture_failure"]
                _, result, _ = run_decision(artifacts)

                self.assertEqual(result["decision"], "NO-GO")
                self.assertIn(f"{key}_gate_not_pass", result["blockers"])
                self.assertIn("not_all_gates_passed", result["blockers"])

    def test_missing_gate_evidence_fails_closed(self):
        _, result, _ = run_decision(happy_artifacts(), missing="lag")

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("lag_gate_missing", result["blockers"])

    def test_malformed_gate_evidence_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["parity"] = "{"
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("parity_gate_malformed", result["blockers"])

    def test_stale_gate_evidence_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["schema"]["expires_at_utc"] = "2026-07-27T04:59:00Z"
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("schema_evidence_expired", result["blockers"])

    def test_mismatched_attempt_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["sequence"]["failover_attempt_id"] = "failover-other"
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("sequence_attempt_mismatch", result["blockers"])

    def test_mismatched_target_binding_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["writer_pause"]["target_binding_fingerprint"] = "sha256:" + "9" * 24
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("writer_pause_target_binding_mismatch", result["blockers"])

    def test_mismatched_revision_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["schema"]["candidate_application_revision"] = "b" * 40
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("schema_candidate_application_revision_mismatch", result["blockers"])

    def test_mismatched_epoch_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["split_brain_fence"]["fence_epoch"] = "epoch-old"
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("split_brain_fence_fence_epoch_mismatch", result["blockers"])

    def test_duplicate_gate_evidence_fails_closed(self):
        artifacts = happy_artifacts()
        artifacts["parity"]["gate"] = "supabase_standby_lag"
        _, result, _ = run_decision(artifacts)

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("duplicate_gate_evidence", result["blockers"])
        self.assertIn("parity_gate_mismatch", result["blockers"])

    def test_go_cannot_be_reused_after_consumption(self):
        artifacts = happy_artifacts()
        _, first, _ = run_decision(artifacts)

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "ledger.json"
            ledger.write_text(json.dumps({"consumed_decision_ids": [first["decision_id"]]}), encoding="utf-8")
            _, result, _ = run_decision(artifacts, extra_args=["--consumption-ledger", str(ledger)])

        self.assertEqual(result["decision"], "NO-GO")
        self.assertTrue(result["consumed"])
        self.assertIn("decision_already_consumed", result["blockers"])

    def test_go_cannot_be_reused_after_expiry(self):
        _, result, _ = run_decision(happy_artifacts(), now="2026-07-27T05:06:00Z")

        self.assertEqual(result["decision"], "NO-GO")
        self.assertIn("lag_evidence_expired", result["blockers"])

    def test_enforce_returns_nonzero_on_no_go(self):
        artifacts = happy_artifacts()
        artifacts["lag"]["status"] = "FAIL"
        exit_code, result, _ = run_decision(artifacts, extra_args=["--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(result["decision"], "NO-GO")

    def test_artifact_is_safe_metadata_only(self):
        _, result, summary = run_decision(happy_artifacts())
        text = json.dumps(result).lower() + summary.lower()

        self.assertTrue(result["safe_metadata_only"])
        for forbidden in ("postgres://", "postgresql://", "password", "service_role", "select ", "insert ", "update ", "delete ", "row_data"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
