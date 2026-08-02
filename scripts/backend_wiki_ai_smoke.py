#!/usr/bin/env python3
"""Verify the public Wiki AI health, authentication, model, and tool-call path."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


EXPECTED_TOOL_VALUE = "nutsnews-wiki-ai-ready"


def tool_call_is_valid(payload: Any) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
        return False
    for item in payload["output"]:
        if not isinstance(item, dict) or item.get("type") != "function_call" or item.get("name") != "echo_value":
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if isinstance(arguments, dict) and arguments.get("value") == EXPECTED_TOOL_VALUE:
            return True
    return False


def request_json(request: urllib.request.Request, *, timeout: int) -> tuple[int, Any]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        raw = response.read(4 * 1024 * 1024)
    return status, json.loads(raw)


def health_url(responses_endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(responses_endpoint)
    if not parsed.path.endswith("/v1/responses"):
        raise ValueError("responses endpoint must end with /v1/responses")
    return urllib.parse.urlunsplit(parsed._replace(path=parsed.path.removesuffix("/v1/responses") + "/health"))


def run_smoke(endpoint: str, api_key: str, model: str, timeout: int) -> dict[str, Any]:
    if len(api_key) < 32 or "\n" in api_key or "\r" in api_key:
        raise ValueError("NUTSNEWS_WIKI_AI_API_KEY is missing or invalid")
    started = time.monotonic()
    health_status, health = request_json(
        urllib.request.Request(health_url(endpoint), headers={"accept": "application/json"}),
        timeout=min(timeout, 30),
    )
    if health_status != 200 or health != {
        "ok": True,
        "service": "nutsnews-wiki-ai",
        "model_ready": True,
    }:
        raise RuntimeError("Wiki AI public health contract failed")
    request_payload = {
        "model": model,
        "instructions": "Call the supplied echo_value function exactly once. Do not answer with ordinary text.",
        "input": f"Call echo_value with value {EXPECTED_TOOL_VALUE}.",
        "tools": [
            {
                "type": "function",
                "name": "echo_value",
                "description": "Return a fixed readiness value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ],
        "stream": False,
        "max_output_tokens": 256,
    }
    body = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
    response_status, response = request_json(
        urllib.request.Request(
            endpoint,
            method="POST",
            data=body,
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        ),
        timeout=timeout,
    )
    if response_status != 200 or not tool_call_is_valid(response):
        raise RuntimeError("Wiki AI did not return the required function call")
    return {
        "status": "pass",
        "endpoint_origin": urllib.parse.urlsplit(endpoint).netloc,
        "model": model,
        "health": "ready",
        "authentication": "accepted",
        "tool_call": "valid",
        "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default=os.environ.get(
            "NUTSNEWS_WIKI_AI_RESPONSES_ENDPOINT",
            "https://backend.nutsnews.com/wiki-ai/v1/responses",
        ),
    )
    parser.add_argument("--model", default="nutsnews-wiki-qwen")
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_key = os.environ.get("NUTSNEWS_WIKI_AI_API_KEY", "")
    try:
        result = run_smoke(args.endpoint, api_key, args.model, args.timeout)
    except (ValueError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
        message = str(error).replace(api_key, "[redacted]") if api_key else str(error)
        print(json.dumps({"status": "fail", "error": message}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
