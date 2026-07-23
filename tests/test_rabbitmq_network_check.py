#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETWORK_CHECK_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_network_check.py"
SPEC = importlib.util.spec_from_file_location("nutsnews_rabbitmq_network_check", NETWORK_CHECK_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load network check module from {NETWORK_CHECK_PATH}")
network_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network_check)


class RabbitMQNetworkCheckTests(unittest.TestCase):
    def test_ss_listener_parser_groups_expected_ports(self):
        output = "\n".join(
            [
                "LISTEN 0 4096 127.0.0.1:5672 0.0.0.0:*",
                "LISTEN 0 4096 127.0.0.1:15672 0.0.0.0:*",
                "LISTEN 0 4096 [::1]:15692 [::]:*",
                "LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*",
            ]
        )
        listeners = network_check.parse_ss_listeners(output, {5672, 15672, 15692})
        self.assertEqual(listeners[5672], ["127.0.0.1"])
        self.assertEqual(listeners[15672], ["127.0.0.1"])
        self.assertEqual(listeners[15692], ["::1"])
        self.assertTrue(network_check.is_loopback_host("127.0.0.1"))
        self.assertFalse(network_check.is_loopback_host("0.0.0.0"))

    def test_docker_publish_parser_keeps_only_safe_binding_shape(self):
        published = network_check.parse_docker_ports(
            '{"5672/tcp":[{"HostIp":"127.0.0.1","HostPort":"5672"}],"15672/tcp":null}'
        )
        self.assertEqual(published["5672/tcp"], [{"HostIp": "127.0.0.1", "HostPort": "5672"}])
        self.assertNotIn("15672/tcp", published)

    def test_docker_network_parser_keeps_private_network_shape(self):
        networks = network_check.parse_docker_networks(
            '{"nutsnews-rabbitmq_default":{"IPAddress":"172.19.0.2","Gateway":"172.19.0.1"},"host":{"IPAddress":"","Gateway":""}}'
        )
        self.assertEqual(networks["nutsnews-rabbitmq_default"]["IPAddress"], "172.19.0.2")
        self.assertEqual(networks["host"]["Gateway"], "")

    def test_guest_user_parser_accepts_json_and_plaintext(self):
        self.assertEqual(network_check.parse_rabbitmq_users('[{"user":"admin","tags":["administrator"]}]'), ["admin"])
        self.assertEqual(network_check.parse_rabbitmq_users("Listing users ...\nadmin [administrator]\nmonitoring [monitoring]\n"), ["admin", "monitoring"])

    def test_topology_credential_check_reports_key_names_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temp:
            topology_env = Path(temp) / "topology.env"
            admin_env = Path(temp) / "rabbitmq.env"
            topology_env.write_text(
                "\n".join(
                    [
                        "RABBITMQ_BREAK_GLASS_ADMIN_USERNAME=nutsnews_rabbitmq_admin",
                        "RABBITMQ_BREAK_GLASS_ADMIN_PASSWORD=admin-secret",
                        "RABBITMQ_MONITORING_USERNAME=nutsnews_rabbitmq_monitoring",
                        "RABBITMQ_MONITORING_PASSWORD=duplicate-secret",
                        "RABBITMQ_SCHEDULER_PUBLISHER_USERNAME=nutsnews_scheduler_publisher",
                        "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD=duplicate-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            admin_env.write_text(
                "RABBITMQ_DEFAULT_USER=nutsnews_rabbitmq_admin\nRABBITMQ_DEFAULT_PASS=admin-secret\n",
                encoding="utf-8",
            )

            check = network_check.check_topology_credentials(topology_env, admin_env)

        self.assertEqual(check["status"], "fail")
        self.assertEqual(
            check["duplicate_password_key_groups"],
            [["RABBITMQ_MONITORING_PASSWORD", "RABBITMQ_SCHEDULER_PUBLISHER_PASSWORD"]],
        )
        self.assertNotIn("duplicate-secret", str(check))

    def test_tls_posture_passes_for_loopback_only_exposure(self):
        listener = {"non_loopback": {}}
        docker = {"non_loopback": {}}
        check = network_check.check_tls_posture(listener, docker)
        self.assertEqual(check["status"], "pass")
        self.assertFalse(check["tls_required"])


if __name__ == "__main__":
    unittest.main()
