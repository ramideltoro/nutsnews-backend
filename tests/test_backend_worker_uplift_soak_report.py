#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backend_worker_uplift_soak_report


def successful_smoke() -> dict:
    return {
        "status": "pass",
        "generated_at_utc": "2026-07-26T14:00:00Z",
        "smoke": {
            "contract": "scheduler-feed-to-final-shadow-v1",
            "trigger": "scheduler-compatible-feed-fetch-request",
            "missing_consumers": [],
            "dlq_growth": {},
            "legacy_ingestion_endpoints_invoked": False,
            "guardrails": {
                stage: {"production_writes_enabled": False}
                for stage in backend_worker_uplift_soak_report.WORKER_STAGES
            },
            "queues_after": {
                stage: [
                    {
                        "queue": f"nutsnews.worker.{stage}.v1",
                        "status": "healthy",
                        "metrics": {
                            "messages": 0,
                            "messages_ready": 0,
                            "messages_unacknowledged": 0,
                            "consumers": 1,
                        },
                    }
                ]
                for stage in backend_worker_uplift_soak_report.WORKER_STAGES
            },
            "db_checks": {
                "api_audit": {"failed_api_requests": "0"},
            },
            "idempotency": {
                "expected_single_final_shadow_result": "1",
                "duplicate_publish_idempotency_key": "smoke:pipeline:feed:fixture",
            },
        },
    }


def successful_runtime_status() -> dict:
    stdout = "\n".join(
        json.dumps(
            {
                "Service": stage,
                "Name": f"nutsnews-worker-uplift-{stage}-1",
                "State": "running",
                "Health": "healthy",
                "Image": f"ghcr.io/ramideltoro/nutsnews-worker-uplift/{stage}@sha256:" + "a" * 64,
            }
        )
        for stage in backend_worker_uplift_soak_report.STAGES
    )
    return {
        "status": "pass",
        "generated_at_utc": "2026-07-26T14:00:00Z",
        "mode": "shadow",
        "production_writes_enabled": False,
        "commands": [{"returncode": 0, "stdout": stdout, "stderr": ""}],
    }


def healthy_host_snapshot() -> dict:
    return {
        "collected_at_utc": "2026-07-26T14:00:00Z",
        "host_metrics_available": True,
        "systemd_available": True,
        "load_1m": 0.5,
        "cpu_count": 2,
        "load_per_vcpu": 0.25,
        "memory_used_percent": 42.0,
        "root_disk_used_percent": 35.0,
        "failed_systemd_units": [],
        "service_states": {
            "docker": "active",
            "rabbitmq-server": "inactive",
            "alloy": "active",
            "caddy": "active",
        },
    }


def query_output(window_hours: str = "50", openai_records: str = "0"):
    def _query_output(_db_url: str, query: str) -> tuple[str, None]:
        if "observed_event_count" in query:
            return (
                "\n".join(
                    [
                        "observed_event_count=42",
                        "observed_window_start_utc=2026-07-24T12:00:00Z",
                        "observed_window_end_utc=2026-07-26T14:00:00Z",
                        f"observed_window_hours={window_hours}",
                    ]
                ),
                None,
            )
        if "api_primary_receipts" in query:
            return (
                "\n".join(
                    [
                        "final_shadow_aggregates=1",
                        "ready_final_shadow_aggregates=1",
                        "published_final_shadow_aggregates=0",
                        "api_shadow_receipts=1",
                        "api_primary_receipts=0",
                        "failed_api_receipts=0",
                        "non_shadow_api_receipts=0",
                        "stage_health_rows=8",
                        "active_ingestion_owner_legacy_shards=8",
                        "active_ingestion_owner_worker_uplift=0",
                        "stage_health_retry_count=0",
                        "stage_health_dlq_count=0",
                        "stage_health_max_queue_age_seconds=0",
                        "failed_persistence_write_requests=0",
                        "retrying_persistence_write_requests=0",
                    ]
                ),
                None,
            )
        if "approval_local_ai_records" in query:
            return (
                "\n".join(
                    [
                        "approval_local_ai_records=1",
                        f"approval_openai_records={openai_records}",
                        "approval_other_provider_records=0",
                        "approval_null_provider_records=0",
                        "translation_local_ai_records=5",
                        "translation_openai_records=0",
                        "translation_other_provider_records=0",
                        "translation_null_provider_records=0",
                        "approval_qwen_model_records=1",
                        "translation_qwen_model_records=5",
                    ]
                ),
                None,
            )
        values: list[str] = []
        for stage in backend_worker_uplift_soak_report.STAGES:
            values.extend(
                [
                    f"{stage}_inbox_received=0",
                    f"{stage}_inbox_processing=0",
                    f"{stage}_inbox_failed_or_parked=0",
                    f"{stage}_outbox_pending=0",
                    f"{stage}_outbox_retrying=0",
                    f"{stage}_outbox_dead_lettered=0",
                    f"{stage}_oldest_unconfirmed_outbox_age_seconds=0",
                    f"{stage}_attempts_failed=0",
                    f"{stage}_attempts_retry_scheduled=0",
                    f"{stage}_attempts_dead_lettered=0",
                ]
            )
        return "\n".join(values), None

    return _query_output


class WorkerUpliftSoakReportTests(unittest.TestCase):
    def test_offline_report_is_non_mutating_and_safe(self):
        with redirect_stdout(StringIO()) as stdout:
            exit_code = backend_worker_uplift_soak_report.main(["--offline", "--enforce"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(report["safe_metadata_only"])
        self.assertFalse(report["writes_performed"])
        self.assertFalse(report["production_cutover_authorized"])

    def test_live_report_passes_with_complete_window_and_safe_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke_path = Path(tmpdir) / "smoke.json"
            runtime_path = Path(tmpdir) / "runtime.json"
            output = Path(tmpdir) / "report.json"
            smoke_path.write_text(json.dumps(successful_smoke()), encoding="utf-8")
            runtime_path.write_text(json.dumps(successful_runtime_status()), encoding="utf-8")
            with mock.patch.dict("os.environ", {"DB": "db-url"}, clear=True):
                with mock.patch.object(backend_worker_uplift_soak_report, "run_psql", side_effect=query_output()):
                    with mock.patch.object(backend_worker_uplift_soak_report, "collect_host_snapshot", return_value=healthy_host_snapshot()):
                        with redirect_stdout(StringIO()):
                            exit_code = backend_worker_uplift_soak_report.main(
                                [
                                    "--db-url-env",
                                    "DB",
                                    "--smoke-report",
                                    str(smoke_path),
                                    "--runtime-status-report",
                                    str(runtime_path),
                                    "--output",
                                    str(output),
                                    "--enforce",
                                    "--require-window",
                                ]
                            )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failed_checks"], [])
        self.assertEqual(report["insufficient_window_checks"], [])

    def test_live_report_records_incomplete_window_without_failing_initial_soak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke_path = Path(tmpdir) / "smoke.json"
            runtime_path = Path(tmpdir) / "runtime.json"
            smoke_path.write_text(json.dumps(successful_smoke()), encoding="utf-8")
            runtime_path.write_text(json.dumps(successful_runtime_status()), encoding="utf-8")
            with mock.patch.dict("os.environ", {"DB": "db-url"}, clear=True):
                with mock.patch.object(backend_worker_uplift_soak_report, "run_psql", side_effect=query_output(window_hours="2")):
                    with mock.patch.object(backend_worker_uplift_soak_report, "collect_host_snapshot", return_value=healthy_host_snapshot()):
                        with redirect_stdout(StringIO()) as stdout:
                            exit_code = backend_worker_uplift_soak_report.main(
                                [
                                    "--db-url-env",
                                    "DB",
                                    "--smoke-report",
                                    str(smoke_path),
                                    "--runtime-status-report",
                                    str(runtime_path),
                                    "--enforce",
                                ]
                            )
            report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "insufficient_window")
        self.assertIn("observation_window", report["insufficient_window_checks"])

    def test_complete_window_requirement_fails_when_window_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke_path = Path(tmpdir) / "smoke.json"
            runtime_path = Path(tmpdir) / "runtime.json"
            smoke_path.write_text(json.dumps(successful_smoke()), encoding="utf-8")
            runtime_path.write_text(json.dumps(successful_runtime_status()), encoding="utf-8")
            with mock.patch.dict("os.environ", {"DB": "db-url"}, clear=True):
                with mock.patch.object(backend_worker_uplift_soak_report, "run_psql", side_effect=query_output(window_hours="2")):
                    with mock.patch.object(backend_worker_uplift_soak_report, "collect_host_snapshot", return_value=healthy_host_snapshot()):
                        with redirect_stdout(StringIO()):
                            exit_code = backend_worker_uplift_soak_report.main(
                                [
                                    "--db-url-env",
                                    "DB",
                                    "--smoke-report",
                                    str(smoke_path),
                                    "--runtime-status-report",
                                    str(runtime_path),
                                    "--enforce",
                                    "--require-window",
                                ]
                            )
        self.assertEqual(exit_code, 1)

    def test_live_report_fails_when_openai_records_are_observed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            smoke_path = Path(tmpdir) / "smoke.json"
            runtime_path = Path(tmpdir) / "runtime.json"
            smoke_path.write_text(json.dumps(successful_smoke()), encoding="utf-8")
            runtime_path.write_text(json.dumps(successful_runtime_status()), encoding="utf-8")
            with mock.patch.dict("os.environ", {"DB": "db-url"}, clear=True):
                with mock.patch.object(backend_worker_uplift_soak_report, "run_psql", side_effect=query_output(openai_records="1")):
                    with mock.patch.object(backend_worker_uplift_soak_report, "collect_host_snapshot", return_value=healthy_host_snapshot()):
                        with redirect_stdout(StringIO()) as stdout:
                            exit_code = backend_worker_uplift_soak_report.main(
                                [
                                    "--db-url-env",
                                    "DB",
                                    "--smoke-report",
                                    str(smoke_path),
                                    "--runtime-status-report",
                                    str(runtime_path),
                                    "--enforce",
                                ]
                            )
            report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn("ai_cost_and_qwen_saturation", report["failed_checks"])


if __name__ == "__main__":
    unittest.main()
