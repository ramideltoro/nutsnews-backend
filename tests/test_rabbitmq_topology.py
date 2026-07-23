#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_topology.py"
TEMPLATE_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "templates" / "worker-uplift-topology.json.j2"
SPEC = importlib.util.spec_from_file_location("nutsnews_rabbitmq_topology", TOPOLOGY_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load topology module from {TOPOLOGY_PATH}")
topology = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(topology)


def load_definition() -> dict:
    rendered = TEMPLATE_PATH.read_text(encoding="utf-8").replace("{{ backend_rabbitmq_vhost }}", "nutsnews-worker-uplift")
    return json.loads(rendered)


def credential_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in topology.managed_environment_names():
        if name.endswith("_USERNAME"):
            values[name] = name.lower()
        else:
            values[name] = f"password-for-{name.lower()}"
    values["RABBITMQ_BREAK_GLASS_ADMIN_USERNAME"] = "nutsnews_rabbitmq_admin"
    values["RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD"] = "admin-password"
    return values


class FakeReadOnlyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, payload=None, ignored_statuses=()):
        del payload, ignored_statuses
        self.calls.append((method, path))
        if "/bindings/" in path:
            return []
        return None

    def get_or_none(self, path: str):
        self.calls.append(("GET", path))
        return None


class FakeUserClient:
    def __init__(self, user: dict, permissions: dict | None = None) -> None:
        self.user = user
        self.permissions = permissions or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, ignored_statuses=()):
        del ignored_statuses
        self.calls.append((method, path, payload))
        return None

    def get_or_none(self, path: str):
        self.calls.append(("GET", path, None))
        if "/api/vhosts/" in path:
            return {}
        if "/api/permissions/" in path:
            return self.permissions
        if path.endswith("/guest"):
            return None
        if "/api/users/" in path:
            return self.user
        return None


class RabbitMQTopologyTests(unittest.TestCase):
    def test_definition_builds_exact_contract_topology_with_classic_limits(self):
        definition = load_definition()
        queues = topology.expected_queues(definition)
        bindings = topology.expected_bindings(definition)
        policies = topology.expected_policies(definition)

        self.assertEqual(definition["source"]["contracts_commit"], "396d94dba76e3773ede50783463419501853b107")
        self.assertEqual(definition["queue_type"], "classic")
        self.assertEqual(len(definition["routes"]), 7)
        self.assertEqual(len(queues), 35)
        self.assertEqual(len(bindings), 35)
        self.assertEqual(len(policies), 3)

        fetch_main = next(queue for queue in queues if queue["name"] == "nutsnews.worker.fetch.v1")
        self.assertEqual(fetch_main["arguments"]["x-queue-type"], "classic")
        self.assertEqual(fetch_main["arguments"]["x-max-length"], 2000)
        self.assertEqual(fetch_main["arguments"]["x-overflow"], "reject-publish")
        self.assertEqual(fetch_main["arguments"]["x-dead-letter-exchange"], "nutsnews.worker.dlq.v1")
        self.assertEqual(fetch_main["arguments"]["x-dead-letter-routing-key"], "nutsnews.worker.fetch.v1.dlq")

        fetch_retry = next(queue for queue in queues if queue["name"] == "nutsnews.worker.fetch.v1.retry-30s")
        self.assertEqual(fetch_retry["arguments"]["x-message-ttl"], 30000)
        self.assertEqual(fetch_retry["arguments"]["x-dead-letter-exchange"], "nutsnews.worker.v1")
        self.assertEqual(fetch_retry["arguments"]["x-dead-letter-routing-key"], "nutsnews.worker.fetch.v1")

        fetch_dlq = next(queue for queue in queues if queue["name"] == "nutsnews.worker.fetch.v1.dlq")
        self.assertEqual(fetch_dlq["arguments"]["x-message-ttl"], 1209600000)

    def test_permission_matrix_allows_only_declared_route_access(self):
        definition = load_definition()
        users = topology.user_records(definition, credential_env())

        self.assertEqual(topology.permission_matrix(definition, users), [])
        fetcher_consumer = next(user for user in users if user["id"] == "fetcher_consumer")
        self.assertTrue(topology.regex_allows(fetcher_consumer["permissions"]["read"], "nutsnews.worker.fetch.v1"))
        self.assertFalse(topology.regex_allows(fetcher_consumer["permissions"]["read"], "nutsnews.worker.translation.v1"))
        self.assertTrue(topology.regex_allows(fetcher_consumer["permissions"]["write"], "nutsnews.worker.retry.v1"))
        self.assertTrue(topology.regex_allows(fetcher_consumer["permissions"]["write"], "nutsnews.worker.dlq.v1"))
        self.assertFalse(topology.regex_allows(fetcher_consumer["permissions"]["write"], "nutsnews.worker.v1"))

        scheduler = next(user for user in users if user["id"] == "scheduler_publisher")
        self.assertTrue(topology.regex_allows(scheduler["permissions"]["write"], "nutsnews.worker.v1"))
        self.assertFalse(topology.regex_allows(scheduler["permissions"]["write"], "nutsnews.worker.retry.v1"))
        self.assertFalse(topology.regex_allows(scheduler["permissions"]["read"], "nutsnews.worker.fetch.v1"))

    def test_live_drift_check_is_read_only(self):
        definition = load_definition()
        users = topology.user_records(definition, credential_env())
        client = FakeReadOnlyClient()

        drift = topology.live_drift(client, definition, users)

        self.assertIn("missing_vhost", drift)
        self.assertTrue(drift)
        self.assertTrue(client.calls)
        self.assertEqual({method for method, _ in client.calls}, {"GET"})

    def test_ensure_user_accepts_management_api_list_tags(self):
        report = {"changed": False, "changes": []}
        user = {
            "id": "break_glass_admin",
            "username": "nutsnews_rabbitmq_admin",
            "password": "admin-password",
            "tags": ["administrator"],
        }
        client = FakeUserClient({"tags": ["administrator"]})

        topology.ensure_user(client, user, report, rotate_passwords=False)

        self.assertFalse(report["changed"])
        self.assertNotIn("PUT", {method for method, _, _ in client.calls})

    def test_live_drift_accepts_management_api_list_tags(self):
        user = {
            "id": "break_glass_admin",
            "username": "nutsnews_rabbitmq_admin",
            "tags": ["administrator"],
            "permissions": {"configure": ".*", "write": ".*", "read": ".*"},
        }
        definition = {"vhost": "nutsnews-worker-uplift", "exchanges": []}
        client = FakeUserClient({"tags": ["administrator"]}, user["permissions"])

        with (
            patch.object(topology, "expected_queues", return_value=[]),
            patch.object(topology, "expected_bindings", return_value=[]),
            patch.object(topology, "expected_policies", return_value=[]),
        ):
            drift = topology.live_drift(client, definition, [user])

        self.assertEqual(drift, [])

    def test_transfer_probe_refuses_nonempty_stage_queues(self):
        definition = load_definition()
        users = topology.user_records(definition, credential_env())
        args = SimpleNamespace(timeout_seconds=1)

        with (
            patch.object(topology, "wait_for_management", return_value={}),
            patch.object(topology, "queue_message_count", return_value=1),
        ):
            with self.assertRaises(SystemExit) as raised:
                topology.action_probe_transfers(args, object(), definition, users)
        self.assertIn("refusing transfer probe because queue is non-empty", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
