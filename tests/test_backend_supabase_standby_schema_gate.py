from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import backend_supabase_standby_schema_gate as gate


NOW = "2026-07-26T20:40:00Z"
MEASURED = "2026-07-26T20:39:45Z"
REVISION = "a" * 40
MANIFEST_FINGERPRINT = "f" * 64


def contract() -> dict:
    return {
        "contract_id": "backend-supabase-sync-relay",
        "version": 1,
        "source_manifest": {"schema_fingerprint": MANIFEST_FINGERPRINT},
        "source": {"label": "backend_postgres_primary"},
        "target": {
            "label": "existing_production_supabase_standby",
            "existing_production_supabase_project": True,
            "create_new_supabase_project": False,
            "create_nutsnews_standby_database": False,
        },
        "safety": {
            "backend_postgresql_remains_primary": True,
            "app_worker_writes_to_supabase_before_failover": False,
        },
        "tables": [
            {"name": "public.articles", "primary_key": ["id"]},
            {"name": "public.rss_feeds", "primary_key": ["id"]},
        ],
        "sequences": [
            {"name": "public.rss_feeds_id_seq", "table": "public.rss_feeds", "column": "id"},
        ],
    }


def candidate_manifest(**overrides) -> dict:
    manifest = {
        "manifestVersion": 1,
        "schemaFingerprint": MANIFEST_FINGERPRINT,
        "source": {
            "migrationHead": "20260717113000",
            "migrationSourceFingerprint": "0" * 64,
        },
        "safety": {
            "existingProductionSupabaseProject": True,
            "createNewSupabaseProject": False,
            "createNutsnewsStandbyDatabase": False,
            "appWorkerSupabaseWritesBeforeApprovedFailover": False,
            "safeMetadataOnly": True,
        },
        "replication": {
            "tables": [
                {"name": "public.articles"},
                {"name": "public.rss_feeds"},
            ],
        },
        "sequences": {
            "items": [
                {"name": "public.rss_feeds_id_seq", "table": "public.rss_feeds", "column": "id"},
            ]
        },
    }
    manifest.update(overrides)
    return manifest


def schema_check(**overrides) -> dict:
    check = {
        "id": "schema-fingerprint",
        "status": "pass",
        "source_schema_sha256": "schema-digest",
        "target_schema_sha256": "schema-digest",
        "source_migration_contract_sha256": "contract-digest",
        "target_migration_contract_sha256": "contract-digest",
        "source_schema_bytes": 1000,
        "target_schema_bytes": 1000,
        "sensitivity": "metadata_hash_only",
    }
    check.update(overrides)
    return check


def manifest_identity(name: str, **overrides) -> dict:
    check = {
        "id": f"manifest-identity.{name}",
        "name": name,
        "status": "pass",
        "reasons": [],
        "replica_identity_type": "primary_key",
        "sensitivity": "metadata_only",
    }
    check.update(overrides)
    return check


def live_identity(name: str, **overrides) -> dict:
    check = {
        "id": f"live-identity.{name}",
        "name": name,
        "status": "pass",
        "reasons": [],
        "expected_primary_key": ["id"],
        "source_primary_key": ["id"],
        "target_primary_key": ["id"],
        "source_relation_kind": "r",
        "target_relation_kind": "r",
        "sensitivity": "metadata_only",
    }
    check.update(overrides)
    return check


def sequence_check(**overrides) -> dict:
    check = {
        "id": "sequence.public.rss_feeds_id_seq",
        "name": "public.rss_feeds_id_seq",
        "table": "public.rss_feeds",
        "column": "id",
        "status": "pass",
        "reasons": [],
        "sensitivity": "sequence_metadata_only",
    }
    check.update(overrides)
    return check


def relay_report(
    *,
    preflight_checks: list[dict] | None = None,
    post_sync_checks: list[dict] | None = None,
    **overrides,
) -> dict:
    preflight = preflight_checks if preflight_checks is not None else [
        schema_check(),
        manifest_identity("public.articles"),
        live_identity("public.articles"),
        manifest_identity("public.rss_feeds"),
        live_identity("public.rss_feeds"),
    ]
    post_sync = post_sync_checks if post_sync_checks is not None else [schema_check(), sequence_check()]
    report = {
        "status": "pass",
        "checked_at_utc": MEASURED,
        "source_label": "backend_postgres_primary",
        "target_label": "existing_production_supabase_standby",
        "safe_metadata_only": True,
        "preflight": {
            "status": "pass",
            "failed_required_checks": [],
            "checks": preflight,
        },
        "post_sync": {
            "status": "pass",
            "failed_required_checks": [],
            "checks": post_sync,
        },
    }
    report.update(overrides)
    return report


def health_report(*, measured_at: str = MEASURED, relay: dict | None = None) -> dict:
    return {
        "version": 1,
        "last_report_run_at": measured_at,
        "ssh": {
            "commands": {
                "supabase_sync_relay_status": {
                    "stdout": json.dumps(relay if relay is not None else relay_report()) + "\n"
                }
            }
        },
    }


def run_gate(
    report: dict | str,
    contract_data: dict | None = None,
    candidate_data: dict | str | None = None,
    *extra_args: str,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        contract_path = tmp / "contract.json"
        manifest_path = tmp / "standby_manifest.json"
        health_path = tmp / "health.json"
        output_path = tmp / "gate.json"
        summary_path = tmp / "summary.md"
        contract_path.write_text(json.dumps(contract_data or contract()), encoding="utf-8")
        if isinstance(candidate_data, str):
            manifest_path.write_text(candidate_data, encoding="utf-8")
        else:
            manifest_path.write_text(json.dumps(candidate_data or candidate_manifest()), encoding="utf-8")
        if isinstance(report, str):
            health_path.write_text(report, encoding="utf-8")
        else:
            health_path.write_text(json.dumps(report), encoding="utf-8")
        with redirect_stdout(StringIO()):
            exit_code = gate.main_args(
                [
                    "--health-report",
                    str(health_path),
                    "--contract",
                    str(contract_path),
                    "--candidate-standby-manifest",
                    str(manifest_path),
                    "--candidate-application-revision",
                    REVISION,
                    "--repository-revision",
                    "b" * 40,
                    "--failover-attempt-id",
                    "failover-20260726T204000Z",
                    "--now-utc",
                    NOW,
                    "--output",
                    str(output_path),
                    "--summary",
                    str(summary_path),
                    *extra_args,
                ]
            )
        return exit_code, json.loads(output_path.read_text(encoding="utf-8")), summary_path.read_text(encoding="utf-8")


class BackendSupabaseStandbySchemaGateTests(unittest.TestCase):
    def test_exact_compatible_schema_passes(self):
        exit_code, result, _ = run_gate(health_report())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["required_table_count"], 2)
        self.assertEqual(result["passed_identity_count"], 2)
        self.assertEqual(result["failed_identity_count"], 0)
        self.assertEqual(result["required_sequence_count"], 1)
        self.assertEqual(result["passed_sequence_binding_count"], 1)
        self.assertEqual(result["failed_sequence_binding_count"], 0)
        self.assertEqual(result["candidate_manifest"]["schema_fingerprint"], MANIFEST_FINGERPRINT)
        self.assertEqual(result["blockers"], [])

    def test_candidate_manifest_fingerprint_mismatch_fails_wrong_revision_evidence(self):
        candidate = candidate_manifest(schemaFingerprint="e" * 64)
        _, result, _ = run_gate(health_report(), None, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("candidate_manifest_fingerprint_mismatch", result["blockers"])

    def test_candidate_manifest_table_set_mismatch_fails(self):
        candidate = candidate_manifest(replication={"tables": [{"name": "public.articles"}]})
        _, result, _ = run_gate(health_report(), None, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("candidate_manifest_table_set_mismatch", result["blockers"])

    def test_candidate_manifest_function_or_view_requires_validator(self):
        candidate = candidate_manifest(functions=[{"name": "public.required_fn"}], views=[{"name": "public.required_view"}])
        _, result, _ = run_gate(health_report(), None, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("required_function_validation_unavailable", result["blockers"])
        self.assertIn("required_view_validation_unavailable", result["blockers"])

    def test_schema_fingerprint_mismatch_fails(self):
        checks = [
            schema_check(status="fail", target_schema_sha256="different"),
            manifest_identity("public.articles"),
            live_identity("public.articles"),
            manifest_identity("public.rss_feeds"),
            live_identity("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(preflight_checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("schema_compatibility_failed", result["blockers"])
        self.assertIn("schema_fingerprint_mismatch", result["schema"]["blockers"])

    def test_migration_contract_mismatch_fails(self):
        checks = [
            schema_check(status="fail", target_migration_contract_sha256="different"),
            manifest_identity("public.articles"),
            live_identity("public.articles"),
            manifest_identity("public.rss_feeds"),
            live_identity("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(preflight_checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("migration_contract_fingerprint_mismatch", result["schema"]["blockers"])

    def test_schema_diff_is_bounded_safe_metadata(self):
        diff = {
            "columns": {
                "missing_in_target_count": 1,
                "extra_in_target_count": 0,
                "different_count": 1,
                "missing_in_target": ["public.articles.title"],
                "extra_in_target": [],
                "different": [{"key": "public.rss_feeds.url", "source": {"type": "text"}, "target": {"type": "varchar"}}],
                "truncated": False,
            }
        }
        checks = [
            schema_check(status="fail", target_schema_sha256="different", schema_diff=diff),
            manifest_identity("public.articles"),
            live_identity("public.articles"),
            manifest_identity("public.rss_feeds"),
            live_identity("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(preflight_checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["schema"]["schema_diff"]["columns"]["missing_in_target"], ["public.articles.title"])

    def test_missing_identity_check_fails(self):
        checks = [
            schema_check(),
            manifest_identity("public.articles"),
            live_identity("public.articles"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(preflight_checks=checks)))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("identity_compatibility_failed", result["blockers"])
        rss = next(item for item in result["identity_checks"] if item["name"] == "public.rss_feeds")
        self.assertIn("manifest_identity_check_missing", rss["blockers"])
        self.assertIn("live_identity_check_missing", rss["blockers"])

    def test_primary_key_or_replica_identity_mismatch_fails(self):
        checks = [
            schema_check(),
            manifest_identity("public.articles", status="fail", reasons=["manifest_replica_identity_not_primary_key"]),
            live_identity("public.articles", status="fail", reasons=["target_primary_key_mismatch"]),
            manifest_identity("public.rss_feeds"),
            live_identity("public.rss_feeds"),
        ]
        _, result, _ = run_gate(health_report(relay=relay_report(preflight_checks=checks)))
        articles = next(item for item in result["identity_checks"] if item["name"] == "public.articles")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("manifest_replica_identity_not_primary_key", articles["blockers"])
        self.assertIn("target_primary_key_mismatch", articles["blockers"])

    def test_missing_sequence_binding_fails(self):
        _, result, _ = run_gate(health_report(relay=relay_report(post_sync_checks=[schema_check()])))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("sequence_binding_failed", result["blockers"])
        self.assertIn("sequence_binding_check_missing", result["sequence_bindings"][0]["blockers"])

    def test_sequence_position_only_failure_does_not_block_schema_gate(self):
        seq = sequence_check(status="fail", reasons=["target_next_value_not_above_source_max_id"])
        _, result, _ = run_gate(health_report(relay=relay_report(post_sync_checks=[schema_check(), seq])))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sequence_bindings"][0]["position_safety_status"], "fail")

    def test_stale_telemetry_fails_closed(self):
        _, result, _ = run_gate(health_report(measured_at="2026-07-26T20:00:00Z"))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("telemetry_stale", result["blockers"])

    def test_malformed_health_report_fails_closed(self):
        _, result, _ = run_gate("{not-json")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["telemetry_malformed"])

    def test_malformed_candidate_manifest_fails_closed(self):
        _, result, _ = run_gate(health_report(), None, "{not-json")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["candidate_manifest_malformed"])

    def test_mismatched_target_fails_without_printing_target_label(self):
        _, result, _ = run_gate(health_report(relay=relay_report(target_label="other_standby_target")))
        text = json.dumps(result)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("target_fingerprint_mismatch", result["blockers"])
        self.assertNotIn("other_standby_target", text)
        self.assertNotIn("existing_production_supabase_standby", text)
        self.assertNotIn("backend_postgres_primary", text)

    def test_unavailable_relay_status_fails_closed(self):
        report = health_report()
        report["ssh"]["commands"]["supabase_sync_relay_status"]["stdout"] = "not_configured\n"
        _, result, _ = run_gate(report)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("relay_telemetry_unavailable", result["blockers"])

    def test_expected_candidate_revision_mismatch_fails(self):
        _, result, _ = run_gate(health_report(), None, None, "--expected-application-revision", "b" * 40)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["blockers"], ["candidate_application_revision_mismatch"])

    def test_enforce_returns_nonzero_on_failure(self):
        candidate = candidate_manifest(schemaFingerprint="e" * 64)
        exit_code, result, _ = run_gate(health_report(), None, candidate, "--enforce")
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "FAIL")

    def test_artifact_and_summary_are_safe_metadata_only(self):
        _, result, summary = run_gate(health_report())
        text = json.dumps(result) + summary
        self.assertTrue(result["safe_metadata_only"])
        self.assertIn("sha256:", text)
        for forbidden in (
            "postgres://",
            "postgresql://",
            "PGPASSWORD",
            "db.",
            "supabase.co",
            "backend_postgres_primary",
            "existing_production_supabase_standby",
            "raw row",
            "password",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
