#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
MAIN_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/main.yml"
DRILL_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/observability_failure_drill.yml"
PROTECTED_APPLY = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"


class BackendObservabilityFailureDrillAnsibleTests(unittest.TestCase):
    def test_failure_drill_uses_fixed_root_owned_paths(self) -> None:
        defaults = DEFAULTS.read_text(encoding="utf-8")
        tasks = DRILL_TASKS.read_text(encoding="utf-8")

        self.assertIn(
            "backend_observability_failure_drill_runner_path: "
            "/usr/local/sbin/nutsnews-observability-failure-drill",
            defaults,
        )
        self.assertIn(
            "backend_observability_failure_drill_state_dir: "
            "/var/lib/nutsnews/observability-drills",
            defaults,
        )
        self.assertIn(
            'backend_observability_failure_drill_metrics_path: "{{ '
            "backend_metrics_textfile_dir }}/observability-failure-drills.prom\"",
            defaults,
        )
        self.assertIn('owner: root', tasks)
        self.assertIn('group: root', tasks)
        self.assertIn('mode: "0750"', tasks)

    def test_failure_drill_is_installed_with_metrics(self) -> None:
        main_tasks = MAIN_TASKS.read_text(encoding="utf-8")

        self.assertIn("ansible.builtin.import_tasks: observability_failure_drill.yml", main_tasks)
        self.assertIn("when: backend_metrics_enabled | bool", main_tasks)

    def test_candidate_is_bounded_validated_and_atomically_installed(self) -> None:
        tasks = DRILL_TASKS.read_text(encoding="utf-8")

        self.assertIn(
            "backend_observability_failure_drill_candidate_path != "
            "backend_observability_failure_drill_runner_path",
            tasks,
        )
        self.assertIn("| dirname", tasks)
        self.assertIn("follow: false", tasks)
        self.assertIn("stat.islnk", tasks)
        self.assertLess(
            tasks.index("Validate observability failure-drill Python candidate"),
            tasks.index("Atomically install validated observability failure-drill executable"),
        )
        self.assertLess(
            tasks.index("Refuse observability failure-drill hook upgrade during an active or unsafe drill state"),
            tasks.index("Atomically install validated observability failure-drill executable"),
        )
        self.assertIn('not isinstance(state.get("recovery_required"), bool)', tasks)
        self.assertIn("candidate.validate_state(state)", tasks)
        self.assertIn("incompatible with the candidate recovery schema", tasks)
        self.assertIn("refusing hook upgrade while failure-drill recovery is required", tasks)
        self.assertIn('src: "{{ backend_observability_failure_drill_candidate_path }}"', tasks)
        self.assertIn('dest: "{{ backend_observability_failure_drill_runner_path }}"', tasks)
        self.assertIn("remote_src: true", tasks)

    def test_metrics_start_with_exactly_five_zero_series(self) -> None:
        tasks = DRILL_TASKS.read_text(encoding="utf-8")

        metric = "nutsnews_observability_failure_drill_active"
        self.assertEqual(tasks.count(f'{metric}{{drill="'), 5)
        self.assertIn("force: false", tasks)
        self.assertIn("assert len(samples) == 5", tasks)
        self.assertIn("assert 'drill_id=' not in text", tasks)
        self.assertIn("assert 'deployment_environment=' not in text", tasks)
        self.assertIn("assert 'host=' not in text", tasks)
        self.assertNotIn(f"{metric}{{deployment_environment=", tasks)
        self.assertNotIn(f"{metric}{{host=", tasks)
        self.assertNotIn("exported_deployment_environment", tasks)
        self.assertNotIn("exported_host", tasks)
        for drill in (
            "worker-unavailable",
            "rabbitmq-zero-consumer",
            "rabbitmq-growing-dlq",
            "postgres-relay-lag",
            "backend-readiness-failed",
        ):
            self.assertIn(f'drill="{drill}"}} 0', tasks)

    def test_persistent_watchdog_runs_every_minute_after_install(self) -> None:
        defaults = DEFAULTS.read_text(encoding="utf-8")
        tasks = DRILL_TASKS.read_text(encoding="utf-8")

        self.assertIn(
            "backend_observability_failure_drill_watchdog_calendar: \"*-*-* *:*:00\"",
            defaults,
        )
        self.assertIn("ExecStart={{ backend_observability_failure_drill_runner_path }} --action watchdog", tasks)
        self.assertIn("OnCalendar={{ backend_observability_failure_drill_watchdog_calendar }}", tasks)
        self.assertIn("Persistent=true", tasks)
        self.assertIn("daemon_reload: true", tasks)
        self.assertIn("enabled: true", tasks)
        self.assertIn("state: started", tasks)
        self.assertIn("unit: nutsnews-observability-failure-drill-watchdog.service", defaults)
        self.assertIn("unit: nutsnews-observability-failure-drill-watchdog.timer", defaults)
        self.assertLess(
            tasks.index("Atomically install validated observability failure-drill executable"),
            tasks.index("Enable persistent observability failure-drill recovery watchdog"),
        )
        self.assertIn("- watchdog", tasks)

    def test_no_parameterized_sudoers_rule_is_installed(self) -> None:
        tasks = DRILL_TASKS.read_text(encoding="utf-8")

        self.assertNotIn("sudoers", tasks.lower())
        self.assertNotIn("NOPASSWD", tasks)
        self.assertIn("privilege_boundary: existing sudo-n workflow boundary", tasks)

    def test_hook_upgrades_share_the_runtime_and_drill_concurrency_boundary(self) -> None:
        workflow = PROTECTED_APPLY.read_text(encoding="utf-8")

        self.assertIn("group: backend-worker-runtime-operations", workflow)
        self.assertIn("cancel-in-progress: false", workflow)


if __name__ == "__main__":
    unittest.main()
