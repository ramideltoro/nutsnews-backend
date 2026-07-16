#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import unittest


TASK_FILE = Path("ansible/roles/backend_baseline/tasks/caddy.yml")


class CaddyConfigTests(unittest.TestCase):
    def test_backend_site_uses_caddy_automatic_https(self):
        task = TASK_FILE.read_text(encoding="utf-8")

        self.assertIn("{{ backend_domain }} {", task)
        self.assertNotIn("http://{{ backend_domain }} {", task)
        self.assertNotIn("https://{{ backend_domain }} {", task)
        self.assertNotIn("http://{{ backend_domain }}, https://{{ backend_domain }}", task)

    def test_ops_dashboard_route_is_loopback_only(self):
        task = TASK_FILE.read_text(encoding="utf-8")

        self.assertIn("http://{{ backend_ops_dashboard_bind }}:{{ backend_ops_dashboard_port }}", task)
        self.assertIn("bind {{ backend_ops_dashboard_bind }}", task)
        self.assertIn("root * {{ backend_ops_dashboard_public_dir }}", task)
        self.assertNotIn("ops-dashboard", task)


if __name__ == "__main__":
    unittest.main()
