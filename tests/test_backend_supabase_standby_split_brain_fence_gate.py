from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_split_brain_fence_gate as gate


ATTEMPT = "failover-20260727T031500Z"
EPOCH = "epoch-20260727T031500Z"
NOW = "2026-07-27T03:15:30Z"
MEASURED = "2026-07-27T03:15:10Z"


def contract() -> dict:
    return json.loads(gate.DEFAULT_CONTRACT.read_text(encoding="utf-8"))


def writer_pause_gate(**overrides) -> dict:
    report = {
        "status": "PASS",
        "gate": "supabase_standby_writer_pause_quiescence",
        "failover_attempt_id": ATTEMPT,
        "repository_revision": "a" * 40,
        "measured_at_utc": MEASURED,
        "expires_at_utc": "2026-07-27T03:20:10Z",
        "required_writer_count": 5,
        "paused_writer_count": 5,
        "failed_writer_count": 0,
        "blockers": [],
        "backend_postgresql_remains_primary": True,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
    }
    report.update(overrides)
    return report


def fence_evidence(**overrides) -> dict:
    report = {
        "status": "pass",
        "failover_attempt_id": ATTEMPT,
        "fence_epoch": EPOCH,
        "checked_at_utc": MEASURED,
        "source_label": gate.EXPECTED_SOURCE_LABEL,
        "target_label": gate.EXPECTED_TARGET_LABEL,
        "target_is_existing_production_supabase": True,
        "create_new_supabase_project": False,
        "create_nutsnews_standby_database": False,
        "app_worker_writes_to_supabase_before_failover": False,
        "safe_metadata_only": True,
        "lease": {
            "epoch": EPOCH,
            "previous_holder": gate.EXPECTED_BACKEND_PROVIDER,
            "holder": gate.EXPECTED_TARGET_PROVIDER,
        },
        "providers": [
            {
                "id": gate.EXPECTED_BACKEND_PROVIDER,
                "write_eligible": False,
                "fenced": True,
                "epoch": EPOCH,
            },
            {
                "id": gate.EXPECTED_TARGET_PROVIDER,
                "write_eligible": True,
                "fenced": False,
                "epoch": EPOCH,
            },
        ],
        "backend_fence": {
            "backend_worker_database_api_writes_enabled": False,
            "worker_uplift_production_writes_enabled": False,
            "application_write_routes_disabled": True,
            "database_write_roles_revoked_or_blocked": True,
            "stale_writer_epoch_rejected": True,
            "provider_epoch_mismatch_rejects_writes": True,
            "verification_reachable": True,
            "idempotent_retry_safe": True,
        },
        "supabase_fence": {
            "write_eligibility_requires_backend_fence": True,
            "write_credentials_not_exposed_to_app_workers_before_failover": True,
            "write_enabled_only_for_current_epoch": True,
            "verification_reachable": True,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(report.get(key), dict):
            report[key].update(value)
        else:
            report[key] = value
    return report


def with_provider(evidence: dict, provider_id: str, **updates) -> dict:
    cloned = json.loads(json.dumps(evidence))
    for provider in cloned["providers"]:
        if provider["id"] == provider_id:
            provider.update(updates)
            return cloned
    raise AssertionError(f"missing provider {provider_id}")


def run_gate(
    *,
    contract_data: dict | None = None,
    pause: dict | None = None,
    evidence: dict | str | None = None,
    now: str = NOW,
    extra_args: list[str] | None = None,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        contract_path = root / "contract.json"
        pause_path = root / "pause.json"
        evidence_path = root / "fence.json"
        output_path = root / "result.json"
        summary_path = root / "summary.md"
        contract_path.write_text(json.dumps(contract_data or contract()), encoding="utf-8")
        pause_path.write_text(json.dumps(pause or writer_pause_gate()), encoding="utf-8")
        if isinstance(evidence, str):
            evidence_path.write_text(evidence, encoding="utf-8")
        else:
            evidence_path.write_text(json.dumps(evidence or fence_evidence()), encoding="utf-8")
        with redirect_stdout(StringIO()):
            exit_code = gate.main_args(
                [
                    "--contract",
                    str(contract_path),
                    "--writer-pause-gate",
                    str(pause_path),
                    "--fence-evidence",
                    str(evidence_path),
                    "--repository-revision",
                    "b" * 40,
                    "--failover-attempt-id",
                    ATTEMPT,
                    "--fence-epoch",
                    EPOCH,
                    "--now-utc",
                    now,
                    "--output",
                    str(output_path),
                    "--summary",
                    str(summary_path),
                    *(extra_args or []),
                ]
            )
        return exit_code, json.loads(output_path.read_text(encoding="utf-8")), summary_path.read_text(encoding="utf-8")


class BackendSupabaseStandbySplitBrainFenceGateTests(unittest.TestCase):
    def test_complete_fence_and_single_target_write_eligibility_passes(self):
        exit_code, result, summary = run_gate()

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["write_eligible_provider_count"], 1)
        self.assertEqual(result["eligible_provider"], gate.EXPECTED_TARGET_PROVIDER)
        self.assertTrue(result["backend_postgresql_fenced"])
        self.assertTrue(result["target_write_eligible_after_backend_fence"])
        self.assertIn("Status: `PASS`", summary)

    def test_enabling_supabase_before_backend_revocation_fails(self):
        evidence = with_provider(fence_evidence(), gate.EXPECTED_BACKEND_PROVIDER, write_eligible=True, fenced=False)
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("simultaneous_write_eligibility", result["blockers"])
        self.assertIn("supabase_write_enabled_before_backend_fence", result["blockers"])

    def test_stale_process_fixture_fails(self):
        evidence = fence_evidence(backend_fence={"stale_writer_epoch_rejected": False})
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("stale_backend_writer_not_rejected", result["blockers"])
        self.assertIn("backend_postgres_fence_incomplete", result["blockers"])

    def test_stale_epoch_fixture_fails(self):
        evidence = fence_evidence(fence_epoch="epoch-old", lease={"epoch": "epoch-old"})
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fence_epoch_mismatch", result["blockers"])
        self.assertIn("lease_epoch_mismatch", result["blockers"])

    def test_partial_backend_fencing_fails(self):
        evidence = fence_evidence(backend_fence={"backend_worker_database_api_writes_enabled": True})
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("backend_postgres_fence_incomplete", result["blockers"])
        api_control = next(
            item for item in result["fence_controls"] if item["id"] == "backend_worker_database_api_writes_disabled"
        )
        self.assertEqual(api_control["status"], "FAIL")

    def test_retry_not_idempotent_fails(self):
        evidence = fence_evidence(backend_fence={"idempotent_retry_safe": False})
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fence_retry_not_safe", result["blockers"])

    def test_verification_unavailable_fails(self):
        evidence = fence_evidence(backend_fence={"verification_reachable": False})
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fence_verification_unavailable", result["blockers"])

    def test_writer_pause_missing_or_stale_fails(self):
        pause = writer_pause_gate(expires_at_utc="2026-07-27T03:14:00Z", measured_at_utc="2026-07-27T03:10:00Z")
        _, result, _ = run_gate(pause=pause)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("writer_pause_evidence_stale", result["blockers"])

    def test_target_mismatch_fails(self):
        evidence = fence_evidence(target_label="fresh_supabase_project")
        _, result, _ = run_gate(evidence=evidence)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_policy_mismatch", result["blockers"])

    def test_malformed_evidence_fails_closed(self):
        _, result, _ = run_gate(evidence="{")

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["fence_evidence_malformed"])

    def test_enforce_mode_returns_non_zero_on_fail(self):
        evidence = with_provider(fence_evidence(), gate.EXPECTED_TARGET_PROVIDER, write_eligible=False)
        exit_code, result, _ = run_gate(evidence=evidence, extra_args=["--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_not_write_eligible", result["blockers"])

    def test_artifact_is_safe_metadata_only(self):
        _, result, _ = run_gate()
        serialized = json.dumps(result).lower()

        self.assertTrue(result["safe_metadata_only"])
        for forbidden in ("postgres://", "password", "service_role", "select ", "insert ", "update ", "delete ", "row_data"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
