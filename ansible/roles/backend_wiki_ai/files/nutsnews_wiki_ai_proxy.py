#!/usr/bin/env python3
"""Authenticated, bounded pass-through for the Wiki AI Responses API."""

from __future__ import annotations

import hmac
import http.client
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class ProxyConfig:
    bind: str
    port: int
    api_key: str
    model: str
    max_request_bytes: int
    upstream_host: str
    upstream_port: int
    upstream_timeout_seconds: int


def _bounded_integer(
    env: dict[str, str] | os._Environ[str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_config(env: dict[str, str] | os._Environ[str] = os.environ) -> ProxyConfig:
    api_key = env.get("NUTSNEWS_WIKI_AI_API_KEY", "")
    bind = env.get("NUTSNEWS_WIKI_AI_BIND", "127.0.0.1").strip()
    upstream_host = env.get("NUTSNEWS_WIKI_AI_UPSTREAM_HOST", "127.0.0.1").strip()
    model = env.get("NUTSNEWS_WIKI_AI_MODEL", "").strip()
    if bind not in {"127.0.0.1", "::1"}:
        raise RuntimeError("NUTSNEWS_WIKI_AI_BIND must remain loopback-only")
    if upstream_host not in {"127.0.0.1", "::1"}:
        raise RuntimeError("NUTSNEWS_WIKI_AI_UPSTREAM_HOST must remain loopback-only")
    if len(api_key) < 32 or any(character in api_key for character in "\r\n"):
        raise RuntimeError("NUTSNEWS_WIKI_AI_API_KEY must be a protected single-line value of at least 32 characters")
    if not model or any(character in model for character in "\r\n"):
        raise RuntimeError("NUTSNEWS_WIKI_AI_MODEL must be a protected model alias")
    return ProxyConfig(
        bind=bind,
        port=_bounded_integer(env, "NUTSNEWS_WIKI_AI_PORT", 18089, 1024, 65535),
        api_key=api_key,
        model=model,
        max_request_bytes=_bounded_integer(
            env,
            "NUTSNEWS_WIKI_AI_MAX_REQUEST_BYTES",
            4 * 1024 * 1024,
            1024,
            8 * 1024 * 1024,
        ),
        upstream_host=upstream_host,
        upstream_port=_bounded_integer(env, "NUTSNEWS_WIKI_AI_UPSTREAM_PORT", 11434, 1024, 65535),
        upstream_timeout_seconds=_bounded_integer(
            env,
            "NUTSNEWS_WIKI_AI_UPSTREAM_TIMEOUT_SECONDS",
            3300,
            30,
            3600,
        ),
    )


def validate_responses_payload(raw: bytes, config: ProxyConfig) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request body must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    if payload.get("model") != config.model:
        raise ValueError("only the configured Wiki AI model is allowed")
    if "input" not in payload:
        raise ValueError("Responses requests must include input")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise ValueError("stream must be a boolean")
    max_output_tokens = payload.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 1
        or max_output_tokens > 32768
    ):
        raise ValueError("max_output_tokens must be between 1 and 32768")
    return payload


def model_names_from_tags(payload: Any) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return set()
    names: set[str] = set()
    for model in payload["models"]:
        if isinstance(model, dict):
            for key in ("name", "model"):
                value = model.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
                    if value.endswith(":latest"):
                        names.add(value.removesuffix(":latest"))
    return names


class WikiAIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig):
        super().__init__(address, WikiAIHandler)
        self.config = config
        self.inference_slot = threading.BoundedSemaphore(value=1)


class WikiAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NutsNewsWikiAI/1"
    sys_version = ""

    @property
    def config(self) -> ProxyConfig:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def inference_slot(self) -> threading.BoundedSemaphore:
        return self.server.inference_slot  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json_response(404, {"error": "not_found"})
            return
        try:
            connection = http.client.HTTPConnection(
                self.config.upstream_host,
                self.config.upstream_port,
                timeout=5,
            )
            connection.request("GET", "/api/tags", headers={"accept": "application/json"})
            response = connection.getresponse()
            body = response.read(self.config.max_request_bytes)
            ready = response.status == 200 and self.config.model in model_names_from_tags(json.loads(body))
        except (OSError, ValueError, json.JSONDecodeError):
            ready = False
        finally:
            try:
                connection.close()
            except (NameError, OSError):
                pass
        self._json_response(
            200 if ready else 503,
            {"ok": ready, "service": "nutsnews-wiki-ai", "model_ready": ready},
        )

    def do_POST(self) -> None:
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        status = 500
        if self.path != "/v1/responses":
            self._json_response(404, {"error": "not_found", "request_id": request_id})
            return
        expected_authorization = f"Bearer {self.config.api_key}"
        authorization = self.headers.get("authorization", "")
        if not hmac.compare_digest(authorization, expected_authorization):
            self._json_response(401, {"error": "unauthorized", "request_id": request_id})
            self._audit(request_id, 401, started)
            return
        if self.headers.get("transfer-encoding"):
            self._json_response(411, {"error": "content_length_required", "request_id": request_id})
            self._audit(request_id, 411, started)
            return
        try:
            content_length = int(self.headers.get("content-length", ""))
        except ValueError:
            content_length = -1
        if content_length < 1 or content_length > self.config.max_request_bytes:
            self._json_response(413, {"error": "request_size_rejected", "request_id": request_id})
            self._audit(request_id, 413, started)
            return
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            self._json_response(400, {"error": "incomplete_request", "request_id": request_id})
            self._audit(request_id, 400, started)
            return
        try:
            validate_responses_payload(raw, self.config)
        except ValueError as error:
            self._json_response(400, {"error": "invalid_request", "detail": str(error), "request_id": request_id})
            self._audit(request_id, 400, started)
            return
        if not self.inference_slot.acquire(blocking=False):
            self._json_response(
                429,
                {"error": "wiki_ai_busy", "request_id": request_id},
                extra_headers={"Retry-After": "30"},
            )
            self._audit(request_id, 429, started)
            return
        try:
            status = self._relay_response(raw, request_id)
        finally:
            self.inference_slot.release()
            self._audit(request_id, status, started)

    def _relay_response(self, raw: bytes, request_id: str) -> int:
        connection = http.client.HTTPConnection(
            self.config.upstream_host,
            self.config.upstream_port,
            timeout=self.config.upstream_timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                "/v1/responses",
                body=raw,
                headers={
                    "accept": self.headers.get("accept", "application/json, text/event-stream"),
                    "content-type": "application/json",
                    "x-request-id": request_id,
                },
            )
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                response.read(4096)
                self._json_response(
                    response.status,
                    {"error": "upstream_rejected_request", "upstream_status": response.status, "request_id": request_id},
                )
                return response.status
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("content-type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", request_id)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return response.status
        except (OSError, http.client.HTTPException):
            self._json_response(502, {"error": "upstream_unavailable", "request_id": request_id})
            return 502
        finally:
            connection.close()

    def _json_response(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _audit(self, request_id: str, status: int, started: float) -> None:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        print(
            json.dumps(
                {
                    "event": "wiki_ai_request",
                    "request_id": request_id,
                    "method": self.command,
                    "path": self.path,
                    "status": status,
                    "duration_ms": duration_ms,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


def main() -> None:
    config = load_config()
    server = WikiAIHTTPServer((config.bind, config.port), config)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
