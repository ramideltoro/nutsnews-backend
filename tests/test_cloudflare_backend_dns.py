#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts import cloudflare_backend_dns


DESIRED = cloudflare_backend_dns.DesiredRecord(
    name="backend.nutsnews.com",
    content="65.75.201.18",
    proxied=False,
    ttl=300,
)


class CloudflareBackendDnsTests(unittest.TestCase):
    def test_check_plan_creates_missing_record(self):
        plan = cloudflare_backend_dns.classify_plan([], DESIRED, "check")
        self.assertEqual(plan["action"], "create")
        self.assertFalse(plan["proxied"])

    def test_check_plan_noops_matching_record(self):
        plan = cloudflare_backend_dns.classify_plan(
            [
                {
                    "id": "record-id",
                    "type": "A",
                    "name": "backend.nutsnews.com",
                    "content": "65.75.201.18",
                    "proxied": False,
                    "ttl": 300,
                }
            ],
            DESIRED,
            "check",
        )
        self.assertEqual(plan["action"], "noop")

    def test_check_plan_updates_mismatched_record(self):
        plan = cloudflare_backend_dns.classify_plan(
            [
                {
                    "id": "record-id",
                    "type": "A",
                    "name": "backend.nutsnews.com",
                    "content": "192.0.2.10",
                    "proxied": True,
                    "ttl": 1,
                }
            ],
            DESIRED,
            "check",
        )
        self.assertEqual(plan["action"], "update")
        self.assertEqual(plan["existing"]["content"], "192.0.2.10")

    def test_multiple_records_block_mutation(self):
        plan = cloudflare_backend_dns.classify_plan(
            [
                {"id": "one", "type": "A", "name": "backend.nutsnews.com"},
                {"id": "two", "type": "A", "name": "backend.nutsnews.com"},
            ],
            DESIRED,
            "apply",
        )
        self.assertEqual(plan["action"], "blocked")

    def test_rollback_deletes_existing_record(self):
        plan = cloudflare_backend_dns.classify_plan(
            [
                {
                    "id": "record-id",
                    "type": "A",
                    "name": "backend.nutsnews.com",
                    "content": "65.75.201.18",
                    "proxied": False,
                    "ttl": 300,
                }
            ],
            DESIRED,
            "rollback",
        )
        self.assertEqual(plan["action"], "delete")


if __name__ == "__main__":
    unittest.main()
