#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backend_health_report


def command(stdout: str, returncode: int = 0) -> dict[str, object]:
    return {"stdout": stdout, "stderr": "", "returncode": returncode}


def fixture_report(**overrides):
    commands = {
        "memory": command(
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:     10415267840   536870912  9448928051     4194304   742391808  9878424780\n"
            "Swap:             0          0          0\n"
        ),
        "kernel": command("7.0.0-28-generic\n"),
        "latest_installed_kernel": command("7.0.0-28-generic\n"),
        "root_disk": command("/dev/vda1 82678120448 2147483648 80530636800 3% /\n"),
        "root_inodes": command("/dev/vda1 5242880 102400 5140480 2% /\n"),
        "failed_units": command(""),
        "reboot_required": command("no\n"),
        "upgradable_count": command("86\n"),
        "service_states": command(
            "ssh=active\n"
            "ufw=active\n"
            "fail2ban=unavailable\n"
            "docker=unavailable\n"
            "caddy=active\n"
            "postgresql=inactive\n"
            "alloy=unavailable\n"
            "sysstat=active\n"
        ),
        "backend_health": command("ok\n"),
        "backup_tools": command("restic=missing\nrclone=missing\npg_dump=missing\ndocker=missing\ncaddy=missing\nalloy=missing\n"),
        "backup_status": command("not_configured\n"),
        "sudo_nopasswd": command("no\n"),
    }
    commands.update(overrides)
    return {
        "version": 1,
        "last_report_run_at": "2026-07-16T12:00:00Z",
        "next_report_run_at": "2026-07-17T12:00:00Z",
        "target": {"host": "65.75.201.18", "user": "rami"},
        "delivery": {"status": "skipped", "detail": "send_email=false"},
        "ssh": {"host": "65.75.201.18", "user": "rami", "commands": commands},
    }


class BackendHealthReportTests(unittest.TestCase):
    def test_redaction_removes_sensitive_values(self):
        raw = (
            "github_pat_1234567890abcdefghijklmnopqrstuvwxyzABCDEF "
            "postgres://user:secret@example.com/db "
            "person@example.com "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        redacted = backend_health_report.redact(raw)
        self.assertNotIn("github_pat_", redacted)
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("person@example.com", redacted)
        self.assertNotIn("abc\n-----END", redacted)

    def test_classify_fixture_distinguishes_known_missing_services(self):
        checks, summary = backend_health_report.classify(fixture_report())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["root_disk_used_percent"]["status"], "healthy")
        self.assertEqual(by_name["package_updates"]["status"], "warning")
        self.assertEqual(by_name["kernel_alignment"]["status"], "healthy")
        self.assertEqual(by_name["service_docker"]["status"], "not_configured")
        self.assertEqual(by_name["backend_endpoint_health"]["status"], "healthy")
        self.assertEqual(by_name["backup_tooling"]["status"], "not_configured")
        self.assertEqual(by_name["backup_freshness"]["status"], "not_configured")
        self.assertEqual(by_name["backup_verification"]["status"], "not_configured")
        self.assertEqual(by_name["backup_restore_drill"]["status"], "not_configured")
        self.assertEqual(by_name["sudo_nopasswd"]["status"], "warning")
        self.assertGreaterEqual(summary["warning"], 2)

    def test_backup_status_checks_are_distinct(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                backup_tools=command("restic=present\n"),
                backup_status=command(
                    "{"
                    '"backup":{"status":"healthy","freshness_status":"healthy","snapshot_id":"abc","quota":{"status":"warning"}},'
                    '"verification":{"status":"healthy","snapshot_id":"abc"},'
                    '"restore_drill":{"status":"critical","snapshot_id":"abc"}'
                    "}\n"
                ),
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["backup_freshness"]["status"], "healthy")
        self.assertEqual(by_name["backup_verification"]["status"], "healthy")
        self.assertEqual(by_name["backup_restore_drill"]["status"], "critical")
        self.assertEqual(by_name["backup_storage_quota"]["status"], "warning")

    def test_active_caddy_with_failed_endpoint_is_critical(self):
        checks, summary = backend_health_report.classify(fixture_report(backend_health=command("")))
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["backend_endpoint_health"]["status"], "critical")
        self.assertGreaterEqual(summary["critical"], 1)

    def test_missing_smtp_degrades_without_failure(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("NUTSNEWS_REPORT_")}
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, env, clear=True):
            args = backend_health_report.parse_args(
                [
                    "--ssh-key",
                    str(Path(tmpdir) / "missing-key"),
                    "--known-hosts",
                    str(Path(tmpdir) / "missing-known-hosts"),
                    "--output",
                    str(Path(tmpdir) / "report.json"),
                    "--send-email",
                ]
            )
            report = backend_health_report.build_report(args)
        self.assertEqual(report["delivery"]["status"], "not_configured")
        self.assertIn("NUTSNEWS_REPORT_SMTP_HOST", report["delivery"]["detail"])

    def test_remote_commands_are_fixed_and_do_not_include_shell_input_placeholder(self):
        self.assertIn("failed_units", backend_health_report.REMOTE_COMMANDS)
        for command_text in backend_health_report.REMOTE_COMMANDS.values():
            self.assertNotIn("{command", command_text)
            self.assertNotIn("$INPUT", command_text)

    def test_render_text_contains_status_counts(self):
        report = fixture_report()
        checks, summary = backend_health_report.classify(report)
        report["checks"] = checks
        report["summary"] = summary
        text = backend_health_report.render_text(report)
        self.assertIn("NutsNews backend health report", text)
        self.assertIn("- warning:", text)
        self.assertIn("package_updates", text)


if __name__ == "__main__":
    unittest.main()
