#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/backend_postgres_migration_access_preflight.py"
PREFLIGHT_WORKFLOW = ROOT / ".github/workflows/backend-postgres-migration-access-preflight.yml"


def load_module():
    spec = importlib.util.spec_from_file_location("backend_postgres_migration_access_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackendPostgresMigrationAccessPreflightTests(unittest.TestCase):
    def test_parse_postgres_bool_accepts_psql_boolean_text(self):
        module = load_module()
        for value in ("t", "true", "1", "yes", "on", " TRUE "):
            self.assertTrue(module.parse_postgres_bool(value))
        for value in ("f", "false", "0", "no", "off", ""):
            self.assertFalse(module.parse_postgres_bool(value))

    def test_worker_uplift_schemas_are_part_of_live_preflight(self):
        module = load_module()
        self.assertNotIn("nutsnews_worker_uplift_scheduler", module.ROLE_NAMES)
        self.assertNotIn("nutsnews_worker_uplift_scheduler", module.PRIMARY_SHADOW_CONNECT_ROLES)
        self.assertEqual("nutsnews_worker_api", module.WORKER_API_ROLE)
        self.assertIn(("scheduler", "worker_uplift_scheduler"), module.WORKER_UPLIFT_STAGE_SCHEMAS)
        self.assertIn("worker_uplift_final", module.WORKER_UPLIFT_SCHEMAS)
        self.assertIn("worker_uplift_views", module.WORKER_UPLIFT_SCHEMAS)

    def test_worker_uplift_stage_role_parser_requires_every_stage(self):
        module = load_module()
        values = [
            f"{stage}:configured_{stage}_role"
            for stage, _schema in module.WORKER_UPLIFT_STAGE_SCHEMAS
        ]
        roles = module.parse_worker_uplift_stage_roles(values)
        self.assertEqual(
            ("scheduler", "configured_scheduler_role", "worker_uplift_scheduler"),
            roles[0],
        )
        with self.assertRaises(SystemExit):
            module.parse_worker_uplift_stage_roles(values[:-1])
        with self.assertRaises(SystemExit):
            module.parse_worker_uplift_stage_roles([*values, values[0]])
        with self.assertRaises(SystemExit):
            module.parse_worker_uplift_stage_roles([*values[:-1], "publication:not-safe-role"])

    def test_worker_uplift_role_discovery_uses_explicit_acl_grants(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("aclexplode(coalesce(n.nspacl", source)
        self.assertIn("aclexplode(coalesce(c.relacl", source)
        self.assertIn("not r.rolsuper", source)
        self.assertIn("excluded.role_name = r.rolname", source)
        self.assertIn("worker_api_final_grant=", source)
        self.assertIn("worker_api_final_shadow_grant", source)
        self.assertIn("api_command_receipts", source)
        self.assertIn("receipt_sequence_usage", source)

    def test_workflow_passes_configured_worker_uplift_stage_roles(self):
        workflow = PREFLIGHT_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("NUTSNEWS_WORKER_UPLIFT_POSTGRES_SCHEDULER_USERNAME", workflow)
        self.assertIn("--worker-uplift-stage-role", workflow)
        self.assertIn("scheduler fetcher canonicalizer enrichment approval translation persistence publication", workflow)

    def test_offline_report_includes_worker_api_final_grant_check(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("\"name\": \"worker_api_final_shadow_grant\"", source)


if __name__ == "__main__":
    unittest.main()
