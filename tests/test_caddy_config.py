#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import unittest


TASK_FILE = Path("ansible/roles/backend_baseline/tasks/caddy.yml")


class CaddyConfigTests(unittest.TestCase):
    def test_http_health_is_separate_from_automatic_https_site(self):
        task = TASK_FILE.read_text(encoding="utf-8")

        self.assertIn("http://{{ backend_domain }} {", task)
        self.assertIn("redir https://{{ backend_domain }}{uri} 308", task)
        self.assertIn("{{ backend_domain }} {", task)
        self.assertNotIn("http://{{ backend_domain }}, https://{{ backend_domain }}", task)


if __name__ == "__main__":
    unittest.main()
