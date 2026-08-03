#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import http.client
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.backend_wiki_ai_smoke import EXPECTED_TOOL_VALUE, health_url, tool_call_is_valid


ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "ansible/roles/backend_wiki_ai/files/nutsnews_wiki_ai_proxy.py"
SPEC = importlib.util.spec_from_file_location("nutsnews_wiki_ai_proxy", PROXY_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)


class FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/tags":
            self.send_error(404)
            return
        self._reply(
            200,
            {
                "models": [
                    {
                        "name": "nutsnews-wiki-qwen:latest",
                        "model": "nutsnews-wiki-qwen:latest",
                    }
                ]
            },
        )

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers["content-length"])
        payload = json.loads(self.rfile.read(length))
        if payload.get("input") == "slow-stream":
            self.server.slow_request_started.set()
            self.server.slow_request_release.wait(timeout=5)
            body = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if payload.get("input") == "slow-stream-body":
            body = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            self.server.slow_body_started.set()
            self.server.slow_body_release.wait(timeout=5)
            self.wfile.write(body)
            return
        self._reply(
            200,
            {
                "model": payload["model"],
                "output": [
                    {
                        "type": "function_call",
                        "name": "echo_value",
                        "arguments": json.dumps({"value": EXPECTED_TOOL_VALUE}),
                    }
                ],
            },
        )

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_arguments):
        return


class BackendWikiAITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
        cls.upstream.slow_request_started = threading.Event()
        cls.upstream.slow_request_release = threading.Event()
        cls.upstream.slow_body_started = threading.Event()
        cls.upstream.slow_body_release = threading.Event()
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.key = "synthetic-wiki-ai-key-with-at-least-32-characters"
        cls.config = proxy.ProxyConfig(
            bind="127.0.0.1",
            port=18089,
            api_key=cls.key,
            model="nutsnews-wiki-qwen",
            max_request_bytes=4096,
            upstream_host="127.0.0.1",
            upstream_port=cls.upstream.server_address[1],
            upstream_timeout_seconds=30,
            queue_wait_seconds=2,
            heartbeat_interval_seconds=1,
        )
        cls.server = proxy.WikiAIHTTPServer(("127.0.0.1", 0), cls.config)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def test_proxy_config_requires_loopback_and_dedicated_key(self):
        base = {
            "NUTSNEWS_WIKI_AI_API_KEY": self.key,
            "NUTSNEWS_WIKI_AI_MODEL": "nutsnews-wiki-qwen",
        }
        self.assertEqual(proxy.load_config(base).bind, "127.0.0.1")
        self.assertEqual(proxy.load_config(base).queue_wait_seconds, 600)
        self.assertEqual(proxy.load_config(base).heartbeat_interval_seconds, 15)
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            proxy.load_config({**base, "NUTSNEWS_WIKI_AI_BIND": "0.0.0.0"})
        with self.assertRaisesRegex(RuntimeError, "at least 32"):
            proxy.load_config({**base, "NUTSNEWS_WIKI_AI_API_KEY": "short"})
        with self.assertRaisesRegex(RuntimeError, "between 1 and 900"):
            proxy.load_config({**base, "NUTSNEWS_WIKI_AI_QUEUE_WAIT_SECONDS": "901"})
        with self.assertRaisesRegex(RuntimeError, "between 1 and 60"):
            proxy.load_config({**base, "NUTSNEWS_WIKI_AI_HEARTBEAT_INTERVAL_SECONDS": "0"})

    def test_public_health_is_bounded_and_model_aware(self):
        with urllib.request.urlopen(f"{self.origin}/health", timeout=5) as response:
            payload = json.load(response)
        self.assertEqual(
            payload,
            {"ok": True, "service": "nutsnews-wiki-ai", "model_ready": True},
        )
        self.assertIn(
            "nutsnews-wiki-qwen",
            proxy.model_names_from_tags({"models": [{"name": "nutsnews-wiki-qwen:latest"}]}),
        )

    def test_responses_proxy_rejects_missing_authentication(self):
        request = urllib.request.Request(
            f"{self.origin}/v1/responses",
            method="POST",
            data=b'{"model":"nutsnews-wiki-qwen","input":"test"}',
            headers={"content-type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        try:
            self.assertEqual(caught.exception.code, 401)
        finally:
            caught.exception.close()

    def test_responses_proxy_rejects_unapproved_model(self):
        request = urllib.request.Request(
            f"{self.origin}/v1/responses",
            method="POST",
            data=b'{"model":"other","input":"test"}',
            headers={
                "authorization": f"Bearer {self.key}",
                "content-type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        try:
            self.assertEqual(caught.exception.code, 400)
        finally:
            caught.exception.close()

    def test_responses_proxy_relays_required_tool_call(self):
        body = json.dumps({"model": "nutsnews-wiki-qwen", "input": "test"}).encode()
        request = urllib.request.Request(
            f"{self.origin}/v1/responses",
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.key}",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        self.assertTrue(tool_call_is_valid(payload))

    def test_streaming_proxy_sends_heartbeat_before_slow_upstream_headers(self):
        self.upstream.slow_request_started.clear()
        self.upstream.slow_request_release.clear()
        body = json.dumps({
            "model": "nutsnews-wiki-qwen",
            "input": "slow-stream",
            "stream": True,
        }).encode()
        request = urllib.request.Request(
            f"{self.origin}/v1/responses",
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.key}",
                "content-type": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.readline(), b": keep-alive\n")
                self.assertEqual(response.readline(), b"\n")
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(self.upstream.slow_request_started.wait(timeout=1))
                heartbeat_started = time.monotonic()
                self.assertEqual(response.readline(), b": keep-alive\n")
                self.assertGreater(time.monotonic() - heartbeat_started, 0.5)
                self.assertEqual(response.readline(), b"\n")
                self.upstream.slow_request_release.set()
                self.assertIn(b"response.completed", response.read())
        finally:
            self.upstream.slow_request_release.set()

    def test_streaming_proxy_keeps_heartbeat_during_slow_upstream_body(self):
        self.upstream.slow_body_started.clear()
        self.upstream.slow_body_release.clear()
        body = json.dumps({
            "model": "nutsnews-wiki-qwen",
            "input": "slow-stream-body",
            "stream": True,
        }).encode()
        request = urllib.request.Request(
            f"{self.origin}/v1/responses",
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.key}",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.readline(), b": keep-alive\n")
                self.assertEqual(response.readline(), b"\n")
                self.assertTrue(self.upstream.slow_body_started.wait(timeout=1))
                heartbeat_started = time.monotonic()
                self.assertEqual(response.readline(), b": keep-alive\n")
                self.assertGreater(time.monotonic() - heartbeat_started, 0.5)
                self.assertEqual(response.readline(), b"\n")
                self.upstream.slow_body_release.set()
                self.assertIn(b"response.completed", response.read())
        finally:
            self.upstream.slow_body_release.set()

    def test_streaming_proxy_releases_inference_when_client_disconnects(self):
        self.upstream.slow_request_started.clear()
        self.upstream.slow_request_release.clear()
        body = json.dumps({
            "model": "nutsnews-wiki-qwen",
            "input": "slow-stream",
            "stream": True,
        }).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        acquired_after_disconnect = False
        try:
            connection.request(
                "POST",
                "/v1/responses",
                body=body,
                headers={
                    "authorization": f"Bearer {self.key}",
                    "content-type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.readline(), b": keep-alive\n")
            self.assertEqual(response.readline(), b"\n")
            self.assertTrue(self.upstream.slow_request_started.wait(timeout=1))
            downstream_socket = response.fp.raw._sock
            self.assertIsInstance(downstream_socket, socket.socket)
            downstream_socket.shutdown(socket.SHUT_RDWR)
            response.close()
            connection.close()
            acquired_after_disconnect = self.server.inference_slot.acquire(timeout=4)
            self.assertTrue(acquired_after_disconnect)
        finally:
            if acquired_after_disconnect:
                self.server.inference_slot.release()
            self.upstream.slow_request_release.set()
            connection.close()

    def test_responses_proxy_allows_one_bounded_waiter_and_rejects_more(self):
        self.assertTrue(self.server.inference_slot.acquire(blocking=False))
        outcome = {}

        def request_as_waiter():
            try:
                body = json.dumps({"model": "nutsnews-wiki-qwen", "input": "wait"}).encode()
                request = urllib.request.Request(
                    f"{self.origin}/v1/responses",
                    method="POST",
                    data=body,
                    headers={
                        "authorization": f"Bearer {self.key}",
                        "content-type": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    outcome["payload"] = json.load(response)
            except Exception as error:  # pragma: no cover - reported by the assertion below
                outcome["error"] = error

        waiter = threading.Thread(target=request_as_waiter, daemon=True)
        waiter.start()
        try:
            deadline = time.monotonic() + 1
            while self.server.waiter_slot._value != 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.server.waiter_slot._value, 0)

            body = json.dumps({"model": "nutsnews-wiki-qwen", "input": "overflow"}).encode()
            overflow = urllib.request.Request(
                f"{self.origin}/v1/responses",
                method="POST",
                data=body,
                headers={
                    "authorization": f"Bearer {self.key}",
                    "content-type": "application/json",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(overflow, timeout=5)
            try:
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(caught.exception.headers["Retry-After"], "30")
            finally:
                caught.exception.close()
        finally:
            self.server.inference_slot.release()
            waiter.join(timeout=5)

        self.assertFalse(waiter.is_alive())
        self.assertNotIn("error", outcome)
        self.assertTrue(tool_call_is_valid(outcome["payload"]))

    def test_smoke_contract_helpers_are_fail_closed(self):
        self.assertEqual(
            health_url("https://backend.nutsnews.com/wiki-ai/v1/responses"),
            "https://backend.nutsnews.com/wiki-ai/health",
        )
        self.assertFalse(tool_call_is_valid({"output": [{"type": "message"}]}))
        self.assertTrue(tool_call_is_valid({
            "output": [{
                "type": "function_call",
                "name": "echo_value",
                "arguments": {"value": EXPECTED_TOOL_VALUE},
            }],
        }))

    def test_ansible_and_workflow_preserve_runtime_boundaries(self):
        defaults = (ROOT / "ansible/roles/backend_wiki_ai/defaults/main.yml").read_text()
        tasks = (ROOT / "ansible/roles/backend_wiki_ai/tasks/main.yml").read_text()
        ollama_unit = (ROOT / "ansible/roles/backend_wiki_ai/templates/ollama.service.j2").read_text()
        proxy_unit = (ROOT / "ansible/roles/backend_wiki_ai/templates/proxy.service.j2").read_text()
        caddy = (ROOT / "ansible/roles/backend_baseline/tasks/caddy.yml").read_text()
        playbook = (ROOT / "ansible/playbooks/bootstrap.yml").read_text()
        workflow = (ROOT / ".github/workflows/protected-backend-ansible-apply.yml").read_text()
        self.assertIn('backend_wiki_ai_version: "0.32.5"', defaults)
        self.assertIn("sha256:f7d6bdbcf71b83aa8670c4e7dc4b6936c0952fcf8b114eaf6a11cbadb9684214", defaults)
        self.assertIn("backend_wiki_ai_base_model: qwen3.5:4b-q4_K_M", defaults)
        self.assertIn("backend_wiki_ai_base_model_id: 2a654d98e6fb", defaults)
        self.assertIn("backend_wiki_ai_context_length: 49152", defaults)
        self.assertIn("backend_wiki_ai_max_output_tokens: 6144", defaults)
        self.assertIn("backend_wiki_ai_queue_wait_seconds: 600", defaults)
        self.assertIn("backend_wiki_ai_heartbeat_interval_seconds: 15", defaults)
        self.assertIn("checksum: \"{{ backend_wiki_ai_archive_checksum }}\"", tasks)
        self.assertIn("not ansible_check_mode", tasks)
        self.assertIn("OLLAMA_NUM_PARALLEL=1", ollama_unit)
        self.assertIn("OLLAMA_MAX_QUEUE=1", ollama_unit)
        self.assertIn("MemoryMax={{ backend_wiki_ai_memory_max }}", ollama_unit)
        self.assertIn("EnvironmentFile={{ backend_wiki_ai_config_dir }}/proxy.env", proxy_unit)
        self.assertIn("NoNewPrivileges=true", proxy_unit)
        self.assertIn("NUTSNEWS_WIKI_AI_QUEUE_WAIT_SECONDS={{ backend_wiki_ai_queue_wait_seconds }}", tasks)
        self.assertIn(
            "NUTSNEWS_WIKI_AI_HEARTBEAT_INTERVAL_SECONDS={{ backend_wiki_ai_heartbeat_interval_seconds }}",
            tasks,
        )
        self.assertIn("handle {{ backend_wiki_ai_public_prefix | default('/wiki-ai') }}/v1/responses", caddy)
        self.assertNotIn("11434 }}", caddy)
        self.assertIn("name: backend_wiki_ai", playbook)
        self.assertIn("NUTSNEWS_WIKI_AI_API_KEY: ${{ secrets.NUTSNEWS_WIKI_AI_API_KEY }}", workflow)
        self.assertIn("scripts/backend_wiki_ai_smoke.py", workflow)
        self.assertIn("timeout-minutes: 90", workflow)


if __name__ == "__main__":
    unittest.main()
