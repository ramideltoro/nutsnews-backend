#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

from scripts import backend_controlled_maintenance as maintenance


ROOT = Path(__file__).resolve().parents[1]


def command(stdout: str, returncode: int = 0) -> dict[str, object]:
    return {"stdout": stdout, "stderr": "", "returncode": returncode}


def evidence(**overrides):
    commands = {
        "hostname": command("backend\n"),
        "kernel": command("7.0.0-28-generic\n"),
        "latest_installed_kernel": command("7.0.0-28-generic\n"),
        "boot_id": command("boot-a\n"),
        "failed_units": command(""),
        "ssh_state": command("active\n"),
        "ufw_state": command("active\n"),
        "fail2ban_state": command("active\n"),
        "docker_state": command("inactive\n"),
        "caddy_state": command("active\n"),
        "backend_units": command("nutsnews-ops-dashboard-collect.service loaded inactive dead collector\n"),
        "backend_endpoint": command("ok\n"),
        "root_disk": command("/dev/vda1 82678120448 2147483648 80530636800 3% /\n"),
        "root_inodes": command("/dev/vda1 5242880 102400 5140480 2% /\n"),
        "reboot_required": command("no\n"),
        "upgradable_count": command("2\n"),
        "unattended_upgrade": command("present\n"),
        "unattended_upgrades_enabled": command("enabled\n"),
        "backup_state": command("resticprofile_present\nactive\n"),
        "active_alerts": command(""),
    }
    commands.update(overrides)
    return {"commands": commands}


class BackendControlledMaintenanceTests(unittest.TestCase):
    def test_remote_and_mutation_commands_are_fixed(self):
        for command_text in list(maintenance.REMOTE_COMMANDS.values()) + list(maintenance.MAINTENANCE_COMMANDS.values()):
            self.assertNotIn("{command", command_text)
            self.assertNotIn("$INPUT", command_text)
        self.assertEqual(set(maintenance.MAINTENANCE_COMMANDS), {"security-upgrade", "reboot"})

    def test_prechecks_classify_live_fixture(self):
        checks = maintenance.classify_prechecks(evidence())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["failed_systemd_units"]["status"], "healthy")
        self.assertEqual(by_name["kernel_alignment"]["status"], "healthy")
        self.assertEqual(by_name["service_docker"]["status"], "not_configured")
        self.assertEqual(by_name["backend_app_health"]["status"], "not_configured")
        self.assertEqual(by_name["package_updates_visible"]["status"], "warning")

    def test_reboot_blocks_without_backup_freshness(self):
        checks = maintenance.classify_prechecks(evidence(backup_state=command("not_configured\n")))
        blockers = maintenance.mutation_blockers("reboot", checks)
        self.assertIn({"check": "backup_freshness", "status": "not_configured"}, blockers)

    def test_backup_status_json_can_satisfy_reboot_freshness_gate(self):
        checks = maintenance.classify_prechecks(
            evidence(backup_state=command('{"status":"healthy","freshness_status":"healthy","snapshot_id":"abc"}\n'))
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["backup_freshness"]["status"], "healthy")

    def test_security_upgrade_blocks_when_unattended_upgrade_missing(self):
        checks = maintenance.classify_prechecks(evidence(unattended_upgrade=command("missing\n")))
        blockers = maintenance.mutation_blockers("security-upgrade", checks)
        self.assertIn({"check": "unattended_security_updates", "status": "warning"}, blockers)

    def test_workflow_has_no_freeform_command_input(self):
        workflow = (ROOT / ".github/workflows/backend-controlled-maintenance.yml").read_text(encoding="utf-8")
        self.assertIn("- precheck", workflow)
        self.assertIn("- security-upgrade", workflow)
        self.assertIn("- reboot", workflow)
        self.assertIn("confirm_target", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertNotIn("command:", workflow)
        self.assertNotIn("remote_command", workflow)


if __name__ == "__main__":
    unittest.main()
