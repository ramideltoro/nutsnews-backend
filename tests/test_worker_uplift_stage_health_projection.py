from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = ROOT / "scripts/backend_worker_uplift_stage_health_projection.py"
VALIDATOR = ROOT / "scripts/validate_worker_uplift_stage_health_projection.py"
CONTRACT = ROOT / "docs/worker-uplift-stage-health-projection-authorization.json"
WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-stage-health-projection.yml"
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


projection = load_module("worker_uplift_stage_health_projection", IMPLEMENTATION)
validator = load_module("validate_worker_uplift_stage_health_projection", VALIDATOR)


class ProjectionFixture:
    def __init__(self, root: Path):
        self.root = root
        self.observed = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        self.runtime_path = root / "runtime-status.json"
        self.manifest_path = root / "runtime-manifest.json"
        self.compose_path = root / "runtime-compose.yml"
        self.queue_dir = root / "queue-evidence"
        self.queue_dir.mkdir()
        self.runtime = self.runtime_value()
        self.manifest = self.manifest_value()
        self.write()

    def runtime_value(self):
        services = {}
        for stage in STAGES:
            consumer = {
                "status": "not_applicable",
                "required": False,
                "queues": [],
            }
            if stage != "scheduler":
                consumer = {
                    "status": "healthy",
                    "required": True,
                    "queues": [
                        {
                            "status": "healthy",
                            "queue": f"nutsnews.worker.{stage}.v1",
                            "metrics": {
                                "consumers": 1,
                                "messages": 0,
                                "messages_ready": 0,
                                "messages_unacknowledged": 0,
                            },
                        }
                    ],
                    "zero_consumer_queues": [],
                    "unavailable_queues": [],
                }
            services[stage] = {
                "readiness": {
                    "status": "healthy",
                    "http_status": 200,
                    "body": json.dumps({"status": "ready", "checkedAt": "2026-01-01T00:00:00Z"}),
                },
                "consumer_readiness": consumer,
            }
        return {
            "schema_version": 1,
            "status": "pass",
            "action": "status",
            "generated_at_utc": projection.iso_utc(self.observed),
            "mode": "shadow",
            "production_writes_enabled": False,
            "services": services,
            "missing_consumers": [],
            "unverifiable_consumers": [],
            "errors": [],
        }

    def manifest_value(self):
        services = []
        for index, stage in enumerate(STAGES):
            main_queue = f"nutsnews.worker.{stage}.v1"
            services.append(
                {
                    "name": stage,
                    "stage": stage,
                    "image": f"ghcr.io/ramideltoro/nutsnews-worker-{stage}@sha256:{index + 1:064x}",
                    "queues": {
                        "main": main_queue,
                        "consumes": [] if stage == "scheduler" else [main_queue],
                        "retry": [f"{main_queue}.retry.1", f"{main_queue}.retry.2"],
                        "dlq": f"{main_queue}.dlq",
                    },
                }
            )
        return {
            "schema_version": 1,
            "mode": "shadow",
            "production_writes_enabled": False,
            "services": services,
        }

    def queue_report(self, stage: str, kind: str, names: list[str], consumers: int = 1):
        return {
            "status": "pass",
            "action": "queue-inspect",
            "service_name": stage,
            "queues": [
                {
                    "status": "healthy",
                    "queue": name,
                    "metrics": {
                        "consumers": consumers if kind == "main" else 0,
                        "messages": 0,
                        "messages_ready": 0,
                        "messages_unacknowledged": 0,
                    },
                }
                for name in names
            ],
        }

    def write(self):
        self.runtime_path.write_text(json.dumps(self.runtime), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.compose_path.write_text("services: {}\n", encoding="utf-8")
        mapped = {item["name"]: item for item in self.manifest["services"]}
        for stage in STAGES:
            service = mapped[stage]
            for kind in ("main", "retry", "dlq"):
                names = projection.allowed_queue_names(service, kind)
                report = self.queue_report(stage, kind, names)
                (self.queue_dir / f"{stage}-{kind}.json").write_text(json.dumps(report), encoding="utf-8")

    def build(self):
        return projection.build_candidate(
            json.loads(CONTRACT.read_text(encoding="utf-8")),
            self.runtime_path,
            self.manifest_path,
            self.compose_path,
            self.queue_dir,
            now=self.observed + timedelta(seconds=5),
        )


class WorkerUpliftStageHealthProjectionTests(unittest.TestCase):
    def test_committed_contract_workflow_and_implementation_validate(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(validator.validate_contract(contract), [])
        self.assertEqual(validator.validate_workflow(WORKFLOW.read_text(encoding="utf-8")), [])
        self.assertEqual(validator.validate_implementation(IMPLEMENTATION.read_text(encoding="utf-8")), [])

    def test_builds_exact_eight_row_current_shadow_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            artifact = fixture.build()
        self.assertEqual(artifact["status"], "pass")
        self.assertEqual(artifact["candidate"]["row_count"], 8)
        self.assertEqual([row["stage_name"] for row in artifact["candidate"]["rows"]], list(STAGES))
        self.assertEqual(artifact["safety"]["runtime_mode"], "shadow")
        self.assertFalse(artifact["safety"]["production_writes_enabled"])
        self.assertEqual(artifact["safety"]["active_ingestion_owner"], "legacy_shards")
        scheduler = artifact["candidate"]["rows"][0]
        self.assertIsNone(scheduler["active_consumers"])
        self.assertEqual(scheduler["queue_age_seconds"], 0)
        for row in artifact["candidate"]["rows"][1:]:
            self.assertEqual(row["active_consumers"], 1)
            self.assertEqual(row["retry_count"], 0)
            self.assertEqual(row["dlq_count"], 0)
            self.assertEqual(row["stage_status"], "healthy")
            self.assertEqual(row["stale_status"], "current")

    def test_candidate_is_deterministic_and_idempotent_for_same_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            first = fixture.build()
            second = fixture.build()
        self.assertEqual(first, second)
        self.assertEqual(projection.sha256_json(first), projection.sha256_json(second))

    def test_zero_consumer_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            path = fixture.queue_dir / "fetcher-main.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["queues"][0]["metrics"]["consumers"] = 0
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(projection.ProjectionError, "zero consumers|disagrees"):
                fixture.build()

    def test_stale_runtime_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(projection.ProjectionError, "freshness window"):
                projection.build_candidate(
                    contract,
                    fixture.runtime_path,
                    fixture.manifest_path,
                    fixture.compose_path,
                    fixture.queue_dir,
                    now=fixture.observed + timedelta(seconds=901),
                )

    def test_mutable_image_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            fixture.manifest["services"][2]["image"] = "ghcr.io/ramideltoro/nutsnews-worker-canonicalizer:latest"
            fixture.write()
            with self.assertRaisesRegex(projection.ProjectionError, "immutable GHCR digest"):
                fixture.build()

    def test_redaction_rejects_secret_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            artifact = fixture.build()
        artifact["candidate"]["rows"][0]["diagnostic_metadata"]["password"] = "not-a-real-value"
        with self.assertRaisesRegex(projection.ProjectionError, "credential or private-value"):
            projection.ensure_value_free(artifact, "test artifact")

    def test_candidate_validator_rejects_stage_or_column_expansion(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            artifact = fixture.build()
        artifact["candidate"]["rows"][0]["unexpected"] = True
        with self.assertRaisesRegex(projection.ProjectionError, "columns"):
            projection.validate_candidate_artifact(artifact, contract)

    def test_stale_guard_is_enforced_in_fixed_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            rows = fixture.build()["candidate"]["rows"]
        sql = projection.apply_sql(rows)
        self.assertEqual(sql.count(f"insert into {projection.TARGET}"), 1)
        self.assertIn(f"where {projection.TARGET}.updated_at <= excluded.updated_at", sql)
        self.assertIn("1 / case when count(*) = 8 then 1 else 0 end", sql)
        self.assertNotRegex(sql.lower(), r"\bdelete\s+from\b|\btruncate\b|\balter\s+table\b|\bdrop\s+")

    def test_privilege_proof_denies_other_mutation_grants(self):
        proof = {
            "current_user": projection.PROJECTION_ROLE,
            "target_select": True,
            "target_insert": True,
            "target_update": True,
            "target_delete": False,
            "target_truncate": False,
            "target_trigger": False,
            "target_references": False,
            "sequence_usage": True,
            "database_create": False,
            "role_create": False,
            "superuser": False,
            "schema_create_grants": [],
            "other_mutation_grants": [],
        }
        projection.validate_privilege_proof(proof)
        proof["other_mutation_grants"] = [{"schema": "public", "table": "articles", "privilege": "UPDATE"}]
        with self.assertRaisesRegex(projection.ProjectionError, "another table mutation"):
            projection.validate_privilege_proof(proof)

    def test_apply_is_bound_to_exact_candidate_and_eight_rows(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ProjectionFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            privilege = {
                "current_user": projection.PROJECTION_ROLE,
                "target_select": True,
                "target_insert": True,
                "target_update": True,
                "target_delete": False,
                "target_truncate": False,
                "target_trigger": False,
                "target_references": False,
                "sequence_usage": True,
                "database_create": False,
                "role_create": False,
                "superuser": False,
                "schema_create_grants": [],
                "other_mutation_grants": [],
            }
            schema = {"columns": ["fixed"], "constraints": ["fixed"], "indexes": ["fixed"]}
            rows_before = {"row_count": 0, "rows": []}
            mutation = {"upserted_rows": 8, "expected_rows": 8, "exact_row_guard": 1}
            rows_after = {"row_count": 8, "rows": projection.expected_db_rows(artifact)}
            with mock.patch.object(
                projection,
                "utc_now",
                return_value=fixture.observed + timedelta(seconds=5),
            ):
                with mock.patch.object(
                    projection,
                    "psql_json",
                    side_effect=[privilege, schema, rows_before, mutation, schema, rows_after],
                ):
                    report = projection.apply_candidate(
                        contract,
                        artifact_path,
                        "fixture-password",
                        projection.APPLY_CONFIRMATION,
                    )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["mutation"]["upserted_rows"], 8)
        self.assertTrue(report["schema_fingerprint"]["unchanged"])
        self.assertEqual(report["database_evidence"]["rows"], projection.expected_db_rows(artifact))

    def test_wrong_confirmation_blocks_before_database_access(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            artifact_path = Path(tmp) / "candidate.json"
            artifact_path.write_text(json.dumps(fixture.build()), encoding="utf-8")
            with mock.patch.object(projection, "psql_json") as psql:
                with self.assertRaisesRegex(projection.ProjectionError, "exact typed confirmation"):
                    projection.apply_candidate(contract, artifact_path, "fixture-password", "wrong")
                psql.assert_not_called()

    def test_stale_candidate_blocks_before_database_access(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ProjectionFixture(Path(tmp))
            artifact = fixture.build()
            stale_observation = projection.iso_utc(projection.utc_now() - timedelta(seconds=901))
            artifact["observed_at_utc"] = stale_observation
            artifact["generated_at_utc"] = stale_observation
            for row in artifact["candidate"]["rows"]:
                row["updated_at"] = stale_observation
            artifact_path = Path(tmp) / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

            with mock.patch.object(projection, "psql_json") as psql:
                with self.assertRaisesRegex(projection.ProjectionError, "authorized freshness window"):
                    projection.apply_candidate(
                        contract,
                        artifact_path,
                        "fixture-password",
                        projection.APPLY_CONFIRMATION,
                    )
                psql.assert_not_called()

    def test_post_apply_proof_binds_database_and_unchanged_runtime_state(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ProjectionFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            apply_report = {
                "status": "applied",
                "workflow": {"run_id": "fixture", "commit": "fixture"},
                "candidate_artifact_sha256": projection.sha256_file(artifact_path),
                "database_evidence": {
                    "row_count": 8,
                    "rows": projection.expected_db_rows(artifact),
                },
                "schema_fingerprint": {"unchanged": True},
            }
            apply_report_path = root / "apply-report.json"
            apply_report_path.write_text(json.dumps(apply_report), encoding="utf-8")

            with mock.patch.object(projection, "build_candidate", return_value=artifact):
                proof = projection.verify_post_apply(
                    contract,
                    artifact_path,
                    apply_report_path,
                    fixture.runtime_path,
                    fixture.manifest_path,
                    fixture.compose_path,
                    fixture.queue_dir,
                )

        self.assertEqual(proof["status"], "pass")
        self.assertTrue(proof["proof"]["target_rows_match_candidate"])
        self.assertTrue(proof["proof"]["consumer_counts_unchanged"])
        self.assertFalse(proof["guardrails"]["article_or_domain_write_performed"])

    def test_post_apply_proof_rejects_queue_or_consumer_drift(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ProjectionFixture(root)
            artifact = fixture.build()
            artifact_path = root / "candidate.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            apply_report = {
                "status": "applied",
                "candidate_artifact_sha256": projection.sha256_file(artifact_path),
                "database_evidence": {
                    "row_count": 8,
                    "rows": projection.expected_db_rows(artifact),
                },
                "schema_fingerprint": {"unchanged": True},
            }
            apply_report_path = root / "apply-report.json"
            apply_report_path.write_text(json.dumps(apply_report), encoding="utf-8")
            changed = copy.deepcopy(artifact)
            changed["candidate"]["rows"][1]["active_consumers"] = 2

            with mock.patch.object(projection, "build_candidate", return_value=changed):
                with self.assertRaisesRegex(projection.ProjectionError, "state changed"):
                    projection.verify_post_apply(
                        contract,
                        artifact_path,
                        apply_report_path,
                        fixture.runtime_path,
                        fixture.manifest_path,
                        fixture.compose_path,
                        fixture.queue_dir,
                    )

    def test_workflow_scope_expansion_fails_validator(self):
        workflow = WORKFLOW.read_text(encoding="utf-8") + "\n# CLOUDFLARE_API_TOKEN\n"
        errors = validator.validate_workflow(workflow)
        self.assertTrue(any("forbidden mutation capability" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
