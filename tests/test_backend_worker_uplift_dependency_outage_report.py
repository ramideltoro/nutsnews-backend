from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from scripts import backend_worker_uplift_dependency_outage_report as report_module
from scripts import validate_worker_uplift_dependency_outage_drills as validator


ROOT = Path(__file__).resolve().parents[1]


class WorkerUpliftDependencyOutageReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.candidates = report_module.parse_runtime_candidates(
            ROOT / "ansible/roles/backend_worker_runtime/defaults/main.yml"
        )
        self.candidate_by_stage = {
            item["service"]: item for item in self.candidates
        }
        self.files: dict[str, Path] = {}
        self._write("pre", self._status())
        self._write("post", self._status())
        self._write(
            "approval_results",
            self._vitest(
                [
                    "createApprovalService starts, becomes ready, registers approval and translation routes, and drains cleanly",
                    "approval accepts valid Qwen decisions, records traceable metadata, and publishes one translation task",
                    "approval retries Qwen timeouts without recording a decision",
                    "approval DLQs repeated transient Qwen failures after retry attempts are exhausted",
                ]
            ),
        )
        self._write(
            "persistence_results",
            self._vitest(
                [
                    "createPersistenceService starts, becomes ready, registers persistence and publication routes, and drains cleanly",
                    "persistence materializes valid persistence deliveries and acks duplicate replays without duplicate side effects",
                    "persistence rolls back local final-shadow writes after backend API failure and remains recoverable",
                    "persistence rolls back local final-shadow transaction failures and recovers through backend idempotency",
                    "persistence sends permanent backend/database faults to DLQ without local accepted output",
                ]
            ),
        )
        for key, scenario, adapter in (
            ("postgres_probe", "postgresql_unavailable", "PostgresPersistenceInboxStore"),
            ("backend_api_probe", "backend_api_unavailable", "HttpPersistenceBackendWorkerApiClient"),
            ("qwen_probe", "qwen_unavailable", "LocalAiApprovalQwenClient"),
        ):
            self._write(
                key,
                {
                    "schema_version": 1,
                    "scenario": scenario,
                    "adapter": adapter,
                    "status": "pass",
                    "failure_detected": True,
                    "detection_ms": 2,
                    "recovered": True,
                    "recovery_ms": 3,
                    "endpoint_recorded": False,
                    "credential_value_recorded": False,
                },
            )
        self._write(
            "delivery_policy",
            {
                "schema_version": 1,
                "max_attempts": 4,
                "manual_ack_required": True,
                "retry_transfer_requires_confirm_before_ack": True,
                "dlq_transfer_requires_confirm_before_ack": True,
                "poison_message_convention": "confirm-before-ack",
            },
        )
        self._write(
            "source_checkouts",
            {
                "approval": self.candidate_by_stage["approval"]["source_commit"],
                "persistence": self.candidate_by_stage["persistence"]["source_commit"],
                "infra": "b10a76cf523c9f5b47bd69b8301dc3d9d9a4d8a6",
            },
        )
        self._write(
            "alerts",
            {
                "alerts": [
                    {
                        "uid": "nn-wu-rmq-dlq-nonempty",
                        "title": "DLQ",
                        "expr": "sum(dlq)",
                        "for": "2m",
                        "test_drill": "poison-message",
                    },
                    {
                        "uid": "nn-wu-rmq-retry-redelivery",
                        "title": "Retry",
                        "expr": "sum(retry)",
                        "for": "10m",
                        "test_drill": "poison-message",
                    },
                    {
                        "uid": "nn-wu-slo-retry-dlq-burn",
                        "title": "Burn",
                        "expr": "sum(retry + dlq)",
                        "for": "5m",
                        "test_drill": "poison-message",
                    },
                ]
            },
        )
        self.service_source = self.directory / "service.ts"
        self.service_source.write_text(
            'telemetry.emit({ name: "runtime.dependency.observed" });\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, name: str, value: dict) -> Path:
        path = self.directory / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.files[name] = path
        return path

    @staticmethod
    def _vitest(names: list[str]) -> dict:
        return {
            "numFailedTests": 0,
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "fullName": name,
                            "status": "passed",
                            "duration": 1,
                        }
                        for name in names
                    ]
                }
            ],
        }

    def _status(self) -> dict:
        command_lines = []
        services = {}
        required_checks = {
            "approval": ["approval-state", "qwen-client"],
            "persistence": ["persistence-inbox", "backend-worker-api"],
        }
        for index, candidate in enumerate(self.candidates):
            stage = candidate["service"]
            command_lines.append(
                json.dumps(
                    {
                        "Service": stage,
                        "Image": candidate["image"],
                        "Labels": (
                            f"org.opencontainers.image.revision={candidate['source_commit']},"
                            f"com.docker.compose.config-hash={index:064x}"
                        ),
                    }
                )
            )
            checks = [
                {"name": name, "status": "ok"}
                for name in required_checks.get(stage, [])
            ]
            consumer = (
                {"status": "not_applicable", "queues": []}
                if stage == "scheduler"
                else {
                    "status": "healthy",
                    "queues": [
                        {
                            "queue": f"nutsnews.worker.{stage}.v1",
                            "metrics": {
                                "consumers": 1,
                                "messages": 0,
                                "messages_ready": 0,
                                "messages_unacknowledged": 0,
                            },
                        }
                    ],
                }
            )
            services[stage] = {
                "readiness": {
                    "status": "healthy",
                    "body": json.dumps({"checks": checks}),
                },
                "consumer_readiness": consumer,
            }
        return {
            "status": "pass",
            "mode": "shadow",
            "production_writes_enabled": False,
            "generated_at_utc": "2026-07-31T00:00:00Z",
            "missing_consumers": [],
            "unverifiable_consumers": [],
            "services": services,
            "commands": [{"stdout": "\n".join(command_lines)}],
        }

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            runtime_defaults=ROOT
            / "ansible/roles/backend_worker_runtime/defaults/main.yml",
            topology=ROOT
            / "ansible/roles/backend_rabbitmq/templates/worker-uplift-topology.json.j2",
            pre_status=self.files["pre"],
            post_status=self.files["post"],
            approval_results=self.files["approval_results"],
            persistence_results=self.files["persistence_results"],
            postgres_probe=self.files["postgres_probe"],
            backend_api_probe=self.files["backend_api_probe"],
            qwen_probe=self.files["qwen_probe"],
            delivery_policy=self.files["delivery_policy"],
            source_checkouts=self.files["source_checkouts"],
            approval_service_source=self.service_source,
            persistence_service_source=self.service_source,
            alert_catalog=self.files["alerts"],
            infra_commit="b10a76cf523c9f5b47bd69b8301dc3d9d9a4d8a6",
            workflow_run_id=12345,
            workflow_commit="1" * 40,
            output=self.directory / "report.json",
        )

    def test_current_candidate_report_passes_full_validator(self) -> None:
        report = report_module.build_report(self._args())

        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(validator.report_errors(report), [])

    def test_deployed_image_drift_fails_report(self) -> None:
        status = self._status()
        lines = status["commands"][0]["stdout"].splitlines()
        first = json.loads(lines[0])
        first["Image"] = first["Image"].replace("sha256:", "sha256:f")
        lines[0] = json.dumps(first)
        status["commands"][0]["stdout"] = "\n".join(lines)
        self._write("pre", status)

        report = report_module.build_report(self._args())

        self.assertEqual(report["status"], "fail")
        self.assertTrue(
            any("deployed image mismatch" in item for item in report["errors"])
        )

    def test_missing_retry_assertion_fails_report(self) -> None:
        results = json.loads(
            self.files["approval_results"].read_text(encoding="utf-8")
        )
        results["testResults"][0]["assertionResults"] = results["testResults"][0][
            "assertionResults"
        ][:-1]
        self._write("approval_results", results)

        report = report_module.build_report(self._args())

        self.assertEqual(report["status"], "fail")
        self.assertTrue(
            any("required focused assertion" in item for item in report["errors"])
        )

    def test_raw_host_command_data_is_rejected_from_final_report(self) -> None:
        report = report_module.build_report(self._args())
        report["commands"] = [{"stdout": "private"}]

        errors = validator.report_errors(report)

        self.assertTrue(
            any("forbidden private/value-bearing keys" in item for item in errors)
        )

    def test_repository_contract_is_valid(self) -> None:
        self.assertEqual(validator.repository_errors(), [])


if __name__ == "__main__":
    unittest.main()
