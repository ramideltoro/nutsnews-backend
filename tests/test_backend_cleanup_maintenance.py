#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

from scripts import backend_cleanup_maintenance as cleanup


ROOT = Path(__file__).resolve().parents[1]


def command(stdout: str, returncode: int = 0) -> dict[str, object]:
    return {"stdout": stdout, "stderr": "", "returncode": returncode}


def evidence(**overrides):
    commands = {
        "hostname": command("backend\n"),
        "root_disk": command("/dev/sda1 82085269504 5918576640 76149915648 8% /\n"),
        "root_inodes": command("/dev/sda1 9909760 145378 9764382 2% /\n"),
        "docker_state": command("inactive\n"),
        "docker_system_df": command("bash: docker: command not found\n"),
        "apt_cache_bytes": command("468676608\n"),
        "old_tmp_files": command("count=0 bytes=0\n"),
        "cleanup_path_sizes": command("0\t/tmp\n"),
        "cleanup_status": command("not_configured\n"),
        "sudo_ready": command("no\n"),
    }
    commands.update(overrides)
    return {"commands": commands}


class BackendCleanupMaintenanceTests(unittest.TestCase):
    def test_safe_cleanup_paths_are_allowlist_based(self):
        self.assertTrue(cleanup.safe_cleanup_path("/tmp"))
        self.assertTrue(cleanup.safe_cleanup_path("/tmp/nutsnews-old-file"))
        self.assertTrue(cleanup.safe_cleanup_path("/var/tmp/nutsnews-old-file"))
        self.assertTrue(cleanup.safe_cleanup_path("/var/cache/apt/archives"))
        for path in cleanup.PROTECTED_PATHS:
            self.assertFalse(cleanup.safe_cleanup_path(path), path)
            self.assertFalse(cleanup.safe_cleanup_path(f"{path.rstrip('/')}/child"), path)

    def test_cleanup_commands_never_touch_protected_state(self):
        cleanup.assert_command_safety()
        joined = "\n".join(item.apply_command for item in cleanup.CLEANUP_COMMANDS)
        self.assertNotIn("docker volume prune", joined)
        self.assertNotIn("docker system prune --volumes", joined)
        self.assertNotIn("/var/lib/caddy", joined)
        self.assertNotIn("/var/lib/nutsnews/backups", joined)
        self.assertNotIn("/var/lib/postgresql", joined)

    def test_classify_live_fixture_exposes_disk_inode_and_cleanup_state(self):
        checks, summary = cleanup.classify(evidence())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["root_disk_pressure"]["status"], "healthy")
        self.assertEqual(by_name["root_inode_pressure"]["status"], "healthy")
        self.assertEqual(by_name["docker_cleanup_surface"]["status"], "not_configured")
        self.assertEqual(by_name["stale_temp_file_candidates"]["status"], "healthy")
        self.assertEqual(by_name["apt_package_cache_size"]["status"], "healthy")
        self.assertEqual(by_name["cleanup_last_run"]["status"], "not_configured")
        self.assertEqual(summary["critical"], 0)

    def test_apt_cache_warning_threshold_is_visible_before_critical(self):
        checks, _ = cleanup.classify(evidence(apt_cache_bytes=command(str(800 * 1024 * 1024) + "\n")))
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["apt_package_cache_size"]["status"], "warning")

    def test_workflow_has_fixed_actions_and_confirmed_apply(self):
        workflow = (ROOT / ".github/workflows/backend-cleanup-maintenance.yml").read_text(encoding="utf-8")
        self.assertIn("- report", workflow)
        self.assertIn("- dry-run", workflow)
        self.assertIn("- apply", workflow)
        self.assertIn("confirm_apply", workflow)
        self.assertIn("backend.nutsnews.com", workflow)
        self.assertNotIn("remote_command", workflow)


if __name__ == "__main__":
    unittest.main()
