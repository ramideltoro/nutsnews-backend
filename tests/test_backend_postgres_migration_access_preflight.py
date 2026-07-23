#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/backend_postgres_migration_access_preflight.py"


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
        self.assertIn(("scheduler", "worker_uplift_scheduler"), module.WORKER_UPLIFT_STAGE_SCHEMAS)
        self.assertIn("worker_uplift_final", module.WORKER_UPLIFT_SCHEMAS)
        self.assertIn("worker_uplift_views", module.WORKER_UPLIFT_SCHEMAS)

    def test_worker_uplift_role_discovery_uses_explicit_acl_grants(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("aclexplode(coalesce(n.nspacl", source)
        self.assertIn("aclexplode(coalesce(c.relacl", source)
        self.assertIn("not r.rolsuper", source)
        self.assertIn("excluded.role_name = r.rolname", source)


if __name__ == "__main__":
    unittest.main()
