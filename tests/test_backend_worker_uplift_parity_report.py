#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_worker_uplift_parity_report


def successful_smoke() -> dict:
    return {
        "status": "pass",
        "generated_at_utc": "2026-07-26T12:46:02Z",
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
                    "publication": {"publication_readiness": "1", "publication_shadow_comparison": "1"},
                    "api_audit": {"failed_api_requests": "0"},
                },
                "health": {
                    stage: {"status": "healthy"}
                    for stage in ("fetcher", "canonicalizer", "enrichment", *backend_worker_uplift_parity_report.STAGES[3:])
                },
                "idempotency": {
                    "expected_single_final_shadow_result": "1",
                    "duplicate_publish_idempotency_key": "smoke:pipeline:feed:pipeline-test",
            },
            "versions": {"fetcher": {"image": "ghcr.io/example/fetcher@sha256:" + "a" * 64}},
            "queues_after": {
                stage: [{"metrics": {"consumers": 1}}]
                for stage in backend_worker_uplift_parity_report.STAGES
            },
        },
    }


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
                                "--enforce",
                            ]
                        )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["smoke"]["queue_consumers_after"]["approval"], 1)
        self.assertEqual(report["failed_checks"], [])

    def test_live_report_fails_when_smoke_had_dlq_growth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke = successful_smoke()
            smoke["smoke"]["dlq_growth"] = {"nutsnews.worker.translation.v1.dlq": 1}
            smoke_path = Path(tmpdir) / "smoke.json"
            smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
            with mock.patch.dict("os.environ", {"DB": "postgresql://example"}, clear=True):
                with mock.patch.object(backend_worker_uplift_parity_report, "run_psql", side_effect=query_output):
                    with redirect_stdout(StringIO()) as stdout:
                        exit_code = backend_worker_uplift_parity_report.main(
                            ["--db-url-env", "DB", "--smoke-report", str(smoke_path), "--enforce"]
                        )
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn("queue_retry_dlq_versions", report["failed_checks"])


if __name__ == "__main__":
    unittest.main()
