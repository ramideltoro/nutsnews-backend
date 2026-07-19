#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
CADDY_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/caddy.yml"
FIREWALL_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/firewall.yml"
APPLY_WORKFLOW = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"
DRILL_WORKFLOW = ROOT / ".github/workflows/backend-postgres-failover-drill.yml"
PLAN = ROOT / "docs/backend-postgres-replacement-plan.json"


class BackendPostgresFailoverTests(unittest.TestCase):
    def test_postgres_defaults_are_private_and_opt_in(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        self.assertIn("backend_postgres_enabled: false", defaults)
        self.assertIn("backend_postgres_listen_addresses: localhost", defaults)
        self.assertIn("backend_db_dashboard_bind: 127.0.0.1", defaults)
        self.assertIn('backend_db_dashboard_port: "8082"', defaults)
        self.assertIn("backend_worker_api_enabled: false", defaults)
        self.assertIn("backend_worker_api_bind: 127.0.0.1", defaults)
        self.assertIn("backend_worker_api_writes_enabled: false", defaults)
        self.assertIn("  - acl", defaults)

    def test_postgres_tasks_keep_database_and_dashboard_loopback_only(self):
        tasks = POSTGRES_TASKS.read_text(encoding="utf-8")
        self.assertIn("listen_addresses = '{{ backend_postgres_listen_addresses }}'", tasks)
        self.assertIn("host all all 127.0.0.1/32 scram-sha-256", tasks)
        self.assertIn("listen.allowed_clients = 127.0.0.1", tasks)
        self.assertIn("backend_db_dashboard_php_fpm_listen", tasks)
        self.assertIn("no multi-writer topology", tasks)

    def test_database_connect_grants_target_database_objects_explicitly(self):
        tasks = POSTGRES_TASKS.read_text(encoding="utf-8")
        failover_grant = tasks.split("- name: Allow read-only role to connect to the failover database", 1)[1].split(
            "- name: Allow migration roles to connect to the future-primary shadow database",
            1,
        )[0]
        shadow_grant = tasks.split(
            "- name: Allow migration roles to connect to the future-primary shadow database",
            1,
        )[1].split("- name: Allow read-only role to use the public schema", 1)[0]
        self.assertIn('objs: "{{ backend_postgres_database }}"', failover_grant)
        self.assertIn('objs: "{{ backend_postgres_primary_shadow_database }}"', shadow_grant)
        self.assertIn("{{ backend_postgres_app_user }}", shadow_grant)
        self.assertIn("{{ backend_postgres_replication_user }}", shadow_grant)
        enforce_grant = tasks.split("- name: Enforce future-primary shadow database CONNECT grants", 1)[1].split(
            "- name: Allow read-only role to use the public schema",
            1,
        )[0]
        self.assertIn("community.postgresql.postgresql_query", enforce_grant)
        self.assertIn("GRANT CONNECT ON DATABASE %I TO %I", enforce_grant)
        self.assertIn("{{ backend_postgres_primary_shadow_database }}", enforce_grant)
        self.assertIn("not ansible_check_mode", enforce_grant)

    def test_caddy_exposes_adminer_only_on_loopback(self):
        caddy = CADDY_TASKS.read_text(encoding="utf-8")
        self.assertIn("http://{{ backend_db_dashboard_bind }}:{{ backend_db_dashboard_port }}", caddy)
        self.assertIn("bind {{ backend_db_dashboard_bind }}", caddy)
        self.assertIn("php_fastcgi {{ backend_db_dashboard_php_fpm_listen }}", caddy)
        self.assertNotIn("{{ backend_domain }}/adminer", caddy)
        self.assertIn("handle /api/worker/db/*", caddy)
        self.assertIn("reverse_proxy http://{{ backend_worker_api_bind }}:{{ backend_worker_api_port }}", caddy)

    def test_firewall_does_not_open_database_or_dashboard_ports(self):
        firewall = FIREWALL_TASKS.read_text(encoding="utf-8")
        self.assertNotIn("5432", firewall)
        self.assertNotIn("8082", firewall)
        self.assertNotIn("9085", firewall)

    def test_protected_apply_wires_postgres_secrets_without_values(self):
        workflow = APPLY_WORKFLOW.read_text(encoding="utf-8")
        build_step = workflow.split("- name: Build runtime extra vars", 1)[1].split(
            "- name: Run backend Ansible baseline",
            1,
        )[0]
        build_step_env = build_step.split("run: |", 1)[0]
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_ENABLED", workflow)
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD", workflow)
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD", workflow)
        self.assertIn("NUTSNEWS_BACKEND_WORKER_API_ENABLED", workflow)
        self.assertIn("NUTSNEWS_BACKEND_API_TOKEN", workflow)
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_ENABLED", build_step)
        self.assertIn("NUTSNEWS_BACKEND_DB_DASHBOARD_ENABLED", build_step)
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD", build_step)
        self.assertIn("NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD", build_step)
        for secret_name in (
            "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_RESTORE_PASSWORD",
            "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD",
            "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD",
            "NUTSNEWS_BACKEND_POSTGRES_MIGRATION_APP_REHEARSAL_PASSWORD",
        ):
            self.assertIn(secret_name, build_step)
        self.assertIn("NUTSNEWS_BACKEND_WORKER_API_ENABLED", build_step_env)
        self.assertIn("NUTSNEWS_BACKEND_WORKER_API_WRITES_ENABLED", build_step_env)
        self.assertIn("NUTSNEWS_BACKEND_API_TOKEN", build_step_env)
        self.assertIn("${{ vars.NUTSNEWS_BACKEND_WORKER_API_ENABLED || 'false' }}", build_step_env)
        self.assertIn("${{ vars.NUTSNEWS_BACKEND_WORKER_API_WRITES_ENABLED || 'false' }}", build_step_env)
        self.assertIn("${{ secrets.NUTSNEWS_BACKEND_API_TOKEN }}", build_step_env)
        self.assertIn('os.environ.get("NUTSNEWS_BACKEND_POSTGRES_ENABLED", "true")', build_step)
        self.assertIn('"backend_postgres_enabled"] = True', workflow)
        self.assertIn('"backend_postgres_restore_password"]', build_step)
        self.assertIn('"backend_postgres_validation_password"]', build_step)
        self.assertIn('"backend_postgres_replication_password"]', build_step)
        self.assertIn('"backend_postgres_app_rehearsal_password"]', build_step)
        self.assertIn('"backend_worker_api_enabled"] = True', build_step)
        self.assertIn('"backend_worker_api_token"] = token', build_step)
        self.assertIn('"backend_worker_api_writes_enabled"] = writes_enabled', build_step)
        self.assertNotIn("postgres://", workflow)

    def test_restore_drill_has_fixed_modes_and_staging_source(self):
        workflow = DRILL_WORKFLOW.read_text(encoding="utf-8")
        for mode in ("status", "dry-run", "restore-staging"):
            self.assertIn(f"- {mode}", workflow)
        self.assertIn("restore-staging-to-backend-postgres", workflow)
        self.assertIn("NUTSNEWS_STAGING_SUPABASE_PROJECT_REF", workflow)
        self.assertIn("supabase db dump --linked --schema public", workflow)
        self.assertNotIn("NUTSNEWS_PRODUCTION_SUPABASE_DB_URL", workflow)
        self.assertNotIn("source_project_ref:", workflow.split("permissions:", 1)[0])

    def test_restore_runner_has_supabase_auth_shim_and_cleans_dumps(self):
        script = (ROOT / "scripts/backend_postgres_restore_remote.sh").read_text(encoding="utf-8")
        primary_script = (ROOT / "scripts/backend_postgres_primary_shadow_restore_remote.sh").read_text(
            encoding="utf-8"
        )
        validation = (ROOT / "scripts/backend_postgres_restore_validation.sql").read_text(encoding="utf-8")
        tasks = POSTGRES_TASKS.read_text(encoding="utf-8")
        self.assertIn("CREATE SCHEMA IF NOT EXISTS auth", script)
        self.assertIn("CREATE TABLE IF NOT EXISTS auth.users", script)
        self.assertIn("role_attr_flags: LOGIN,BYPASSRLS", tasks)
        self.assertIn("trap cleanup_remote_dir EXIT", script)
        self.assertIn("-name 'nutsnews-postgres-drill-*'", script)
        self.assertIn("shred -u", script)
        self.assertIn("Schema restore failed; last schema log lines follow.", script)
        self.assertNotIn("Data restore failed; last data log lines follow.", script)
        self.assertIn("sudo -n chgrp postgres", script)
        self.assertIn("chmod 0750", script)
        self.assertIn("chmod 0640", script)
        self.assertIn("GRANT SELECT ON ALL TABLES IN SCHEMA public", script)
        self.assertIn("GRANT SELECT ON ALL TABLES IN SCHEMA supabase_migrations", primary_script)
        self.assertIn("GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public", script)
        self.assertIn("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public", script)
        self.assertIn("refresh materialized view public.public_feed_snapshot", validation)
        self.assertIn("ALTER SUBSCRIPTION :\"subscription\" SET (slot_name = NONE);", primary_script)
        self.assertIn("DROP SUBSCRIPTION IF EXISTS :\"subscription\";", primary_script)
        replication_target = (ROOT / "scripts/backend_postgres_logical_replication_target_remote.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$sub_exists" == "true"', replication_target)
        self.assertIn('os.environ["WORKER_PRESENT"] in {"1", "t", "true"}', replication_target)

    def test_plan_forbids_multi_writer_and_production_cutover(self):
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        self.assertEqual(plan["decision"], "deploy_private_restore_verified_failover_target")
        self.assertTrue(plan["install_postgres_now"])
        self.assertFalse(plan["production_cutover_allowed"])
        self.assertEqual(plan["initial_operating_mode"]["multi_writer"], "forbidden")
        self.assertFalse(plan["failback"]["sync_back_supported"])


if __name__ == "__main__":
    unittest.main()
