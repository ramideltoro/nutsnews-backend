#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_worker_uplift_parity_report


def successful_smoke() -> dict:
    decision = json.loads(
        backend_worker_uplift_parity_report.DEFAULT_READINESS_DECISION.read_text(
            encoding="utf-8"
        )
    )
    return {
        "status": "pass",
        "generated_at_utc": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "smoke": {
            "contract": "scheduler-feed-to-final-shadow-v1",
            "trigger": "scheduler-compatible-feed-fetch-request",
            "fixture": {"fixture_id": "pipeline-test"},
            "fixture_hits": {"feed": 1, "article": 1},
            "missing_consumers": [],
            "dlq_growth": {},
            "legacy_ingestion_endpoints_invoked": False,
            "db_checks": {
                "approval": {"processed_inbox": "1"},
                "translation": {"processed_inbox": "1", "distinct_languages": "5"},
                "persistence": {"final_shadow_aggregate": "1"},
                "publication": {
                    "publication_readiness": "1",
                    "publication_shadow_comparison": "1",
                },
                "api_audit": {"failed_api_requests": "0"},
            },
            "health": {
                stage: {"status": "healthy"}
                for stage in (
                    "fetcher",
                    "canonicalizer",
                    "enrichment",
                    *backend_worker_uplift_parity_report.STAGES[3:],
                )
            },
            "idempotency": {
                "expected_single_final_shadow_result": "1",
                "duplicate_publish_idempotency_key": "smoke:pipeline:feed:pipeline-test",
            },
            "versions": {
                item["stage"]: {
                    "image": item["image"],
                    "contract_version": item["contract_version"],
                    "runtime_package_version": item["runtime_package_version"],
                    "runtime_mode": item["mode"],
                }
                for item in decision["deployed_services"]
            },
            "queues_after": {
                stage: [{"metrics": {"consumers": 1}}]
                for stage in backend_worker_uplift_parity_report.STAGES
            },
        },
    }


def write_candidate_files(root: Path) -> tuple[Path, Path]:
    decision = json.loads(
        backend_worker_uplift_parity_report.DEFAULT_READINESS_DECISION.read_text(
            encoding="utf-8"
        )
    )
    runtime_manifest = {
        "mode": "shadow",
        "production_writes_enabled": False,
        "backend_api": {"writes_enabled": False},
        "services": [
            {
                "name": item["stage"],
                "stage": item["stage"],
                "image": item["image"],
                "image_tag": item["source_commit"],
                "contract_version": item["contract_version"],
                "runtime_package_version": item["runtime_package_version"],
                "runtime_mode": item["mode"],
                "provenance": {
                    "source_repository": item["source_repository"],
                },
                "postgres": {"production_write_path": False},
            }
            for item in decision["deployed_services"]
        ],
    }
    runtime_manifest_path = root / "runtime-manifest.json"
    runtime_compose_path = root / "runtime-compose.yml"
    runtime_manifest_path.write_text(
        json.dumps(runtime_manifest), encoding="utf-8"
    )
    runtime_compose_path.write_text("services: {}\n", encoding="utf-8")
    return runtime_manifest_path, runtime_compose_path


def candidate_args(root: Path) -> list[str]:
    runtime_manifest, runtime_compose = write_candidate_files(root)
    return [
        "--runtime-manifest",
        str(runtime_manifest),
        "--runtime-compose",
        str(runtime_compose),
    ]


def query_output(_db_url: str, query: str) -> tuple[str, None]:
    if "worker_uplift_final.article_shadow_aggregates" in query:
        return (
            "\n".join(
                [
                    "final_shadow_aggregates=1",
                    "ready_final_shadow_aggregates=1",
                    "api_shadow_receipts=1",
                    "failed_api_receipts=0",
                    "stage_health_rows=7",
                    "active_ingestion_owner_legacy_shards=7",
                    "active_ingestion_owner_worker_uplift=0",
                ]
            ),
            None,
        )
    if "worker_uplift_translation.translation_records" in query:
        return (
            "\n".join(
                [
                    "approval_approved=1",
                    "approval_rejected=0",
                    "translation_accepted=5",
                    "translation_distinct_languages=5",
                    "publication_ready=1",
                    "publication_shadow_comparisons=1",
                ]
            ),
            None,
        )
    if "worker_uplift_fetcher.fetch_versions" in query:
        return (
            "\n".join(
                [
                    "fetch_versions=0",
                    "feed_health_projections=0",
                    "article_identities=0",
                    "enrichment_records=0",
                ]
            ),
            None,
        )
    values: list[str] = []
    for stage in backend_worker_uplift_parity_report.STAGES:
        values.extend(
            [
                f"{stage}_processed_inbox={0 if stage in ('fetcher', 'canonicalizer', 'enrichment') else 1}",
                f"{stage}_failed_inbox={1 if stage in ('translation', 'persistence') else 0}",
                f"{stage}_pending_outbox=0",
                f"{stage}_confirmed_outbox=1",
            ]
        )
    return "\n".join(values), None


class WorkerUpliftParityReportTests(unittest.TestCase):
    def test_offline_report_is_non_mutating_and_safe(self):
        with redirect_stdout(StringIO()) as stdout:
            exit_code = backend_worker_uplift_parity_report.main(["--offline", "--enforce"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(report["safe_metadata_only"])
        self.assertFalse(report["writes_performed"])
        self.assertFalse(report["production_cutover_authorized"])

    def test_live_report_passes_with_aggregate_db_and_smoke_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke_path = Path(tmpdir) / "smoke.json"
            output = Path(tmpdir) / "report.json"
            smoke_path.write_text(json.dumps(successful_smoke()), encoding="utf-8")
            current_candidate_args = candidate_args(Path(tmpdir))
            with mock.patch.dict("os.environ", {"DB": "postgresql://example"}, clear=True):
                with mock.patch.object(backend_worker_uplift_parity_report, "run_psql", side_effect=query_output):
                    with redirect_stdout(StringIO()):
                        exit_code = backend_worker_uplift_parity_report.main(
                            [
                                "--db-url-env",
                                "DB",
                                "--smoke-report",
                                str(smoke_path),
                                "--output",
                                str(output),
                                *current_candidate_args,
                                "--enforce",
                            ]
                        )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["smoke"]["queue_consumers_after"]["approval"], 1)
        self.assertEqual(report["failed_checks"], [])
        self.assertEqual(len(report["current_candidate"]["services"]), 8)
        self.assertEqual(len(report["current_candidate"]["immutable_packages"]), 4)
        self.assertEqual(
            report["current_candidate"]["single_writer"]["owner"],
            "legacy_shards",
        )
        self.assertFalse(
            report["current_candidate"]["single_writer"][
                "production_writes_enabled"
            ]
        )
        self.assertIn(
            "deployed_runtime_manifest",
            report["current_candidate"]["configuration_hashes"],
        )
        self.assertTrue(report["comparison_results"]["error_budget"]["within_budget"])
        self.assertFalse(
            report["comparison_results"]["guardrails"]["writes_performed"]
        )

    def test_live_report_fails_when_smoke_had_dlq_growth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke = successful_smoke()
            smoke["smoke"]["dlq_growth"] = {"nutsnews.worker.translation.v1.dlq": 1}
            smoke_path = Path(tmpdir) / "smoke.json"
            smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
            current_candidate_args = candidate_args(Path(tmpdir))
            with mock.patch.dict("os.environ", {"DB": "postgresql://example"}, clear=True):
                with mock.patch.object(backend_worker_uplift_parity_report, "run_psql", side_effect=query_output):
                    with redirect_stdout(StringIO()) as stdout:
                        exit_code = backend_worker_uplift_parity_report.main(
                            [
                                "--db-url-env",
                                "DB",
                                "--smoke-report",
                                str(smoke_path),
                                *current_candidate_args,
                                "--enforce",
                            ]
                        )
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn("queue_retry_dlq_versions", report["failed_checks"])

    def test_live_report_fails_when_smoke_candidate_image_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke = successful_smoke()
            smoke["smoke"]["versions"]["scheduler"]["image"] = (
                "ghcr.io/ramideltoro/nutsnews-worker-feed-scheduler@sha256:"
                + "0" * 64
            )
            smoke_path = Path(tmpdir) / "smoke.json"
            smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
            current_candidate_args = candidate_args(Path(tmpdir))
            with mock.patch.dict("os.environ", {"DB": "postgresql://example"}, clear=True):
                with mock.patch.object(
                    backend_worker_uplift_parity_report,
                    "run_psql",
                    side_effect=query_output,
                ):
                    with redirect_stdout(StringIO()) as stdout:
                        exit_code = backend_worker_uplift_parity_report.main(
                            [
                                "--db-url-env",
                                "DB",
                                "--smoke-report",
                                str(smoke_path),
                                *current_candidate_args,
                                "--enforce",
                            ]
                        )
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["current_candidate"]["status"], "fail")
        self.assertIn(
            "scheduler.image_mismatch",
            report["current_candidate"]["mismatches"],
        )
        self.assertIn("current_candidate_identity", report["failed_checks"])

    def test_live_report_fails_when_smoke_window_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke = successful_smoke()
            smoke["generated_at_utc"] = (
                datetime.now(UTC) - timedelta(days=2)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            smoke_path = Path(tmpdir) / "smoke.json"
            smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
            current_candidate_args = candidate_args(Path(tmpdir))
            with mock.patch.dict("os.environ", {"DB": "postgresql://example"}, clear=True):
                with mock.patch.object(
                    backend_worker_uplift_parity_report,
                    "run_psql",
                    side_effect=query_output,
                ):
                    with redirect_stdout(StringIO()) as stdout:
                        exit_code = backend_worker_uplift_parity_report.main(
                            [
                                "--db-url-env",
                                "DB",
                                "--smoke-report",
                                str(smoke_path),
                                *current_candidate_args,
                                "--enforce",
                            ]
                        )
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(report["current_candidate"]["comparison_window"]["fresh"])
        self.assertIn(
            "smoke_window_not_fresh",
            report["current_candidate"]["mismatches"],
        )


if __name__ == "__main__":
    unittest.main()
