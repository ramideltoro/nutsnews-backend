#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
MODEL_TEMPLATE = ROOT / "ansible/roles/backend_baseline/templates/worker-uplift-shadow-data-model.sql.j2"
CHECK_SCRIPT = ROOT / "scripts/backend_worker_uplift_shadow_model_check.py"
PROTECTED_APPLY = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
PROOF_WORKFLOW = ROOT / ".github/workflows/backend-worker-uplift-shadow-data-model.yml"
BACKEND_CHECKS = ROOT / ".github/workflows/backend-checks.yml"

STAGES = (
    "scheduler",
    "fetcher",
    "canonicalizer",
    "enrichment",
    "approval",
    "translation",
    "persistence",
    "publication",
)


class WorkerUpliftShadowModelTests(unittest.TestCase):
    def test_defaults_target_primary_shadow_database_with_stage_roles(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        self.assertIn("backend_worker_uplift_postgres_enabled: false", defaults)
        self.assertIn('backend_worker_uplift_postgres_database: "{{ backend_postgres_primary_shadow_database }}"', defaults)
        self.assertIn("backend_worker_uplift_postgres_final_schema: worker_uplift_final", defaults)
        self.assertIn("backend_worker_uplift_postgres_views_schema: worker_uplift_views", defaults)
        for stage in STAGES:
            self.assertIn(f"backend_worker_uplift_postgres_{stage}_user: nutsnews_worker_uplift_{stage}", defaults)
            self.assertIn(f"backend_worker_uplift_postgres_{stage}_password: \"\"", defaults)
            self.assertIn(f"schema: worker_uplift_{stage}", defaults)

    def test_postgres_tasks_install_roles_schema_and_status_metadata(self):
        tasks = POSTGRES_TASKS.read_text(encoding="utf-8")
        self.assertIn("Validate worker-uplift PostgreSQL stage role identifiers", tasks)
        self.assertIn("Ensure worker-uplift PostgreSQL stage roles exist", tasks)
        self.assertIn("Allow worker-uplift stage roles to connect to the future-primary shadow database", tasks)
        self.assertIn("Install worker-uplift shadow PostgreSQL data model", tasks)
        self.assertIn("worker-uplift-shadow-data-model.sql.j2", tasks)
        self.assertIn('"direct_public_domain_writes_allowed": false', tasks)
        self.assertIn("backend_worker_uplift_postgres_stage_user_results is changed", tasks)

    def test_template_has_required_stage_state_and_denies_public_writes(self):
        template = MODEL_TEMPLATE.read_text(encoding="utf-8")
        for schema in [f"worker_uplift_{stage}" for stage in STAGES]:
            self.assertIn(schema, template)
            for table in ("inbox", "outbox", "attempts", "transition_ledger", "reconciliation_watermarks"):
                self.assertIn(table, template)
        for table in (
            "feed_schedules",
            "feed_leases",
            "fetch_versions",
            "feed_health_projections",
            "article_identities",
            "article_aliases",
            "enrichment_records",
            "approval_decisions",
            "translation_records",
            "write_requests",
            "publication_readiness",
            "article_shadow_aggregates",
            "api_command_receipts",
            "stage_health_projections",
        ):
            self.assertIn(table, template)
        self.assertIn("UNIQUE (message_id)", template)
        self.assertIn("UNIQUE (idempotency_key)", template)
        self.assertIn("redact_after", template)
        self.assertIn("payload_ref", template)
        self.assertIn("payload_digest", template)
        self.assertIn("sanitized_error_message", template)
        self.assertIn("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public", template)
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON TABLE %I.article_shadow_aggregates", template)
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON TABLE %I.api_command_receipts", template)
        self.assertIn("GRANT SELECT ON TABLE %I.stage_health_projections", template)
        self.assertIn("api_command_receipts_id_seq", template)
        self.assertIn("worker_api_role", template)
        self.assertIn("app_role", template)
        self.assertIn("worker_api_role, app_role", template)
        self.assertIn("CREATE OR REPLACE VIEW %I.enrichment_projection", template)
        self.assertIn("canonical_url_hash AS article_identity_hash", template)
        self.assertIn("diagnostic_metadata->>'sourceFeedUrl' AS source_feed_url", template)
        self.assertIn("original_url_hash,\n           canonical_url_hash,\n           operation_version,\n           identity_status,\n           canonical_url_hash AS article_identity_hash", template)
        self.assertIn("translation_version,\n           quality_status,\n           translated_at,\n           summary_ref", template)
        self.assertIn("decisionSnapshot'->>'canonicalUrl' AS canonical_url", template)
        self.assertIn("decisionSnapshot'->>'sourceSummary' AS source_summary", template)
        self.assertIn("resultSnapshot'->>'title' AS translated_title", template)
        self.assertIn("resultSnapshot'->>'summary' AS translated_summary", template)
        for forbidden in ("article_body", "full_prompt", "raw_provider_response", "bearer_token"):
            self.assertNotIn(forbidden, template)

    def test_protected_apply_wires_worker_uplift_credentials_without_second_database(self):
        workflow = PROTECTED_APPLY.read_text(encoding="utf-8")
        self.assertIn("NUTSNEWS_WORKER_UPLIFT_POSTGRES_ENABLED", workflow)
        self.assertIn('"backend_worker_uplift_postgres_database"] = extra_vars["backend_postgres_primary_shadow_database"]', workflow)
        self.assertNotIn('"backend_worker_uplift_postgres_database"] = os.environ.get("NUTSNEWS_WORKER_UPLIFT_POSTGRES_DATABASE"', workflow)
        for stage in STAGES:
            upper = stage.upper()
            self.assertIn(f"NUTSNEWS_WORKER_UPLIFT_POSTGRES_{upper}_USERNAME", workflow)
            self.assertIn(f"NUTSNEWS_WORKER_UPLIFT_POSTGRES_{upper}_PASSWORD", workflow)
        self.assertIn('extra_vars[f"backend_worker_uplift_postgres_{stage_name}_user"] = username', workflow)
        self.assertIn('extra_vars[f"backend_worker_uplift_postgres_{stage_name}_password"] = password', workflow)

    def test_proof_workflow_and_backend_checks_call_validator(self):
        proof_workflow = PROOF_WORKFLOW.read_text(encoding="utf-8")
        backend_checks = BACKEND_CHECKS.read_text(encoding="utf-8")
        check_script = CHECK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("offline|status|permissions", proof_workflow)
        self.assertIn("backend_worker_uplift_shadow_model_check.py", proof_workflow)
        self.assertIn("--permission-checks", proof_workflow)
        self.assertIn("No database URL, password, token, payload, article body, prompt, or provider response is printed.", proof_workflow)
        self.assertIn("backend_worker_uplift_shadow_model_check.py --offline --enforce", backend_checks)
        self.assertIn("WORKER_API_ROLE = \"nutsnews_worker_api\"", check_script)
        self.assertIn("APP_ROLE = \"nutsnews_app\"", check_script)
        self.assertIn("WORKER_API_FINAL_ROLES", check_script)
        self.assertIn("worker_api_final_grant=", check_script)
        self.assertIn("worker_api_final_grant_failures", check_script)
        self.assertIn("missing_worker_api_final_grant_row", check_script)
        self.assertIn("receipt_sequence_usage", check_script)

    def test_offline_validator_passes_and_reports_safe_metadata(self):
        proc = subprocess.run(
            [sys.executable, str(CHECK_SCRIPT), "--offline", "--enforce"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["safe_metadata_only"])
        self.assertFalse(report["public_domain_writes_allowed"])
        self.assertEqual(report["database_boundary"], "backend_postgres_primary_shadow")


if __name__ == "__main__":
    unittest.main()
