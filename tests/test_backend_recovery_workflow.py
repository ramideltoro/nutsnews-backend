#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

from scripts import backend_recovery_workflow as recovery


ROOT = Path(__file__).resolve().parents[1]


def command(stdout: str, returncode: int = 0) -> dict[str, object]:
    return {"stdout": stdout, "stderr": "", "returncode": returncode}


def evidence(**overrides):
    commands = {
        "hostname": command("backend\n"),
        "boot_id": command("boot-a\n"),
        "kernel": command("7.0.0-28-generic\n"),
        "failed_units": command(""),
        "root_disk": command("/dev/vda1 82678120448 2147483648 80530636800 3% /\n"),
        "root_inodes": command("/dev/vda1 5242880 102400 5140480 2% /\n"),
        "reboot_required": command("no\n"),
        "service_states": command(
            "ssh=loaded/active\n"
            "ufw=loaded/active\n"
            "fail2ban=loaded/active\n"
            "caddy=loaded/active\n"
            "alloy=loaded/active\n"
            "nutsnews-backup.service=loaded/inactive\n"
            "nutsnews-backup-verify.service=loaded/inactive\n"
            "nutsnews-restore-drill.service=loaded/inactive\n"
            "nutsnews-metrics-textfile.service=loaded/inactive\n"
            "nutsnews-ops-dashboard-collect.service=loaded/inactive\n"
        ),
        "timers": command(""),
        "backend_health": command("ok\n"),
        "caddy_config": command("Valid configuration\n"),
        "alloy_config": command("Config file is valid\n"),
        "backup_runner": command("present\n"),
        "backup_status": command(
            "{"
            '"backup":{"status":"healthy","freshness_status":"healthy","snapshot_id":"abc"},'
            '"verification":{"status":"healthy","snapshot_id":"abc"},'
            '"restore_drill":{"status":"healthy","snapshot_id":"abc"}'
            "}\n"
        ),
        "metrics_textfile": command("present mtime=1784255100 size=2872 path=/var/lib/nutsnews/metrics/nutsnews.prom\n"),
        "ops_dashboard_snapshot": command(
            "present mtime=1784255053 size=7266 path=/var/www/nutsnews-ops-dashboard/status.json\n"
        ),
        "recovery_status": command("not_configured\n"),
    }
    commands.update(overrides)
    return {"commands": commands}


class BackendRecoveryWorkflowTests(unittest.TestCase):
    def test_action_commands_are_fixed_and_narrow(self):
        self.assertIn("diagnostics", recovery.RECOVERY_ACTIONS)
        self.assertIn("refresh-metrics", recovery.RECOVERY_ACTIONS)
        for name, definition in recovery.RECOVERY_ACTIONS.items():
            command_text = definition["command"] or ""
            self.assertNotIn("{command", command_text, name)
            self.assertNotIn("$INPUT", command_text, name)
            self.assertNotIn("docker system prune", command_text, name)
            self.assertNotIn("ansible-playbook", command_text, name)
            self.assertNotIn("eval ", command_text, name)
        self.assertEqual(
            {
                name
                for name, definition in recovery.RECOVERY_ACTIONS.items()
                if definition["mutates"]
            },
            {
                "trigger-backup",
                "trigger-restore-drill",
                "reload-caddy",
                "restart-caddy",
                "restart-alloy",
                "restart-fail2ban",
                "refresh-metrics",
                "refresh-ops-dashboard",
            },
        )

    def test_classify_live_like_fixture(self):
        checks = recovery.classify_checks(evidence())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["service_ssh"]["status"], "healthy")
        self.assertEqual(by_name["service_caddy"]["status"], "healthy")
        self.assertEqual(by_name["caddy_config"]["status"], "healthy")
        self.assertEqual(by_name["alloy_config"]["status"], "healthy")
        self.assertEqual(by_name["backup_action_surface"]["status"], "healthy")
        self.assertEqual(by_name["backup_freshness"]["status"], "healthy")
        self.assertEqual(by_name["metrics_textfile"]["status"], "healthy")
        self.assertEqual(by_name["ops_dashboard_snapshot"]["status"], "healthy")
        self.assertEqual(by_name["recovery_last_run"]["status"], "not_configured")

    def test_action_blockers_are_action_specific(self):
        checks = recovery.classify_checks(evidence(caddy_config=command("invalid\n", returncode=1)))
        self.assertIn({"check": "caddy_config", "status": "critical"}, recovery.blockers_for_action("reload-caddy", checks))
        self.assertNotIn({"check": "caddy_config", "status": "critical"}, recovery.blockers_for_action("restart-alloy", checks))

    def test_postcheck_requirements_are_enforced(self):
        postcheck = recovery.build_check(
            "refresh-metrics",
            {"host": "65.75.201.18", "user": "rami"},
            evidence(metrics_textfile=command("not_configured\n")),
        )
        self.assertEqual(
            recovery.postcheck_failures("refresh-metrics", postcheck),
            [{"check": "metrics_textfile", "status": "not_configured"}],
        )

    def test_workflow_has_no_freeform_command_or_service_input(self):
        workflow = (ROOT / ".github/workflows/backend-recovery.yml").read_text(encoding="utf-8")
        for action in recovery.RECOVERY_ACTIONS:
            self.assertIn(f"- {action}", workflow)
        self.assertIn("confirm_target", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertIn("production-backend", workflow)
        self.assertNotIn("remote_command", workflow)
        self.assertNotIn("shell_command", workflow)
        self.assertNotIn("service_name", workflow)
        self.assertNotIn("ansible_tags", workflow)
        self.assertNotIn("script_body", workflow)


if __name__ == "__main__":
    unittest.main()
