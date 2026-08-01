from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "ansible"
    / "roles"
    / "backend_baseline"
    / "files"
    / "nutsnews_observability_failure_drill.py"
)
ANSIBLE_TASKS = ROOT / "ansible" / "roles" / "backend_baseline" / "tasks" / "observability_failure_drill.yml"
SPEC = importlib.util.spec_from_file_location("nutsnews_observability_failure_drill", SCRIPT)
assert SPEC and SPEC.loader
drill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drill
SPEC.loader.exec_module(drill)


DRILL_ID = "nnobs-1234567890-deadbeef"
OTHER_DRILL_ID = "nnobs-1234567891-cafebabe"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self, *, running: bool = True, schedule_ok: bool = True, stop_ok: bool = True):
        self.running = running
        self.schedule_ok = schedule_ok
        self.stop_ok = stop_ok
        self.commands: list[list[str]] = []
        self.on_command = None

    def run(self, argv, *, timeout=120):
        command = list(argv)
        self.commands.append(command)
        if self.on_command is not None:
            self.on_command(command)
        returncode = 0
        stdout = ""
        if command[0] == "/usr/bin/systemd-run":
            returncode = 0 if self.schedule_ok else 1
        elif command[:2] == ["/usr/bin/systemctl", "stop"]:
            returncode = 0
        elif command[0:2] == ["/usr/bin/docker", "compose"]:
            if "ps" in command:
                stdout = "translation\n" if self.running else ""
            elif "stop" in command:
                returncode = 0 if self.stop_ok else 1
                if self.stop_ok:
                    self.running = False
            elif "up" in command:
                self.running = True
        return subprocess.CompletedProcess(command, returncode, stdout, "")


def manifest() -> dict:
    return {
        "schema_version": 1,
        "generated_by": "backend_worker_runtime",
        "mode": "shadow",
        "cutover_state": "shadow",
        "production_writes_enabled": False,
        "backend_api": {"writes_enabled": False},
        "services": [
            {
                "name": "translation",
                "stage": "translation",
                "runtime_mode": "shadow",
                "postgres": {"production_write_path": False},
                "env": {"NUTSNEWS_TRANSLATION_SHADOW_MODE": "true"},
            }
        ],
    }


def args(
    action: str,
    selected_drill: str = "postgres-relay-lag",
    drill_id: str = DRILL_ID,
    *,
    execute: bool = False,
):
    values = [
        "--action",
        action,
        "--drill",
        selected_drill,
        "--drill-id",
        drill_id,
        "--duration-seconds",
        "900",
        "--confirm-target",
        "backend.nutsnews.com",
        "--confirm-drill",
        selected_drill,
    ]
    if execute:
        values.append("--execute")
    return drill.parse_args(values)


class BackendObservabilityFailureDrillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = drill.Paths(
            state_dir=root / "state",
            state=root / "state" / "state.json",
            lock=root / "state" / "operation.lock",
            metrics=root / "metrics" / "observability-failure-drills.prom",
            manifest=root / "services.json",
            compose=root / "compose.yml",
        )
        self.paths.manifest.write_text(json.dumps(manifest()) + "\n", encoding="utf-8")
        self.paths.compose.write_text("services: {}\n", encoding="utf-8")
        drill.write_metrics(self.paths, None)

    def tearDown(self):
        self.temporary.cleanup()

    def run_as_root(self, parsed, *, runner=None, probe=lambda: True, now_fn=lambda: NOW):
        with patch.object(drill.os, "geteuid", return_value=0):
            return drill.run(
                parsed,
                paths=self.paths,
                runner=runner or FakeRunner(),
                probe=probe,
                now_fn=now_fn,
                sleeper=lambda _seconds: None,
            )

    def inject(self, selected_drill="postgres-relay-lag", *, runner=None, probe=lambda: True):
        return self.run_as_root(
            args("inject", selected_drill, execute=True),
            runner=runner,
            probe=probe,
        )

    def test_metric_contract_has_five_fixed_drill_only_series(self):
        rendered = drill.render_metrics("rabbitmq-growing-dlq")
        samples = [line for line in rendered.splitlines() if line.startswith("nutsnews_")]

        self.assertEqual(5, len(samples))
        self.assertEqual(1, sum(line.endswith(" 1") for line in samples))
        self.assertIn('nutsnews_observability_failure_drill_active{drill="rabbitmq-growing-dlq"} 1', samples)
        self.assertNotIn("host=", rendered)
        self.assertNotIn("deployment_environment=", rendered)
        self.assertEqual(
            Path("/var/lib/nutsnews/metrics/observability-failure-drills.prom"),
            drill.DEFAULT_PATHS.metrics,
        )
        tasks = ANSIBLE_TASKS.read_text(encoding="utf-8")
        for line in drill.render_metrics(None).splitlines():
            self.assertIn(line, tasks)

    def test_plan_is_the_non_mutating_default(self):
        parsed = drill.parse_args(
            [
                "--drill",
                "postgres-relay-lag",
                "--drill-id",
                DRILL_ID,
                "--confirm-target",
                "backend.nutsnews.com",
                "--confirm-drill",
                "postgres-relay-lag",
            ]
        )
        runner = FakeRunner()
        before = self.paths.metrics.read_bytes()
        report = drill.run(parsed, paths=self.paths, runner=runner)

        self.assertEqual("plan", report["action"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(self.paths.state_dir.exists())
        self.assertEqual(before, self.paths.metrics.read_bytes())
        self.assertEqual([], runner.commands)

    def test_cli_rejects_unbounded_id_wrong_duration_and_execute_scope(self):
        for parsed, expected in (
            (args("plan", drill_id="gha-123-1"), "drill_id_bounded"),
            (
                drill.parse_args(
                    [
                        "--action",
                        "plan",
                        "--drill",
                        "postgres-relay-lag",
                        "--drill-id",
                        DRILL_ID,
                        "--duration-seconds",
                        "899",
                        "--confirm-target",
                        "backend.nutsnews.com",
                        "--confirm-drill",
                        "postgres-relay-lag",
                    ]
                ),
                "duration_fixed",
            ),
            (args("inject"), "execute_confirmation"),
            (args("recover", execute=True), "execute_scope"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(drill.DrillFailure, expected):
                    drill.validate_cli(parsed)

    def test_each_non_worker_injection_is_telemetry_only_and_schedules_first(self):
        for selected_drill in drill.TELEMETRY_ONLY_DRILLS:
            with self.subTest(drill=selected_drill):
                self.tearDown()
                self.setUp()
                runner = FakeRunner()

                def assert_schedule_precedes_fixture(command):
                    if command[0] == "/usr/bin/systemd-run":
                        self.assertFalse(self.paths.state.exists())
                        self.assertTrue(drill.metrics_match(self.paths, None))

                runner.on_command = assert_schedule_precedes_fixture
                report = self.inject(selected_drill, runner=runner)

                self.assertTrue(report["recovery_scheduled"])
                self.assertTrue(report["recovery_required"])
                self.assertTrue(drill.metrics_match(self.paths, selected_drill))
                self.assertEqual("/usr/bin/systemd-run", runner.commands[0][0])
                self.assertFalse(any(command[0] == "/usr/bin/docker" for command in runner.commands))
                self.assertEqual({"/usr/bin/systemd-run"}, {command[0] for command in runner.commands})

    def test_scheduled_recovery_is_fixed_and_has_no_shell(self):
        runner = FakeRunner()
        self.inject(runner=runner)
        command = runner.commands[0]

        self.assertIn("--on-active=1080s", command)
        self.assertIn("--action", command)
        self.assertIn("recover", command)
        self.assertIn(DRILL_ID, command)
        self.assertIn(str(drill.SCRIPT_PATH), command)
        self.assertNotIn("bash", command)
        self.assertNotIn("sh", command)

    def test_overlap_and_stale_recovery_id_fail_without_clearing_fixture(self):
        self.inject("postgres-relay-lag")
        with self.assertRaisesRegex(drill.DrillFailure, "single_active_drill"):
            self.run_as_root(
                args("inject", "backend-readiness-failed", OTHER_DRILL_ID, execute=True)
            )
        with self.assertRaisesRegex(drill.DrillFailure, "drill_identity_matches"):
            self.run_as_root(
                args("recover", "postgres-relay-lag", OTHER_DRILL_ID)
            )

        state = drill.read_state(self.paths)
        self.assertEqual(DRILL_ID, state["drill_id"])
        self.assertTrue(state["recovery_required"])
        self.assertTrue(drill.metrics_match(self.paths, "postgres-relay-lag"))

    def test_metric_only_recovery_clears_all_series_and_state(self):
        runner = FakeRunner()
        self.inject("backend-readiness-failed", runner=runner)
        report = self.run_as_root(
            args("recover", "backend-readiness-failed"), runner=runner
        )

        self.assertTrue(report["recovered"])
        self.assertFalse(report["recovery_required"])
        self.assertTrue(drill.metrics_match(self.paths, None))
        self.assertTrue(drill.read_state(self.paths)["recovered"])
        self.assertTrue(any(command[:2] == ["/usr/bin/systemctl", "stop"] for command in runner.commands))

    def test_worker_drill_stops_only_translation_after_recovery_is_scheduled(self):
        runner = FakeRunner(running=True)
        report = self.inject(
            "worker-unavailable",
            runner=runner,
            probe=lambda: runner.running,
        )
        schedule_index = next(i for i, command in enumerate(runner.commands) if command[0] == "/usr/bin/systemd-run")
        stop_index = next(i for i, command in enumerate(runner.commands) if "stop" in command and command[0] == "/usr/bin/docker")
        stop_command = runner.commands[stop_index]

        self.assertLess(schedule_index, stop_index)
        self.assertEqual(["stop", "-t", "30", "translation"], stop_command[-4:])
        self.assertFalse(runner.running)
        self.assertTrue(report["recovery_required"])
        self.assertTrue(drill.metrics_match(self.paths, "worker-unavailable"))

    def test_worker_preflight_rejects_every_production_ownership_escape(self):
        mutations = (
            lambda value: value.update({"schema_version": 2}),
            lambda value: value.update({"generated_by": "other"}),
            lambda value: value.update({"mode": "production"}),
            lambda value: value.update({"cutover_state": "cutover-approved"}),
            lambda value: value.update({"production_writes_enabled": True}),
            lambda value: value["backend_api"].update({"writes_enabled": True}),
            lambda value: value["services"][0].update({"runtime_mode": "production"}),
            lambda value: value["services"][0]["postgres"].update({"production_write_path": True}),
            lambda value: value["services"][0]["env"].update({"NUTSNEWS_TRANSLATION_SHADOW_MODE": "false"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                unsafe = copy.deepcopy(manifest())
                mutate(unsafe)
                self.paths.manifest.write_text(json.dumps(unsafe) + "\n", encoding="utf-8")
                runner = FakeRunner()
                with self.assertRaisesRegex(drill.DrillFailure, "shadow_manifest"):
                    self.run_as_root(
                        args("inject", "worker-unavailable", execute=True),
                        runner=runner,
                        probe=lambda: True,
                    )
                self.assertFalse(any(command[0] == "/usr/bin/systemd-run" for command in runner.commands))
                self.paths.manifest.write_text(json.dumps(manifest()) + "\n", encoding="utf-8")

    def test_worker_recovery_restores_liveness_before_clearing_fixture(self):
        runner = FakeRunner(running=True)
        self.inject("worker-unavailable", runner=runner, probe=lambda: runner.running)
        liveness_observations = []

        def recovery_probe():
            if runner.running:
                liveness_observations.append(drill.metrics_match(self.paths, "worker-unavailable"))
            return runner.running

        report = self.run_as_root(
            args("recover", "worker-unavailable"),
            runner=runner,
            probe=recovery_probe,
        )
        up_command = next(command for command in runner.commands if "up" in command)

        self.assertEqual(
            ["up", "-d", "--no-deps", "--pull", "never", "translation"],
            up_command[-6:],
        )
        self.assertEqual([True], liveness_observations)
        self.assertTrue(report["recovered"])
        self.assertTrue(drill.metrics_match(self.paths, None))

    def test_worker_recovery_does_not_depend_on_later_cutover_manifest_state(self):
        runner = FakeRunner(running=True)
        self.inject("worker-unavailable", runner=runner, probe=lambda: runner.running)
        changed = manifest()
        changed["mode"] = "production"
        changed["cutover_state"] = "cutover-approved"
        changed["production_writes_enabled"] = True
        changed["backend_api"]["writes_enabled"] = True
        changed["services"][0]["runtime_mode"] = "production"
        changed["services"][0]["postgres"]["production_write_path"] = True
        changed["services"][0]["env"]["NUTSNEWS_TRANSLATION_SHADOW_MODE"] = "false"
        self.paths.manifest.write_text(json.dumps(changed) + "\n", encoding="utf-8")

        report = self.run_as_root(
            args("recover", "worker-unavailable"),
            runner=runner,
            probe=lambda: runner.running,
        )

        self.assertTrue(report["recovered"])
        self.assertTrue(runner.running)
        self.assertTrue(drill.metrics_match(self.paths, None))

    def test_failed_worker_stop_immediately_runs_fixed_recovery(self):
        runner = FakeRunner(running=True, stop_ok=False)
        with self.assertRaisesRegex(drill.DrillFailure, "translation_stopped"):
            self.inject("worker-unavailable", runner=runner, probe=lambda: runner.running)

        flattened = [item for command in runner.commands for item in command]
        self.assertIn("up", flattened)
        self.assertTrue(runner.running)
        self.assertTrue(drill.metrics_match(self.paths, None))
        self.assertTrue(drill.read_state(self.paths)["recovered"])

    def test_watchdog_is_non_mutating_before_deadline_and_recovers_after(self):
        runner = FakeRunner()
        self.inject("postgres-relay-lag", runner=runner)
        command_count = len(runner.commands)
        watchdog = drill.parse_args(["--action", "watchdog"])

        before = self.run_as_root(
            watchdog,
            runner=runner,
            now_fn=lambda: NOW + timedelta(seconds=1079),
        )
        self.assertTrue(before["recovery_required"])
        self.assertEqual(command_count, len(runner.commands))
        self.assertTrue(drill.metrics_match(self.paths, "postgres-relay-lag"))

        after = self.run_as_root(
            watchdog,
            runner=runner,
            now_fn=lambda: NOW + timedelta(seconds=1081),
        )
        self.assertTrue(after["recovered"])
        self.assertTrue(drill.metrics_match(self.paths, None))

    def test_corrupt_state_blocks_watchdog_without_clearing_fixture(self):
        self.paths.state_dir.mkdir()
        self.paths.state.write_text('{"recovery_required":true}\n', encoding="utf-8")
        drill.write_metrics(self.paths, "postgres-relay-lag")
        watchdog = drill.parse_args(["--action", "watchdog"])

        with self.assertRaisesRegex(drill.DrillFailure, "state_valid"):
            self.run_as_root(watchdog)
        self.assertTrue(drill.metrics_match(self.paths, "postgres-relay-lag"))

    def test_missing_malformed_or_extra_fixture_series_block_preflight(self):
        candidates = (
            None,
            "garbage\n",
            drill.render_metrics(None) + 'nutsnews_observability_failure_drill_active{drill="extra"} 0\n',
            drill.render_metrics("postgres-relay-lag"),
        )
        for content in candidates:
            with self.subTest(content=content):
                if self.paths.metrics.exists():
                    self.paths.metrics.unlink()
                if content is not None:
                    self.paths.metrics.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(drill.DrillFailure, "fixture_series_clear"):
                    drill.run(args("plan"), paths=self.paths, runner=FakeRunner())

    def test_symlink_compose_is_rejected_before_recovery_schedule(self):
        real_compose = self.paths.compose.with_name("real-compose.yml")
        real_compose.write_text("services: {}\n", encoding="utf-8")
        self.paths.compose.unlink()
        self.paths.compose.symlink_to(real_compose)
        runner = FakeRunner()

        with self.assertRaisesRegex(drill.DrillFailure, "shadow_manifest"):
            self.run_as_root(
                args("inject", "worker-unavailable", execute=True),
                runner=runner,
                probe=lambda: True,
            )
        self.assertFalse(any(command[0] == "/usr/bin/systemd-run" for command in runner.commands))

    def test_liveness_probe_rejects_oversized_response_body(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                self.assert_limit = limit
                return b"x" * limit

        response = Response()
        with patch.object(drill.urllib.request, "urlopen", return_value=response):
            self.assertFalse(drill.liveness_healthy())
        self.assertEqual(65_537, response.assert_limit)

    def test_active_state_requires_exact_recovery_deadline_delta(self):
        state = drill.active_state(
            "postgres-relay-lag",
            DRILL_ID,
            NOW,
            drill.recovery_unit(DRILL_ID),
        )
        state["recovery_deadline_utc"] = drill.render_timestamp(NOW + timedelta(days=30))
        with self.assertRaisesRegex(drill.DrillFailure, "state_valid"):
            drill.validate_state(state)

    def test_state_rejects_integer_booleans_and_noncanonical_timestamps(self):
        for field, value in (
            ("recovery_required", 1),
            ("recovered", 0),
            ("injected_at_utc", "2026-08-01T12:00:00+00:00"),
        ):
            with self.subTest(field=field):
                state = drill.active_state(
                    "postgres-relay-lag",
                    DRILL_ID,
                    NOW,
                    drill.recovery_unit(DRILL_ID),
                )
                state[field] = value
                with self.assertRaisesRegex(drill.DrillFailure, "state_valid"):
                    drill.validate_state(state)

    def test_stdout_schema_is_allowlisted_and_unexpected_errors_are_redacted(self):
        expected_keys = {
            "schema_version",
            "safe_metadata_only",
            "action",
            "drill",
            "drill_id",
            "status",
            "dry_run",
            "recovery_scheduled",
            "recovery_required",
            "recovered",
            "injected_at_utc",
            "recovery_deadline_utc",
            "duration_seconds",
            "checks",
        }
        report = drill.run(args("plan"), paths=self.paths, runner=FakeRunner())
        self.assertEqual(expected_keys, set(report))
        self.assertTrue(all(set(item) == {"name", "status"} for item in report["checks"]))

        output = io.StringIO()
        with patch.object(drill, "run", side_effect=RuntimeError("protected-secret-value")):
            with redirect_stdout(output):
                self.assertEqual(1, drill.main([
                    "--drill", "postgres-relay-lag",
                    "--drill-id", DRILL_ID,
                    "--confirm-target", "backend.nutsnews.com",
                    "--confirm-drill", "postgres-relay-lag",
                ]))
        rendered = output.getvalue()
        self.assertNotIn("protected-secret-value", rendered)
        self.assertEqual("internal_failure", json.loads(rendered)["checks"][0]["name"])


if __name__ == "__main__":
    unittest.main()
