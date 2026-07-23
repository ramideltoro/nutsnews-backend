#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
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


if __name__ == "__main__":
    unittest.main()
