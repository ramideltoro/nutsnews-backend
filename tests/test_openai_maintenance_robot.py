#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import openai_maintenance_robot as robot


def candidate(**overrides):
    base = {
        "title": "Unpinned workflow action in backend checks",
        "fingerprint": "mnt-test-123",
        "detected_repo": "ramideltoro/nutsnews-backend",
        "target_repo": "ramideltoro/nutsnews-backend",
        "category": "security",
        "severity": "medium",
        "confidence": "high",
        "possible_noise": "The action may be intentionally version-pinned elsewhere, but this line is not SHA-pinned.",
        "evidence": ".github/workflows/backend-checks.yml uses actions/checkout@v4",
        "why_matters": "Mutable action tags can change behavior without a reviewed commit.",
        "suggested_fix": "Pin the action to a full commit SHA.",
        "acceptance_criteria": ["The workflow action uses a full 40-character SHA."],
        "validation_ideas": ["Run actionlint and backend checks."],
        "related_files": [".github/workflows/backend-checks.yml"],
        "secret_redaction_status": "redacted",
        "labels": ["bug"],
        "possible_duplicate": "",
    }
    base.update(overrides)
    return base


class OpenAIMaintenanceRobotTests(unittest.TestCase):
    def test_scan_label_uses_stable_utc_shape(self):
        label = robot.scan_label(dt.datetime(2026, 7, 16, 21, 30, tzinfo=dt.UTC))
        self.assertEqual(label, "scan+2026-07-16T21-30-00Z")

    def test_redaction_removes_sensitive_values(self):
        raw = "\n".join(
            [
                "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456",
                "Cookie: sessionid=abc123",
                "DATABASE_URL=postgres://user:secret@example.com/db?token=abc",
                "owner@example.com",
                "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            ]
        )
        redacted = robot.redact_text(raw)
        self.assertNotIn("ghp_", redacted)
        self.assertNotIn("sessionid=abc123", redacted)
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("token=abc", redacted)
        self.assertNotIn("owner@example.com", redacted)
        self.assertNotIn("abc\n-----END", redacted)

    def test_parse_structured_output_accepts_object_and_fenced_json(self):
        payload = {"findings": [candidate()]}
        self.assertEqual(len(robot.parse_openai_findings(json.dumps(payload))), 1)
        self.assertEqual(len(robot.parse_openai_findings("```json\n" + json.dumps(payload) + "\n```")), 1)

    def test_repo_routing_uses_explicit_and_keyword_routes(self):
        self.assertEqual(robot.target_repo_for(candidate(target_repo="ramideltoro/nutsnews")), "ramideltoro/nutsnews")
        self.assertEqual(
            robot.target_repo_for(candidate(target_repo="", title="Docs runbook missing recovery note")),
            "ramideltoro/nutsnews-docs",
        )
        self.assertEqual(
            robot.target_repo_for(candidate(target_repo="", title="VPS Ansible role drift")),
            "ramideltoro/nutsnews-infra",
        )
        self.assertEqual(
            robot.target_repo_for(candidate(target_repo="", title="Public API response regression")),
            "ramideltoro/nutsnews",
        )

    def test_issue_body_contains_required_sections(self):
        body = robot.render_issue_body(candidate(), "scan+2026-07-16T21-30-00Z")
        for field in robot.BODY_FIELDS:
            self.assertIn(f"## {field}", body)
        self.assertIn("mnt-test-123", body)

    def test_low_confidence_finding_is_not_dropped_and_gets_question_label(self):
        normalized = robot.normalize_candidate(
            candidate(confidence="low", category="investigation", labels=[]),
            "scan+2026-07-16T21-30-00Z",
        )
        self.assertIn("question", normalized["labels"])
        self.assertEqual(normalized["confidence"], "low")

    def test_duplicate_note_is_rendered_but_not_suppressed(self):
        normalized = robot.normalize_candidate(
            candidate(possible_duplicate="possible duplicate of #123"),
            "scan+2026-07-16T21-30-00Z",
        )
        self.assertIn("## Possible duplicate", normalized["body"])
        self.assertIn("possible duplicate of #123", normalized["body"])

    def test_issue_creation_failure_creates_followup_when_possible(self):
        normalized = robot.normalize_candidate(candidate(), "scan+2026-07-16T21-30-00Z")
        args = SimpleNamespace(mode="create", test_target_repo="ramideltoro/nutsnews-backend")
        with mock.patch.dict("os.environ", {"NUTSNEWS_MAINTENANCE_GITHUB_TOKEN": "token"}, clear=True):
            with mock.patch.object(robot, "find_possible_duplicate", return_value=""):
                with mock.patch.object(
                    robot,
                    "create_issue",
                    side_effect=[RuntimeError("primary failed"), {"number": 99, "html_url": "https://example.test/99"}],
                ):
                    results = robot.create_issues([normalized], args, "scan+2026-07-16T21-30-00Z")
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[1]["status"], "created_failure_report")


if __name__ == "__main__":
    unittest.main()
