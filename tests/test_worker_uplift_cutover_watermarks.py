from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.test_worker_uplift_stage_health_projection import ProjectionFixture


ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = ROOT / "scripts/backend_worker_uplift_cutover_watermarks.py"
VALIDATOR = ROOT / "scripts/validate_worker_uplift_cutover_watermarks.py"
CONTRACT = ROOT / "docs/worker-uplift-cutover-watermark-authorization.json"
WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-cutover-watermarks.yml"
STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watermarks = load_module("worker_uplift_cutover_watermarks", IMPLEMENTATION)
validator = load_module("validate_worker_uplift_cutover_watermarks", VALIDATOR)


def contract_value():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def privilege_proof():
    return {
        "current_user": watermarks.WATERMARK_ROLE,
        "database_create": False,
        "role_create": False,
        "superuser": False,
        "role_inherit": False,
        "row_level_security_bypass": False,
        "role_memberships": [],
        "schema_create_grants": [],
        "other_mutation_grants": [],
        "targets": [
            {
                "stage": stage,
                "target_select": True,
                "target_insert": True,
                "target_update": True,
                "target_delete": False,
                "target_truncate": False,
                "target_trigger": False,
                "target_references": False,
                "inbox_select": True,
                "outbox_select": True,
                "sequence_usage": True,
            }
            for stage in STAGES
        ],
    }


def database_snapshot(observed, *, current_rows=None, active_inbox=None):
    current_rows = current_rows or {}
    active_inbox = active_inbox or {}
    return {
        "stages": [
            {
                "stage": stage,
                "schema": f"worker_uplift_{stage}",
                "watermark_row_count": 1 if stage in current_rows else 0,
                "target_watermark_count": 1 if stage in current_rows else 0,
                "current_target": current_rows.get(stage),
                "max_confirmed_outbox_id": index + 1,
                "max_confirmed_message_id": f"message-{stage}",
                "max_confirmed_at": watermarks.iso_utc(observed - timedelta(seconds=30)),
                "confirmed_outbox_count": index + 1,
                "unconfirmed_outbox_count": 0,
                "retrying_outbox_count": 0,
                "dead_lettered_outbox_count": 0,
                "active_inbox_count": active_inbox.get(stage, 0),
                "failed_or_parked_inbox_count": 2 if stage == "translation" else (50 if stage == "persistence" else 0),
                "failed_or_parked_reason_bucket_count": 1 if stage in {"translation", "persistence"} else 0,
                "schema_fingerprint": f"{index + 1:032x}",
            }
            for index, stage in enumerate(STAGES)
        ]
    }


class WatermarkFixture:
    def __init__(self, root: Path):
        first_root = root / "first"
        followup_root = root / "followup"
        first_root.mkdir()
        followup_root.mkdir()
        first_fixture = ProjectionFixture(first_root)
        followup_fixture = ProjectionFixture(followup_root)
        followup_fixture.observed = first_fixture.observed + timedelta(seconds=10)
        followup_fixture.runtime["generated_at_utc"] = followup_fixture.observed.isoformat().replace("+00:00", "Z")
        followup_fixture.write()
        self.observed = followup_fixture.observed
        self.first = first_fixture.build()
        self.followup = followup_fixture.build()
        self.first_path = root / "first-runtime.json"
        self.followup_path = root / "followup-runtime.json"
        self.first_path.write_text(json.dumps(self.first), encoding="utf-8")
        self.followup_path.write_text(json.dumps(self.followup), encoding="utf-8")

    def build(self, snapshot=None):
        snapshot = snapshot or database_snapshot(self.observed)
        with mock.patch.object(watermarks, "psql_json", side_effect=[privilege_proof(), snapshot]):
            return watermarks.build_candidate(
                contract_value(),
                self.first_path,
                self.followup_path,
                "fixture-password",
                now=self.observed + timedelta(seconds=5),
            )


class WorkerUpliftCutoverWatermarkTests(unittest.TestCase):
    def test_committed_contract_workflow_implementation_and_ansible_validate(self):
        self.assertEqual(validator.validate_contract(contract_value()), [])
        self.assertEqual(validator.validate_workflow(WORKFLOW.read_text(encoding="utf-8")), [])
        self.assertEqual(validator.validate_implementation(IMPLEMENTATION.read_text(encoding="utf-8")), [])
        self.assertEqual(
            validator.validate_ansible(
                validator.DEFAULTS.read_text(encoding="utf-8"),
                validator.POSTGRES_TASKS.read_text(encoding="utf-8"),
                validator.MODEL_TEMPLATE.read_text(encoding="utf-8"),
                validator.PROTECTED_APPLY.read_text(encoding="utf-8"),
            ),
            [],
        )

    def test_builds_exact_value_free_eight_stage_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = WatermarkFixture(Path(tmp)).build()
        self.assertEqual(artifact["status"], "pass")
        self.assertEqual(artifact["candidate"]["row_count"], 8)
        self.assertEqual([row["stage"] for row in artifact["candidate"]["rows"]], list(STAGES))
        self.assertTrue(all(row["watermark_name"] == "cutover-boundary-v1" for row in artifact["candidate"]["rows"]))
        self.assertTrue(all(row["lag_count"] == 0 for row in artifact["candidate"]["rows"]))
        self.assertEqual(artifact["retained_failure_aggregates"]["translation"]["count"], 2)
        self.assertEqual(artifact["retained_failure_aggregates"]["persistence"]["count"], 50)
        self.assertEqual(list(artifact["pre_state"]), list(STAGES))
        self.assertTrue(all(state["watermark_row_count"] == 0 for state in artifact["pre_state"].values()))
        self.assertFalse(artifact["safety"]["production_writes_enabled"])
        self.assertEqual(artifact["safety"]["active_ingestion_owner"], "legacy_shards")

    def test_candidate_is_deterministic_for_same_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WatermarkFixture(Path(tmp))
            first = fixture.build()
            second = fixture.build()
        self.assertEqual(first, second)
        self.assertEqual(watermarks.sha256_json(first), watermarks.sha256_json(second))

    def test_exact_consumer_count_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WatermarkFixture(Path(tmp))
            for artifact in (fixture.first, fixture.followup):
                artifact["candidate"]["rows"][1]["active_consumers"] = 2
            fixture.first_path.write_text(json.dumps(fixture.first), encoding="utf-8")
            fixture.followup_path.write_text(json.dumps(fixture.followup), encoding="utf-8")
            with self.assertRaisesRegex(watermarks.WatermarkError, "exact authorized consumer count"):
                fixture.build()

    def test_nonzero_active_inbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WatermarkFixture(Path(tmp))
            snapshot = database_snapshot(fixture.observed, active_inbox={"approval": 1})
            with self.assertRaisesRegex(watermarks.WatermarkError, "active_inbox_count is not zero"):
                fixture.build(snapshot)

    def test_unexpected_watermark_row_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WatermarkFixture(Path(tmp))
            snapshot = database_snapshot(fixture.observed)
            snapshot["stages"][0]["watermark_row_count"] = 1
            snapshot["stages"][0]["target_watermark_count"] = 0
            with self.assertRaisesRegex(watermarks.WatermarkError, "unexpected reconciliation watermark"):
                fixture.build(snapshot)

    def test_fixed_sql_contains_exactly_eight_upserts_and_no_destructive_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = WatermarkFixture(Path(tmp)).build()["candidate"]["rows"]
        sql = watermarks.apply_sql(rows)
        self.assertEqual(sql.lower().count("insert into worker_uplift_"), 8)
        for stage in STAGES:
            self.assertIn(f"insert into worker_uplift_{stage}.reconciliation_watermarks", sql)
        self.assertIn("exact_row_guard", sql)
        self.assertIn("capturedAtUtc", sql)
        self.assertNotRegex(sql.lower(), r"\bdelete\s+from\b|\btruncate\b|\balter\s+table\b|\bdrop\s+")

    def test_privilege_proof_rejects_other_mutation(self):
        proof = privilege_proof()
        watermarks.validate_privilege_proof(proof)
        proof["other_mutation_grants"] = [{"schema": "public", "table": "articles", "privilege": "UPDATE"}]
        with self.assertRaisesRegex(watermarks.WatermarkError, "outside the exact authorized tables"):
            watermarks.validate_privilege_proof(proof)

    def test_apply_is_bound_to_exact_candidate_and_all_eight_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WatermarkFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            before = database_snapshot(fixture.observed)
            expected = watermarks.expected_db_rows(artifact)
            current = {stage: row for stage, row in zip(STAGES, expected)}
            after = database_snapshot(fixture.observed, current_rows=current)
            mutation = {
                "upserted_rows": 8,
                "expected_rows": 8,
                "per_stage": {stage: 1 for stage in STAGES},
                "exact_row_guard": 1,
            }
            with mock.patch.object(watermarks, "utc_now", return_value=fixture.observed + timedelta(seconds=5)):
                with mock.patch.object(
                    watermarks,
                    "psql_json",
                    side_effect=[privilege_proof(), before, mutation, after],
                ):
                    report = watermarks.apply_candidate(
                        contract_value(),
                        artifact_path,
                        "fixture-password",
                        watermarks.APPLY_CONFIRMATION,
                    )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["mutation"]["upserted_rows"], 8)
        self.assertTrue(report["schema_fingerprints"]["unchanged"])
        self.assertTrue(report["non_target_database_state"]["unchanged"])
        self.assertEqual(report["database_evidence"]["rows"], expected)

    def test_apply_rerun_is_idempotent_for_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WatermarkFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            expected = watermarks.expected_db_rows(artifact)
            current = {stage: row for stage, row in zip(STAGES, expected)}
            unchanged = database_snapshot(fixture.observed, current_rows=current)
            mutation = {
                "upserted_rows": 8,
                "expected_rows": 8,
                "per_stage": {stage: 1 for stage in STAGES},
                "exact_row_guard": 1,
            }
            with mock.patch.object(watermarks, "utc_now", return_value=fixture.observed + timedelta(seconds=5)):
                with mock.patch.object(
                    watermarks,
                    "psql_json",
                    side_effect=[privilege_proof(), unchanged, mutation, unchanged],
                ):
                    report = watermarks.apply_candidate(
                        contract_value(),
                        artifact_path,
                        "fixture-password",
                        watermarks.APPLY_CONFIRMATION,
                    )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["mutation"]["upserted_rows"], 8)
        self.assertEqual(report["non_target_database_state"]["before_sha256"], report["non_target_database_state"]["after_sha256"])

    def test_stale_candidate_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WatermarkFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            newer = copy.deepcopy(watermarks.expected_db_rows(artifact)[0])
            newer_at = fixture.observed + timedelta(minutes=1)
            newer["confirmed_at"] = watermarks.iso_utc(newer_at)
            newer["diagnostic_metadata"]["capturedAtUtc"] = watermarks.iso_utc(newer_at)
            before = database_snapshot(fixture.observed, current_rows={"scheduler": newer})
            with mock.patch.object(watermarks, "utc_now", return_value=fixture.observed + timedelta(seconds=5)):
                with mock.patch.object(watermarks, "psql_json", side_effect=[privilege_proof(), before]) as psql:
                    with self.assertRaisesRegex(watermarks.WatermarkError, "stale evidence"):
                        watermarks.apply_candidate(
                            contract_value(),
                            artifact_path,
                            "fixture-password",
                            watermarks.APPLY_CONFIRMATION,
                        )
            self.assertEqual(psql.call_count, 2)

    def test_wrong_confirmation_blocks_before_database_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WatermarkFixture(root)
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(fixture.build()), encoding="utf-8")
            with mock.patch.object(watermarks, "psql_json") as psql:
                with self.assertRaisesRegex(watermarks.WatermarkError, "exact typed confirmation"):
                    watermarks.apply_candidate(contract_value(), artifact_path, "fixture-password", "wrong")
                psql.assert_not_called()

    def test_post_apply_proof_matches_exact_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = WatermarkFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            expected = watermarks.expected_db_rows(artifact)
            current = {stage: row for stage, row in zip(STAGES, expected)}
            after = database_snapshot(fixture.observed, current_rows=current)
            non_target_sha = watermarks.sha256_json(watermarks.non_target_db_state(after))
            apply_report = {
                "status": "applied",
                "candidate_artifact_sha256": watermarks.sha256_file(artifact_path),
                "workflow": {"run_id": "local", "commit": "local"},
                "schema_fingerprints": {"unchanged": True},
                "non_target_database_state": {
                    "before_sha256": non_target_sha,
                    "after_sha256": non_target_sha,
                    "unchanged": True,
                },
            }
            apply_report_path = root / "apply.json"
            apply_report_path.write_text(json.dumps(apply_report), encoding="utf-8")
            with mock.patch.object(watermarks, "utc_now", return_value=fixture.observed + timedelta(seconds=20)):
                with mock.patch.object(watermarks, "psql_json", side_effect=[privilege_proof(), after]):
                    proof = watermarks.verify_post_apply(
                        contract_value(),
                        artifact_path,
                        apply_report_path,
                        fixture.followup_path,
                        "fixture-password",
                    )
        self.assertEqual(proof["proof"], contract_value()["post_apply_proof"])

    def test_scope_drift_invalidates_standing_authorization(self):
        contract = copy.deepcopy(contract_value())
        contract["scope_fingerprint"]["watermark_name"] = "expanded"
        errors = validator.validate_contract(contract)
        self.assertTrue(any("standing authorization" in error or "scope fingerprint" in error for error in errors))

    def test_candidate_redaction_rejects_private_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = WatermarkFixture(Path(tmp)).build()
        artifact["candidate"]["rows"][0]["diagnostic_metadata"]["password"] = "fixture"
        with self.assertRaisesRegex(watermarks.WatermarkError, "credential or private-value"):
            watermarks.ensure_value_free(artifact, "fixture")

    def test_retained_failure_aggregate_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = WatermarkFixture(Path(tmp)).build()
        artifact["retained_failure_aggregates"]["translation"]["count"] = 0
        with self.assertRaisesRegex(watermarks.WatermarkError, "retained failure aggregates"):
            watermarks.validate_candidate_artifact(artifact, contract_value())


if __name__ == "__main__":
    unittest.main()
