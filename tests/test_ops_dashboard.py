#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


COLLECTOR_PATH = Path("ansible/roles/backend_baseline/files/ops_dashboard_collector.py")
ASSET_DIR = Path("ansible/roles/backend_baseline/files/ops-dashboard")


def load_collector():
    spec = importlib.util.spec_from_file_location("ops_dashboard_collector", COLLECTOR_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OpsDashboardTests(unittest.TestCase):
    def test_redaction_removes_sensitive_values(self):
        collector = load_collector()
        raw = (
            "github_pat_1234567890abcdefghijklmnopqrstuvwxyzABCDEF "
            "postgres://user:secret@example.com/db "
            "person@example.com "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        redacted = collector.redact(raw)
        self.assertNotIn("github_pat_", redacted)
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("person@example.com", redacted)
        self.assertNotIn("abc\n-----END", redacted)

    def test_public_listener_parser_omits_loopback_and_udp(self):
        collector = load_collector()
        listeners = collector.public_listeners(
            "\n".join(
                [
                    "tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*",
                    "tcp LISTEN 0 4096 127.0.0.1:8081 0.0.0.0:*",
                    "udp UNCONN 0 0 0.0.0.0:443 0.0.0.0:*",
                    "tcp LISTEN 0 4096 *:443 *:*",
                ]
            )
        )
        self.assertEqual(listeners, [{"address": "0.0.0.0", "port": 22}, {"address": "*", "port": 443}])

    def test_static_dashboard_is_read_only(self):
        combined = "\n".join(path.read_text(encoding="utf-8") for path in ASSET_DIR.iterdir() if path.is_file())
        lowered = combined.lower()
        self.assertNotIn("<button", lowered)
        self.assertNotIn("<form", lowered)
        self.assertNotIn("exec", lowered)
        self.assertIn('fetch("status.json"', combined)


if __name__ == "__main__":
    unittest.main()
