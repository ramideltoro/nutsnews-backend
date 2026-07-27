from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


MANAGER_PATH = Path("ansible/roles/backend_baseline/files/nutsnews_writer_pause.py")


def load_manager():
    spec = importlib.util.spec_from_file_location("nutsnews_writer_pause", MANAGER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def inventory() -> dict:
    return {
        "schema_version": 1,
        "gate_id": "backend-supabase-standby-writer-pause",
        "known_runtime_services": ["fetcher"],
        "writer_classes": [
            {"id": "backend_worker_database_api", "class": "api", "kind": "systemd_env_guard", "required": True},
            {"id": "worker_uplift_runtime_services", "class": "runtime", "kind": "docker_compose_scale_zero", "required": True},
            {"id": "backend_mutation_workflows", "class": "automation", "kind": "protected_automation_freeze", "required": True},
            {"id": "manual_database_access", "class": "manual", "kind": "operator_freeze", "required": True},
            {"id": "standby_sync_relay", "class": "relay", "kind": "read_source_write_target", "required": True},
        ],
    }


def command_result(stdout: str = "", returncode: int = 0):
    return {"argv": ["test"], "returncode": returncode, "stdout": stdout, "stderr": ""}


class BackendWriterPauseManagerTests(unittest.TestCase):
    def test_pause_installs_write_guard_dropin_and_emits_safe_status(self):
        manager = load_manager()
        calls: list[list[str]] = []

        def fake_run(argv: list[str], *, timeout: int = 120):
            calls.append(argv)
            if argv[:2] == ["systemctl", "is-active"]:
                return command_result("active\n")
            if argv[:2] == ["systemctl", "is-enabled"]:
                return command_result("enabled\n")
            if argv[:2] == ["systemctl", "show"]:
                return command_result("loaded\n")
            if argv[:4] == ["docker", "compose", "-f", str(compose)] and argv[-3:] == ["ps", "--format", "json"]:
                return command_result("")
            return command_result("")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inventory_path = root / "inventory.json"
            env_path = root / "worker.env"
            dropin = root / "systemd" / "50-writer-pause.conf"
            state_dir = root / "state"
            manifest = root / "services.json"
            compose = root / "compose.yml"
            inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
            env_path.write_text(
                "NUTSNEWS_WORKER_DB_API_DB_PASSWORD=do-not-print\n"
                "NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=true\n"
                "NUTSNEWS_WORKER_UPLIFT_PRODUCTION_WRITES_ENABLED=false\n",
                encoding="utf-8",
            )
            manifest.write_text(json.dumps({"services": [{"name": "fetcher", "replicas": 1}]}), encoding="utf-8")
            compose.write_text("services: {}\n", encoding="utf-8")
            with mock.patch.object(manager, "run_command", side_effect=fake_run), redirect_stdout(StringIO()) as stdout:
                exit_code = manager.main(
                    [
                        "pause",
                        "--inventory",
                        str(inventory_path),
                        "--state-dir",
                        str(state_dir),
                        "--worker-api-env",
                        str(env_path),
                        "--worker-api-dropin",
                        str(dropin),
                        "--worker-runtime-manifest",
                        str(manifest),
                        "--worker-runtime-compose",
                        str(compose),
                        "--failover-attempt-id",
                        "failover-20260726T220500Z",
                        "--drain-timeout-seconds",
                        "1",
                        "--confirm-action",
                    ]
                )
            printed = stdout.getvalue()
            state = json.loads((state_dir / "active-pause.json").read_text(encoding="utf-8"))
            dropin_text = dropin.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=false", dropin_text)
        self.assertEqual(state["attempt_id"], "failover-20260726T220500Z")
        self.assertNotIn("do-not-print", printed)
        self.assertTrue(any("restart" in call for call in calls))
        self.assertTrue(any("--scale" in call and "fetcher=0" in call for call in calls))

    def test_resume_restores_recorded_runtime_replicas_and_removes_dropin(self):
        manager = load_manager()
        calls: list[list[str]] = []

        def fake_run(argv: list[str], *, timeout: int = 120):
            calls.append(argv)
            if argv[:2] == ["systemctl", "is-active"]:
                return command_result("active\n")
            if argv[:2] == ["systemctl", "is-enabled"]:
                return command_result("enabled\n")
            if argv[:2] == ["systemctl", "show"]:
                return command_result("loaded\n")
            if argv[:4] == ["docker", "compose", "-f", str(compose)] and argv[-3:] == ["ps", "--format", "json"]:
                if any("--scale" in call and "fetcher=1" in call for call in calls):
                    return command_result(json.dumps([{"Service": "fetcher", "State": "running"}]))
                return command_result("")
            return command_result("")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inventory_path = root / "inventory.json"
            env_path = root / "worker.env"
            dropin = root / "systemd" / "50-writer-pause.conf"
            state_dir = root / "state"
            manifest = root / "services.json"
            compose = root / "compose.yml"
            inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
            env_path.write_text("NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=true\n", encoding="utf-8")
            manifest.write_text(json.dumps({"services": [{"name": "fetcher", "replicas": 1}]}), encoding="utf-8")
            compose.write_text("services: {}\n", encoding="utf-8")
            dropin.parent.mkdir(parents=True)
            dropin.write_text("[Service]\nEnvironment=NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=false\n", encoding="utf-8")
            state_dir.mkdir()
            (state_dir / "active-pause.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "attempt_id": "failover-20260726T220500Z",
                        "pause_started_at_utc": "2026-07-26T22:05:00Z",
                        "writer_inventory_fingerprint": "sha256:test",
                        "worker_api": {
                            "before": {
                                "active": "active",
                                "enabled": "enabled",
                                "paused": False,
                            }
                        },
                        "worker_runtime": {
                            "before": {
                                "services": [
                                    {"name": "fetcher", "running_replicas": 1},
                                ]
                            }
                        },
                        "safe_metadata_only": True,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(manager, "run_command", side_effect=fake_run), redirect_stdout(StringIO()):
                exit_code = manager.main(
                    [
                        "resume",
                        "--inventory",
                        str(inventory_path),
                        "--state-dir",
                        str(state_dir),
                        "--worker-api-env",
                        str(env_path),
                        "--worker-api-dropin",
                        str(dropin),
                        "--worker-runtime-manifest",
                        str(manifest),
                        "--worker-runtime-compose",
                        str(compose),
                        "--failover-attempt-id",
                        "failover-20260726T220500Z",
                        "--confirm-action",
                    ]
                )
            dropin_exists = dropin.exists()
            active_state_exists = (state_dir / "active-pause.json").exists()
            last_resume_exists = (state_dir / "last-resume.json").exists()
            report = json.loads((state_dir / "last-report.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(dropin_exists)
        self.assertFalse(active_state_exists)
        self.assertTrue(last_resume_exists)
        self.assertTrue(any("--scale" in call and "fetcher=1" in call for call in calls))
        self.assertEqual("pass", report["resume_verification"]["status"])
        self.assertEqual([], report["resume_verification"]["blockers"])

    def test_resume_fails_closed_when_runtime_replica_is_not_restored(self):
        manager = load_manager()

        def fake_run(argv: list[str], *, timeout: int = 120):
            if argv[:2] == ["systemctl", "is-active"]:
                return command_result("active\n")
            if argv[:2] == ["systemctl", "is-enabled"]:
                return command_result("enabled\n")
            if argv[:2] == ["systemctl", "show"]:
                return command_result("loaded\n")
            if argv[:4] == ["docker", "compose", "-f", str(compose)] and argv[-3:] == ["ps", "--format", "json"]:
                return command_result("")
            return command_result("")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inventory_path = root / "inventory.json"
            env_path = root / "worker.env"
            dropin = root / "systemd" / "50-writer-pause.conf"
            state_dir = root / "state"
            manifest = root / "services.json"
            compose = root / "compose.yml"
            inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
            env_path.write_text("NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=true\n", encoding="utf-8")
            manifest.write_text(json.dumps({"services": [{"name": "fetcher", "replicas": 1}]}), encoding="utf-8")
            compose.write_text("services: {}\n", encoding="utf-8")
            dropin.parent.mkdir(parents=True)
            dropin.write_text("[Service]\nEnvironment=NUTSNEWS_WORKER_DB_API_WRITES_ENABLED=false\n", encoding="utf-8")
            state_dir.mkdir()
            (state_dir / "active-pause.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "attempt_id": "failover-20260726T220500Z",
                        "pause_started_at_utc": "2026-07-26T22:05:00Z",
                        "writer_inventory_fingerprint": "sha256:test",
                        "worker_api": {"before": {"active": "active", "enabled": "enabled", "paused": False}},
                        "worker_runtime": {
                            "before": {
                                "services": [
                                    {"name": "fetcher", "running_replicas": 1},
                                ]
                            }
                        },
                        "safe_metadata_only": True,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(manager, "run_command", side_effect=fake_run), redirect_stdout(StringIO()) as stdout:
                exit_code = manager.main(
                    [
                        "resume",
                        "--inventory",
                        str(inventory_path),
                        "--state-dir",
                        str(state_dir),
                        "--worker-api-env",
                        str(env_path),
                        "--worker-api-dropin",
                        str(dropin),
                        "--worker-runtime-manifest",
                        str(manifest),
                        "--worker-runtime-compose",
                        str(compose),
                        "--failover-attempt-id",
                        "failover-20260726T220500Z",
                        "--confirm-action",
                    ]
                )
            report = json.loads(stdout.getvalue())
            active_state_exists = (state_dir / "active-pause.json").exists()

        self.assertEqual(exit_code, 1)
        self.assertTrue(active_state_exists)
        self.assertTrue(report["active_pause_state"])
        self.assertEqual("fail", report["resume_verification"]["status"])
        self.assertIn("worker_runtime_service_resume_mismatch", report["errors"])

    def test_status_fails_when_pause_state_is_missing(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
            with mock.patch.object(manager, "run_command", return_value=command_result("not-found\n")), redirect_stdout(StringIO()) as stdout:
                exit_code = manager.main(
                    [
                        "status",
                        "--inventory",
                        str(inventory_path),
                        "--state-dir",
                        str(root / "state"),
                        "--worker-api-env",
                        str(root / "missing.env"),
                        "--worker-api-dropin",
                        str(root / "dropin.conf"),
                        "--worker-runtime-manifest",
                        str(root / "missing-services.json"),
                        "--worker-runtime-compose",
                        str(root / "missing-compose.yml"),
                        "--failover-attempt-id",
                        "failover-20260726T220500Z",
                    ]
                )
            report = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["active_pause_state"])
        self.assertFalse(report["all_writers_paused"])

    def test_status_fails_for_unknown_runtime_service(self):
        manager = load_manager()

        def fake_run(argv: list[str], *, timeout: int = 120):
            if argv[:2] == ["systemctl", "is-active"]:
                return command_result("inactive\n", returncode=3)
            if argv[:2] == ["systemctl", "is-enabled"]:
                return command_result("disabled\n", returncode=1)
            if argv[:2] == ["systemctl", "show"]:
                return command_result("loaded\n")
            if argv[:4] == ["docker", "compose", "-f", str(compose)] and argv[-3:] == ["ps", "--format", "json"]:
                return command_result("")
            return command_result("")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inventory_path = root / "inventory.json"
            manifest = root / "services.json"
            compose = root / "compose.yml"
            inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
            manifest.write_text(
                json.dumps({"services": [{"name": "fetcher", "replicas": 1}, {"name": "surprise", "replicas": 1}]}),
                encoding="utf-8",
            )
            compose.write_text("services: {}\n", encoding="utf-8")
            with mock.patch.object(manager, "run_command", side_effect=fake_run), redirect_stdout(StringIO()) as stdout:
                exit_code = manager.main(
                    [
                        "status",
                        "--inventory",
                        str(inventory_path),
                        "--state-dir",
                        str(root / "state"),
                        "--worker-api-env",
                        str(root / "missing.env"),
                        "--worker-api-dropin",
                        str(root / "dropin.conf"),
                        "--worker-runtime-manifest",
                        str(manifest),
                        "--worker-runtime-compose",
                        str(compose),
                        "--failover-attempt-id",
                        "failover-20260726T220500Z",
                    ]
                )
            report = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertIn("worker_runtime_service:surprise", report["unknown_writers"])
        self.assertIn("unknown_writer", report["errors"])


if __name__ == "__main__":
    unittest.main()
