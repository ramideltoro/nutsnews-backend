#!/usr/bin/env python3
"""Plan, apply, or roll back the backend Cloudflare DNS record."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True)
class DesiredRecord:
    name: str
    content: str
    proxied: bool
    ttl: int


class CloudflareError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("check", "apply", "rollback"), required=True)
    parser.add_argument("--zone-id", required=True)
    parser.add_argument("--api-token", required=True)
    parser.add_argument("--record-name", required=True)
    parser.add_argument("--target-ipv4", required=True)
    parser.add_argument("--proxied", choices=("true", "false"), default="false")
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--output", help="Optional path for JSON result output.")
    return parser.parse_args(argv)


def request_json(method: str, path: str, api_token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudflareError(f"Cloudflare API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudflareError(f"Cloudflare API request failed: {exc.reason}") from exc

    if not body.get("success"):
        raise CloudflareError(f"Cloudflare API returned errors: {body.get('errors', [])}")
    return body


def list_records(zone_id: str, api_token: str, name: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"type": "A", "name": name})
    body = request_json("GET", f"/zones/{zone_id}/dns_records?{query}", api_token)
    return body.get("result", [])


def desired_payload(record: DesiredRecord) -> dict[str, Any]:
    return {
        "type": "A",
        "name": record.name,
        "content": record.content,
        "ttl": record.ttl,
        "proxied": record.proxied,
        "comment": "Managed by ramideltoro/nutsnews-backend protected workflow.",
    }


def classify_plan(records: list[dict[str, Any]], desired: DesiredRecord, mode: str) -> dict[str, Any]:
    if len(records) > 1:
        return {
            "mode": mode,
            "action": "blocked",
            "reason": "multiple matching A records exist",
            "record_name": desired.name,
            "matching_records": len(records),
        }

    if mode == "rollback":
        return {
            "mode": mode,
            "action": "delete" if records else "noop",
            "record_name": desired.name,
            "target_ipv4": desired.content,
            "proxied": desired.proxied,
            "ttl": desired.ttl,
            "existing": summarize_record(records[0]) if records else None,
        }

    if not records:
        action = "create"
        existing = None
    else:
        existing = summarize_record(records[0])
        action = "noop" if record_matches(records[0], desired) else "update"

    return {
        "mode": mode,
        "action": action,
        "record_name": desired.name,
        "target_ipv4": desired.content,
        "proxied": desired.proxied,
        "ttl": desired.ttl,
        "existing": existing,
    }


def summarize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "type": record.get("type"),
        "content": record.get("content"),
        "proxied": record.get("proxied"),
        "ttl": record.get("ttl"),
    }


def record_matches(record: dict[str, Any], desired: DesiredRecord) -> bool:
    return (
        record.get("type") == "A"
        and record.get("name") == desired.name
        and record.get("content") == desired.content
        and bool(record.get("proxied")) is desired.proxied
        and int(record.get("ttl", 1)) == desired.ttl
    )


def apply_plan(zone_id: str, api_token: str, desired: DesiredRecord, plan: dict[str, Any]) -> dict[str, Any]:
    action = plan["action"]
    if action == "blocked":
        raise CloudflareError(plan["reason"])
    if action == "noop":
        return {**plan, "applied": False}

    payload = desired_payload(desired)
    existing = plan.get("existing")
    if action == "create":
        result = request_json("POST", f"/zones/{zone_id}/dns_records", api_token, payload)["result"]
    elif action == "update":
        result = request_json("PUT", f"/zones/{zone_id}/dns_records/{existing['id']}", api_token, payload)["result"]
    elif action == "delete":
        result = request_json("DELETE", f"/zones/{zone_id}/dns_records/{existing['id']}", api_token)["result"]
    else:
        raise CloudflareError(f"Unsupported action: {action}")

    return {**plan, "applied": True, "result": summarize_record(result)}


def write_result(path: str | None, result: dict[str, Any]) -> None:
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    desired = DesiredRecord(
        name=args.record_name.strip(),
        content=args.target_ipv4.strip(),
        proxied=args.proxied == "true",
        ttl=args.ttl,
    )

    try:
        records = list_records(args.zone_id, args.api_token, desired.name)
        plan = classify_plan(records, desired, args.mode)
        if args.mode == "check":
            result = plan
            if result["action"] == "blocked":
                raise CloudflareError(result["reason"])
        else:
            result = apply_plan(args.zone_id, args.api_token, desired, plan)
        write_result(args.output, result)
    except CloudflareError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
