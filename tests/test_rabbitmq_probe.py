#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = ROOT / "ansible" / "roles" / "backend_rabbitmq" / "files" / "nutsnews_rabbitmq_probe.py"
SPEC = importlib.util.spec_from_file_location("nutsnews_rabbitmq_probe", PROBE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load probe module from {PROBE_PATH}")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class RabbitMQProbeTests(unittest.TestCase):
    def test_publish_deletes_existing_probe_queue_before_declaring(self):
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):
            calls.append(kwargs)
            if kwargs["path"].endswith("/publish"):
                return {"routed": True}
            return None

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env_path = temp / "rabbitmq.env"
            state_path = temp / "state.json"
            env_path.write_text(
                "\n".join(
                    [
                        "RABBITMQ_DEFAULT_USER=admin",
                        "RABBITMQ_DEFAULT_PASS=not-a-real-password",
                        "RABBITMQ_DEFAULT_VHOST=nutsnews-worker-uplift",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                env=env_path,
                management_url="http://127.0.0.1:15672",
                queue="worker.uplift.probe.host-restart",
                state=state_path,
            )
            with (
                patch.object(probe, "wait_for_management", return_value={}),
                patch.object(probe, "request_json", side_effect=fake_request_json),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.action_publish(args), 0)

        self.assertEqual([call["method"] for call in calls], ["DELETE", "PUT", "POST"])
        self.assertEqual(calls[0]["path"], "/api/queues/nutsnews-worker-uplift/worker.uplift.probe.host-restart")
        self.assertEqual(calls[0]["ignored_statuses"], (404,))
        self.assertEqual(calls[1]["payload"]["arguments"]["x-max-length"], 10)

    def test_publish_refuses_non_probe_queue_name(self):
        args = SimpleNamespace(queue="worker.production.fetch")
        with self.assertRaises(SystemExit) as raised:
            probe.action_publish(args)
        self.assertIn("refusing to mutate non-probe RabbitMQ queue", str(raised.exception))

    def test_request_json_ignores_configured_http_status(self):
        error = urllib.error.HTTPError(
            url="http://127.0.0.1:15672/api/queues/vhost/name",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"error":"not_found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            result = probe.request_json(
                base_url="http://127.0.0.1:15672",
                username="admin",
                password="not-a-real-password",
                method="DELETE",
                path="/api/queues/vhost/name",
                ignored_statuses=(404,),
            )
        self.assertIsNone(result)

    def test_request_json_reports_http_error_body_without_traceback_dependency(self):
        error = urllib.error.HTTPError(
            url="http://127.0.0.1:15672/api/queues/vhost/name",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"reason":"inequivalent arg x-max-length"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                probe.request_json(
                    base_url="http://127.0.0.1:15672",
                    username="admin",
                    password="not-a-real-password",
                    method="PUT",
                    path="/api/queues/vhost/name",
                )
        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("inequivalent arg x-max-length", message)

    def test_smoke_covers_publish_ack_retry_dlq_restart_and_permission_denial(self):
        published_ids: list[str] = []

        def fake_publish(*call_args, **_kwargs):
            published_ids.append(call_args[6])
            return True

        def fake_get(*call_args, **_kwargs):
            ackmode = call_args[5]
            if ackmode == "ack_requeue_true":
                return {"message_id": published_ids[0]}
            if ackmode == "reject_requeue_false":
                return {"message_id": published_ids[2]}
            return None

        def fake_request_json(**kwargs):
            if kwargs.get("username") == "monitor" and kwargs.get("method") == "POST":
                raise RuntimeError(
                    "RabbitMQ management API POST /api/exchanges/nutsnews-worker-uplift/probe/publish "
                    "returned HTTP 400: {\"error\":\"bad_request\",\"reason\":\"403 ACCESS_REFUSED - write access refused\"}"
                )
            return None

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env_path = temp / "rabbitmq.env"
            topology_env = temp / "topology.env"
            output_path = temp / "smoke.json"
            env_path.write_text(
                "RABBITMQ_DEFAULT_USER=admin\n"
                "RABBITMQ_DEFAULT_PASS=not-a-real-password\n"
                "RABBITMQ_DEFAULT_VHOST=nutsnews-worker-uplift\n",
                encoding="utf-8",
            )
            topology_env.write_text(
                "RABBITMQ_MONITORING_USERNAME=monitor\n"
                "RABBITMQ_MONITORING_PASSWORD=not-a-real-monitor-password\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                env=env_path,
                credentials_env=topology_env,
                management_url="http://127.0.0.1:15672",
                retry_ttl_ms=10,
                timeout_seconds=1,
                restart_timeout_seconds=1,
                restart_service="nutsnews-rabbitmq.service",
                skip_restart=False,
                output=output_path,
            )
            with (
                patch.object(probe, "wait_for_management", return_value={}),
                patch.object(probe, "publish_message", side_effect=fake_publish),
                patch.object(probe, "get_message", side_effect=fake_get),
                patch.object(probe, "wait_for_message", return_value=True),
                patch.object(probe, "completed_process", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                patch.object(probe, "request_json", side_effect=fake_request_json),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.action_smoke(args), 0)

            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                {check["name"] for check in report["checks"]},
                {"publish_confirm", "consume_manual_ack", "retry", "dlq", "restart_persistence", "permission_denial"},
            )
            self.assertTrue(report["resource_prefix"].startswith("worker.uplift.probe.smoke."))
            self.assertNotIn("not-a-real", json.dumps(report))

    def test_drift_reports_expected_rabbitmq_surfaces(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env_path = temp / "rabbitmq.env"
            metadata_path = temp / "metadata.json"
            config_path = temp / "compose.yml"
            env_path.write_text(
                "RABBITMQ_DEFAULT_USER=admin\n"
                "RABBITMQ_DEFAULT_PASS=not-a-real-password\n"
                "RABBITMQ_DEFAULT_VHOST=nutsnews-worker-uplift\n",
                encoding="utf-8",
            )
            config_path.write_text("image: rabbitmq@sha256:abc\n", encoding="utf-8")
            metadata_path.write_text(
                json.dumps(
                    {
                        "image": "rabbitmq@sha256:abc",
                        "paths": {"compose": str(config_path)},
                        "checksums": {"compose": probe.sha256_file(config_path)},
                    }
                ),
                encoding="utf-8",
            )

            def fake_completed(command, _timeout):
                text = " ".join(str(part) for part in command)
                if command[:2] == ["docker", "inspect"]:
                    self.assertEqual(command[-1], "{{.Config.Image}} {{.Image}}")
                    return {"returncode": 0, "stdout": "rabbitmq@sha256:abc sha256:image-id\n", "stderr": ""}
                if command[:3] == ["docker", "image", "inspect"]:
                    return {"returncode": 0, "stdout": "", "stderr": "image inspect should not be needed"}
                if "nutsnews-rabbitmq-topology" in text:
                    return {"returncode": 0, "stdout": '{"status":"pass","drift":[]}\n', "stderr": ""}
                if "nutsnews-rabbitmq-network-check" in text:
                    return {"returncode": 0, "stdout": '{"status":"pass","failed_checks":[]}\n', "stderr": ""}
                if "nutsnews-backup" in text:
                    return {
                        "returncode": 0,
                        "stdout": (
                            '{"backup":{"status":"healthy"},'
                            '"rabbitmq_recovery":{'
                            '"definition_export":{"status":"healthy"},'
                            '"clean_rebuild_drill":{"status":"healthy"}}}\n'
                        ),
                        "stderr": "",
                    }
                return {"returncode": 1, "stdout": "", "stderr": "unexpected command"}

            args = SimpleNamespace(
                env=env_path,
                metadata=metadata_path,
                container_name="nutsnews-rabbitmq",
                timeout_seconds=1,
                topology_path=Path("/usr/local/sbin/nutsnews-rabbitmq-topology"),
                credentials_env=Path("/etc/nutsnews-rabbitmq/topology.env"),
                definition=Path("/etc/nutsnews-rabbitmq/worker-uplift-topology.json"),
                network_check_path=Path("/usr/local/sbin/nutsnews-rabbitmq-network-check"),
                backup_path=Path("/usr/local/sbin/nutsnews-backup"),
                management_url="http://127.0.0.1:15672",
            )
            output = io.StringIO()
            with (
                patch.object(probe, "wait_for_management", return_value={"rabbitmq_version": "4.3.3"}),
                patch.object(probe, "completed_process", side_effect=fake_completed),
                redirect_stdout(output),
            ):
                self.assertEqual(probe.action_drift(args), 0)

            report = json.loads(output.getvalue())
            surfaces = {check["surface"] for check in report["checks"]}
            self.assertEqual(report["status"], "pass")
            self.assertIn("rabbitmq_image_digest", surfaces)
            self.assertIn("rabbitmq_config_checksum:compose", surfaces)
            self.assertIn("rabbitmq_topology", surfaces)
            self.assertIn("rabbitmq_permissions_metadata", surfaces)
            self.assertIn("rabbitmq_listeners_network", surfaces)
            self.assertIn("rabbitmq_backup_freshness", surfaces)

    def test_canary_writes_redacted_report_and_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env_path = temp / "rabbitmq.env"
            topology_env = temp / "topology.env"
            definition = temp / "topology.json"
            report_path = temp / "last-canary.json"
            metrics_path = temp / "rabbitmq-canary.prom"
            env_path.write_text(
                "RABBITMQ_DEFAULT_USER=admin\n"
                "RABBITMQ_DEFAULT_PASS=not-a-real-admin-password\n"
                "RABBITMQ_DEFAULT_VHOST=nutsnews-worker-uplift\n",
                encoding="utf-8",
            )
            topology_env.write_text(
                "RABBITMQ_MONITORING_USERNAME=monitor\n"
                "RABBITMQ_MONITORING_PASSWORD=not-a-real-monitor-password\n",
                encoding="utf-8",
            )
            definition.write_text(
                json.dumps(
                    {
                        "exchanges": [{"id": "canary", "name": "worker.uplift.canary.v4"}],
                        "canary": {
                            "exchange_id": "canary",
                            "routing_key": "worker.uplift.canary.v4",
                            "queue": {"name": "worker.uplift.canary.runtime.v4", "runtime_declared": True},
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                env=env_path,
                credentials_env=topology_env,
                definition=definition,
                output=report_path,
                metrics_output=metrics_path,
                failure_mode="none",
                amqp_host="127.0.0.1",
                amqp_port=5672,
                failure_amqp_port=9,
                timeout_seconds=1,
            )
            with (
                patch.object(
                    probe,
                    "amqp_canary_roundtrip",
                    return_value={
                        "expected_failure": False,
                        "failure_class": "none",
                        "message_id": "probe-message-id",
                        "latency_seconds": 0.25,
                        "message_age_seconds": 0.5,
                    },
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.action_canary(args), 0)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            metrics = metrics_path.read_text(encoding="utf-8")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["failure_class"], "none")
            self.assertIn("nutsnews_backend_rabbitmq_canary_success", metrics)
            self.assertIn('failure_mode="none"', metrics)
            self.assertNotIn("not-a-real", json.dumps(report) + metrics)

    def test_canary_drains_stale_canary_messages_before_publish(self):
        class FakeMethod:
            def __init__(self, delivery_tag: int) -> None:
                self.delivery_tag = delivery_tag

        class FakeBasicProperties:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class FakeChannel:
            def __init__(self) -> None:
                self.messages = [
                    b'{"probe":"nutsnews-rabbitmq-canary","message_id":"stale-1"}',
                    b'{"probe":"nutsnews-rabbitmq-canary","message_id":"stale-2"}',
                ]
                self.acked: list[int] = []
                self.published = 0

            def confirm_delivery(self) -> None:
                return None

            def basic_get(self, queue: str, auto_ack: bool = False):
                self.last_queue = queue
                self.last_auto_ack = auto_ack
                if not self.messages:
                    return None, None, None
                delivery_tag = len(self.acked) + 1
                return FakeMethod(delivery_tag), None, self.messages.pop(0)

            def basic_ack(self, delivery_tag: int) -> None:
                self.acked.append(delivery_tag)

            def basic_publish(self, *, exchange: str, routing_key: str, body: bytes, properties, mandatory: bool) -> None:
                self.last_publish = {
                    "exchange": exchange,
                    "routing_key": routing_key,
                    "properties": properties,
                    "mandatory": mandatory,
                }
                self.published += 1
                self.messages.append(body)

            def queue_declare(self, *, queue: str, durable: bool, exclusive: bool, auto_delete: bool, arguments: dict) -> None:
                self.last_declare = {
                    "queue": queue,
                    "durable": durable,
                    "exclusive": exclusive,
                    "auto_delete": auto_delete,
                    "arguments": arguments,
                }

            def queue_bind(self, *, queue: str, exchange: str, routing_key: str) -> None:
                self.last_bind = {"queue": queue, "exchange": exchange, "routing_key": routing_key}

        class FakeConnection:
            def __init__(self) -> None:
                self.channel_instance = FakeChannel()
                self.closed = False

            def channel(self) -> FakeChannel:
                return self.channel_instance

            def close(self) -> None:
                self.closed = True

        class FakePika:
            BasicProperties = FakeBasicProperties

            class exceptions:
                class UnroutableError(Exception):
                    pass

                class NackError(Exception):
                    pass

            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def BlockingConnection(self, _parameters):
                return self.connection

        fake_connection = FakeConnection()
        fake_pika = FakePika(fake_connection)
        args = SimpleNamespace(amqp_host="127.0.0.1", amqp_port=5672, timeout_seconds=1)
        route = {
            "exchange": "worker.uplift.canary.v4",
            "routing_key": "worker.uplift.canary.v4",
            "queue": "worker.uplift.canary.runtime.v4",
            "runtime_declared": True,
            "durable": False,
            "exclusive": True,
            "auto_delete": True,
            "arguments": {"x-max-length": 10},
        }

        with (
            patch.object(probe, "import_pika", return_value=fake_pika),
            patch.object(probe, "amqp_connection_parameters", return_value=object()),
        ):
            result = probe.amqp_canary_roundtrip(
                args,
                username="monitor",
                password="not-a-real-monitor-password",
                vhost="nutsnews-worker-uplift",
                route=route,
                failure_mode="none",
            )

        self.assertFalse(result["expected_failure"])
        self.assertEqual(result["preflight_drained"], 2)
        self.assertEqual(result["cleanup_drained"], 0)
        self.assertEqual(fake_connection.channel_instance.published, 1)
        self.assertEqual(fake_connection.channel_instance.acked, [1, 2, 3])
        self.assertEqual(fake_connection.channel_instance.last_declare["queue"], "worker.uplift.canary.runtime.v4")
        self.assertFalse(fake_connection.channel_instance.last_declare["durable"])
        self.assertTrue(fake_connection.channel_instance.last_declare["exclusive"])
        self.assertTrue(fake_connection.channel_instance.last_declare["auto_delete"])
        self.assertEqual(fake_connection.channel_instance.last_bind["exchange"], "worker.uplift.canary.v4")
        self.assertEqual(fake_connection.channel_instance.last_publish["properties"].kwargs["delivery_mode"], 1)
        self.assertTrue(fake_connection.closed)

    def test_canary_failure_fixture_returns_expected_failure_metric(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            args = SimpleNamespace(
                output=temp / "last-canary.json",
                metrics_output=temp / "rabbitmq-canary.prom",
                failure_mode="disk-watermark",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(probe.action_canary(args), 0)

            report = json.loads(args.output.read_text(encoding="utf-8"))
            metrics = args.metrics_output.read_text(encoding="utf-8")
            self.assertEqual(report["status"], "expected_failure")
            self.assertEqual(report["failure_class"], "disk-watermark")
            self.assertIn('failure_class="disk-watermark"', metrics)

    def test_restart_drill_retries_post_restart_canary_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            args = SimpleNamespace(
                drill="restart",
                output=temp / "last-canary-drill.json",
                restart_service="nutsnews-rabbitmq.service",
                restart_timeout_seconds=1,
                restart_readiness_attempts=3,
                restart_readiness_interval_seconds=0,
            )
            with (
                patch.object(
                    probe,
                    "build_canary_report",
                    side_effect=[
                        {"status": "pass"},
                        {"status": "fail", "failure_class": "none"},
                        {"status": "pass", "failure_class": "none"},
                    ],
                ),
                patch.object(probe, "completed_process", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                patch.object(probe.time, "sleep"),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.action_drill(args), 0)

            report = json.loads(args.output.read_text(encoding="utf-8"))
            after = next(step for step in report["steps"] if step["name"] == "after_restart_canary")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(after["status"], "pass")
            self.assertEqual([attempt["status"] for attempt in after["attempts"]], ["fail", "pass"])


if __name__ == "__main__":
    unittest.main()
