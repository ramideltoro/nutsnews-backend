#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts import backend_drift_check


BASELINE = {
    "host": "backend",
    "public_tcp_ports": [
        {"port": 22, "address": "0.0.0.0", "purpose": "SSH"},
        {"port": 22, "address": "::", "purpose": "SSH"},
    ],
    "not_deployed": [
        "backend app",
        "Docker Engine",
        "Caddy",
        "PostgreSQL",
        "Redis or Valkey",
    ],
}


def evidence(**overrides):
    commands = {
        "hostname": {"stdout": "backend\n"},
        "failed_units": {"stdout": ""},
        "listeners": {
            "stdout": "\n".join(
                [
                    "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
                    "tcp LISTEN 0 128 [::]:22 [::]:*",
                    "tcp LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*",
                    "udp UNCONN 0 0 127.0.0.1:323 0.0.0.0:*",
                ]
            )
        },
        "sudo_nopasswd": {"stdout": "no\n"},
        "docker_present": {"stdout": "no\n"},
        "docker_active": {"stdout": "inactive\n"},
        "caddy_present": {"stdout": "no\n"},
        "caddy_active": {"stdout": "inactive\n"},
        "postgres_active": {"stdout": "inactive\n"},
        "redis_active": {"stdout": "inactive\n"},
        "backend_units": {"stdout": ""},
        "managed_files": {"stdout": "missing /etc/ssh/sshd_config.d/00-nutsnews-hardening.conf\n"},
    }
    commands.update(overrides)
    return {"commands": commands}


class BackendDriftCheckTests(unittest.TestCase):
    def test_public_listener_parser_ignores_private_and_udp(self):
        ports = backend_drift_check.parse_public_tcp_ports(evidence()["commands"]["listeners"]["stdout"])
        self.assertEqual(ports, [{"address": "0.0.0.0", "port": 22}, {"address": "::", "port": 22}])

    def test_expected_baseline_passes_with_known_missing_apply_files(self):
        checks, summary = backend_drift_check.classify(evidence(), BASELINE)
        self.assertEqual(summary["high_priority_unexpected"], [])
        self.assertGreater(summary["missing"], 0)
        self.assertIn("sudo_nopasswd", {item["surface"] for item in checks})

    def test_unexpected_public_http_is_high_priority(self):
        fixture = evidence(
            listeners={
                "stdout": "\n".join(
                    [
                        "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
                        "tcp LISTEN 0 128 0.0.0.0:80 0.0.0.0:*",
                    ]
                )
            }
        )
        checks, summary = backend_drift_check.classify(fixture, BASELINE)
        self.assertIn("public_tcp_ports", summary["high_priority_unexpected"])
        public_check = next(item for item in checks if item["surface"] == "public_tcp_ports")
        self.assertEqual(public_check["status"], "unexpected")

    def test_failed_units_are_high_priority(self):
        fixture = evidence(failed_units={"stdout": "example.service loaded failed failed Example\n"})
        _, summary = backend_drift_check.classify(fixture, BASELINE)
        self.assertIn("failed_systemd_units", summary["high_priority_unexpected"])

    def test_redaction_removes_tokens_keys_and_url_passwords(self):
        raw = (
            "github_pat_1234567890abcdefghijklmnopqrstuvwxyzABCDEF "
            "postgres://user:secret@example.com/db "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        redacted = backend_drift_check.redact(raw)
        self.assertNotIn("github_pat_", redacted)
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("abc\n-----END", redacted)


if __name__ == "__main__":
    unittest.main()
