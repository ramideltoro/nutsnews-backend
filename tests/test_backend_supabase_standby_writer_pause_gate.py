from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_writer_pause_gate as gate


NOW = "2026-07-26T22:20:00Z"
PAUSE_STARTED = "2026-07-26T22:18:45Z"
FIRST = "2026-07-26T22:19:00Z"
SECOND = "2026-07-26T22:19:30Z"
ATTEMPT = "failover-20260726T221845Z"


def inventory() -> dict:
    return json.loads(gate.DEFAULT_INVENTORY.read_text(encoding="utf-8"))


def writer_classes() -> list[dict]:
    return [
        {
            "id": item["id"],
            "class": item["class"],
            "kind": item["kind"],
            "production_write_path": item.get("production_write_path"),
            "paused": True,
            "blockers": [],
            "safe_status_only": True,
        }
        for item in inventory()["writer_classes"]
    ]


def pause_report(**overrides) -> dict:
    inv = inventory()
    report = {
        "status": "pass",
        "action": "pause",
        "attempt_id": ATTEMPT,
        "pause_started_at_utc": PAUSE_STARTED,
        "writer_inventory_version": inv["schema_version"],
        "writer_inventory_fingerprint": "sha256:" + gate.canonical_sha256(inv)[:24],
        "writer_classes": writer_classes(),
        "unknown_writers": [],
        "drain": {
            "status": "pass",
            "timeout_seconds": 180,
            "checked_at_utc": FIRST,
            "undrained_services": [],
        },
        "all_writers_paused": True,
        "active_pause_state": True,
        "resumed_at_utc": None,
        "safe_metadata_only": True,
    }
    report.update(overrides)
    return report


def write_position(checked_at: str, fingerprint: str = "sha256:" + "a" * 24, **overrides) -> dict:
    report = {
        "status": "pass",
        "issue": gate.ISSUE,
        "epic": gate.EPIC,
        "checked_at_utc": checked_at,
        "source_label": gate.EXPECTED_SOURCE_LABEL,
        "target_label": gate.EXPECTED_TARGET_LABEL,
        "manifest_fingerprint": "f" * 64,
        "relay_contract_fingerprint": "sha256:" + "b" * 24,
        "required_table_count": 1,
        "passed_table_count": 1,
        "failed_table_count": 0,
        "write_position_fingerprint": fingerprint,
        "tables": [
            {
                "id": "table.public.worker_runs",
                "name": "public.worker_runs",
                "status": "pass",
                "row_count": 2,
                "row_checksum_sha256": "c" * 64,
                "column_contract_sha256": "d" * 64,
                "primary_key_fingerprint": "sha256:" + "e" * 24,
                "sensitivity": "aggregate_and_hash_only",
            }
        ],
        "safe_metadata_only": True,
    }
    report.update(overrides)
    return report


def automation_report(**overrides) -> dict:
    report = {
        "status": "pass",
        "checked_at_utc": FIRST,
        "manual_freeze_confirmed": True,
        "active_writer_workflows": [],
        "checked_workflows": [
            "protected-backend-ansible-apply.yml",
            "backend-worker-runtime-operations.yml",
            "backend-production-cutover.yml",
        ],
        "safe_metadata_only": True,
    }
    report.update(overrides)
    return report


def run_gate(
    *,
    inv: dict | None = None,
    pause: dict | None = None,
    first: dict | None = None,
    second: dict | None = None,
    automation: dict | None = None,
    now: str = NOW,
    extra_args: list[str] | None = None,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        inventory_path = root / "inventory.json"
        pause_path = root / "pause.json"
        first_path = root / "first.json"
        second_path = root / "second.json"
        automation_path = root / "automation.json"
        output_path = root / "gate.json"
        summary_path = root / "summary.md"
        inventory_path.write_text(json.dumps(inv or inventory()), encoding="utf-8")
        pause_path.write_text(json.dumps(pause or pause_report()), encoding="utf-8")
        first_path.write_text(json.dumps(first or write_position(FIRST)), encoding="utf-8")
        second_path.write_text(json.dumps(second or write_position(SECOND)), encoding="utf-8")
        automation_path.write_text(json.dumps(automation or automation_report()), encoding="utf-8")
        with redirect_stdout(StringIO()):
            exit_code = gate.main_args(
                [
                    "--inventory",
                    str(inventory_path),
                    "--pause-report",
                    str(pause_path),
                    "--first-write-position",
                    str(first_path),
                    "--second-write-position",
                    str(second_path),
                    "--automation-report",
                    str(automation_path),
                    "--failover-attempt-id",
                    ATTEMPT,
                    "--repository-revision",
                    "c" * 40,
                    "--quiet-window-seconds",
                    "30",
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


class BackendSupabaseStandbyWriterPauseGateTests(unittest.TestCase):
    def test_complete_pause_and_stable_write_position_passes(self):
        exit_code, result, summary = run_gate()

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["paused_writer_count"], 5)
        self.assertEqual(result["actual_quiet_window_seconds"], 30)
        self.assertIn("Status: `PASS`", summary)

    def test_active_writer_fixture_fails(self):
        writers = writer_classes()
        writers[0]["paused"] = False
        result = run_gate(pause=pause_report(status="fail", all_writers_paused=False, writer_classes=writers))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("writer_pause_failed", result["blockers"])
        api = next(item for item in result["writer_classes"] if item["id"] == "backend_worker_database_api")
        self.assertIn("writer_class_not_paused", api["blockers"])

    def test_unknown_writer_fixture_fails(self):
        result = run_gate(pause=pause_report(status="fail", unknown_writers=["worker:surprise"]))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("unknown_writer", result["blockers"])

    def test_failed_pause_fixture_fails(self):
        result = run_gate(pause=pause_report(status="fail", all_writers_paused=False))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("pause_report_failed", result["blockers"])
        self.assertIn("not_all_writers_paused", result["blockers"])

    def test_drain_timeout_fixture_fails(self):
        pause = pause_report(status="fail", drain={"status": "fail", "timeout_seconds": 180, "undrained_services": ["fetcher"]})
        result = run_gate(pause=pause)[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("drain_timeout", result["blockers"])

    def test_resumed_writer_fixture_fails(self):
        result = run_gate(pause=pause_report(status="fail", resumed_at_utc="2026-07-26T22:19:10Z"))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("writer_resumed_during_attempt", result["blockers"])

    def test_observed_write_fixture_fails(self):
        first = write_position(FIRST, fingerprint="sha256:" + "a" * 24)
        second = write_position(SECOND, fingerprint="sha256:" + "f" * 24)
        result = run_gate(first=first, second=second)[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("observed_write_position_advance", result["blockers"])

    def test_stale_evidence_fails(self):
        result = run_gate(second=write_position("2026-07-26T22:00:00Z"))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("evidence_stale", result["blockers"])

    def test_incomplete_inventory_fails(self):
        inv = inventory()
        inv["writer_classes"] = inv["writer_classes"][:-1]
        exit_code, result, _ = run_gate(inv=inv)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("writer_inventory_incomplete", result["blockers"])

    def test_active_writer_workflow_fails(self):
        automation = automation_report(status="fail", active_writer_workflows=[{"workflow": "protected-backend-ansible-apply.yml"}])
        result = run_gate(automation=automation)[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("writer_automation_freeze_failed", result["blockers"])
        self.assertIn("active_writer_workflow", result["blockers"])

    def test_manual_freeze_must_be_confirmed(self):
        result = run_gate(automation=automation_report(manual_freeze_confirmed=False))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("manual_freeze_not_confirmed", result["blockers"])

    def test_existing_production_supabase_policy_fails_closed(self):
        inv = inventory()
        inv["target"]["existing_production_supabase_project"] = False
        inv["target"]["create_new_supabase_project"] = True
        inv["safety"]["app_worker_writes_to_supabase_before_failover"] = True
        result = run_gate(inv=inv, pause=pause_report(writer_inventory_fingerprint="sha256:" + gate.canonical_sha256(inv)[:24]))[1]

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_existing_production_supabase_not_confirmed", result["blockers"])
        self.assertIn("new_supabase_project_not_forbidden", result["blockers"])
        self.assertIn("app_worker_supabase_writes_not_blocked", result["blockers"])

    def test_enforce_returns_nonzero_on_failure(self):
        exit_code, result, _ = run_gate(pause=pause_report(status="fail", unknown_writers=["worker:surprise"]), extra_args=["--enforce"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")

    def test_artifact_is_safe_metadata_only(self):
        _, result, _ = run_gate()
        encoded = json.dumps(result).lower()

        self.assertTrue(result["safe_metadata_only"])
        for forbidden in ("postgres://", "postgresql://", "pgpassword", "service_role", "select ", "row-md5-digest"):
            self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()
