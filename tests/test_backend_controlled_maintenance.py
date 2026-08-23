#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

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
        "rabbitmq_state": command("inactive\n"),
        "rabbitmq_health": command("not_configured\n"),
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

    def test_rabbitmq_probe_commands_are_fixed(self):
        publish = maintenance.rabbitmq_probe_command("publish")
        verify = maintenance.rabbitmq_probe_command("verify")
        self.assertIn("nutsnews-rabbitmq-probe publish", publish)
        self.assertIn("nutsnews-rabbitmq-probe verify", verify)
        self.assertIn("--delete-queue", verify)
        self.assertIn("sudo -n", publish)
        self.assertNotIn("$INPUT", publish + verify)

    def test_rabbitmq_probe_not_configured_fails_when_required(self):
        with patch.object(maintenance, "run_ssh_command", return_value=command("not_configured\n")):
            result = maintenance.run_rabbitmq_probe_action("publish", "host", "user", Path("/key"), Path("/known_hosts"), 15)
        self.assertEqual(result["status"], "fail")

    def test_wait_for_reboot_requires_boot_id_change(self):
        with (
            patch.object(maintenance, "run_ssh_command", side_effect=[command("boot-a\n"), command("boot-b\n")]) as run_ssh,
            patch.object(maintenance.time, "sleep", return_value=None),
        ):
            observed, boot_id = maintenance.wait_for_reboot("host", "user", Path("/key"), Path("/known_hosts"), "boot-a", 15, 60)
        self.assertTrue(observed)
        self.assertEqual(boot_id, "boot-b")
        self.assertEqual(run_ssh.call_count, 2)

    def test_wait_for_reboot_times_out_when_boot_id_does_not_change(self):
        with (
            patch.object(maintenance, "run_ssh_command", return_value=command("boot-a\n")),
            patch.object(maintenance.time, "monotonic", side_effect=[0, 0, 2]),
            patch.object(maintenance.time, "sleep", return_value=None),
        ):
            observed, boot_id = maintenance.wait_for_reboot("host", "user", Path("/key"), Path("/known_hosts"), "boot-a", 15, 1)
        self.assertFalse(observed)
        self.assertEqual(boot_id, "")

    def test_prechecks_classify_live_fixture(self):
        checks = maintenance.classify_prechecks(evidence())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["failed_systemd_units"]["status"], "healthy")
        self.assertEqual(by_name["kernel_alignment"]["status"], "healthy")
        self.assertEqual(by_name["service_docker"]["status"], "not_configured")
        self.assertEqual(by_name["service_rabbitmq"]["status"], "not_configured")
        self.assertEqual(by_name["rabbitmq_health"]["status"], "not_configured")
        self.assertEqual(by_name["backend_app_health"]["status"], "not_configured")
        self.assertEqual(by_name["package_updates_visible"]["status"], "warning")

    def test_rabbitmq_health_json_is_classified(self):
        checks = maintenance.classify_prechecks(
            evidence(
                rabbitmq_state=command("active\n"),
                rabbitmq_health=command('{"rabbitmq_version":"4.3.3","status":"healthy"}\n'),
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["service_rabbitmq"]["status"], "healthy")
        self.assertEqual(by_name["rabbitmq_health"]["status"], "healthy")
        self.assertIn("version=4.3.3", by_name["rabbitmq_health"]["summary"])

    def test_reboot_blocks_without_backup_freshness(self):
        checks = maintenance.classify_prechecks(evidence(backup_state=command("not_configured\n")))
        blockers = maintenance.mutation_blockers("reboot", checks)
        self.assertIn({"check": "backup_freshness", "status": "not_configured"}, blockers)

    def test_reboot_allows_missing_active_alert_state(self):
        checks = maintenance.classify_prechecks(evidence(active_alerts=command("not_configured\n")))
        blockers = maintenance.mutation_blockers("reboot", checks)
        self.assertNotIn({"check": "active_alerts", "status": "not_configured"}, blockers)

    def test_reboot_blocks_when_active_alerts_are_present(self):
        checks = maintenance.classify_prechecks(evidence(active_alerts=command("root_disk.active\n")))
        blockers = maintenance.mutation_blockers("reboot", checks)
        self.assertIn({"check": "active_alerts", "status": "critical"}, blockers)

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

    def test_mutation_prechecks_and_rabbitmq_probes_are_advisory(self):
        source = Path("scripts/backend_controlled_maintenance.py").read_text(encoding="utf-8")
        self.assertNotIn('elif precheck["mutation_blockers"]', source)
        self.assertIn("publish_failed_before_reboot", source)
        self.assertIn("verify_failed_after_reboot", source)
        self.assertIn(
            'report["status"] = "pass" if postcheck["boot_id_changed"] else "fail"',
            source,
        )

    def test_workflow_has_no_freeform_command_input(self):
        workflow = (ROOT / ".github/workflows/backend-controlled-maintenance.yml").read_text(encoding="utf-8")
        self.assertIn("- precheck", workflow)
        self.assertIn("- security-upgrade", workflow)
        self.assertIn("- reboot", workflow)
        self.assertNotIn("confirm_target:", workflow)
        self.assertNotIn("inputs.confirm_target", workflow)
        self.assertIn("CONFIRM_TARGET: backend.nutsnews.com", workflow)
        self.assertNotIn("command:", workflow)
        self.assertNotIn("remote_command", workflow)


if __name__ == "__main__":
    unittest.main()
