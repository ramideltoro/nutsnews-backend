#!/usr/bin/env python3
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
ALLOY = ROOT / "ansible/roles/backend_baseline/templates/alloy-config.alloy.j2"
METRICS_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/metrics.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
CADDY_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/caddy.yml"
WORKER_API_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/worker_api.yml"
MAIN_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/main.yml"
PRODUCTION_VARS = ROOT / "ansible/inventories/production/group_vars/backend.yml"
PROTECTED_APPLY = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"


class BackendAlloyObservabilityTests(unittest.TestCase):
    def test_worker_service_version_relabel_accepts_full_semver(self):
        template = ALLOY.read_text(encoding="utf-8")
        self.assertIn(
            'regex         = "^([0-9]+[.][0-9]+[.][0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$"',
            template,
        )

    def test_production_alloy_is_enabled_and_requires_explicit_disable_confirmation(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        production = PRODUCTION_VARS.read_text(encoding="utf-8")
        tasks = MAIN_TASKS.read_text(encoding="utf-8")

        self.assertIn("backend_metrics_production_desired_state: false", defaults)
        self.assertIn("backend_metrics_enabled: true", production)
        self.assertIn("backend_metrics_production_desired_state: true", production)
        self.assertIn("backend_logs_enabled: true", production)
        self.assertIn("DISABLE_PRODUCTION_ALLOY", tasks)
        self.assertIn("state: stopped", tasks)

    def test_alloy_credentials_fail_closed_before_configuration(self):
        tasks = METRICS_TASKS.read_text(encoding="utf-8")

        self.assertIn("Validate Grafana Cloud Prometheus settings", tasks)
        self.assertIn("backend_metrics_prometheus_remote_write_url is match('^https://')", tasks)
        self.assertIn("backend_metrics_prometheus_password | length > 0", tasks)
        self.assertIn("backend_logs_loki_url is match('^https://')", tasks)

    def test_all_eight_worker_metrics_endpoints_are_bounded_and_private(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        alloy = ALLOY.read_text(encoding="utf-8")
        worker_defaults = defaults.split("backend_metrics_worker_uplift_targets:", 1)[1].split(
            "backend_metrics_caddy_enabled:", 1
        )[0]

        addresses = re.findall(r"address: (127\.0\.0\.1:\d+)", worker_defaults)
        self.assertEqual(addresses, [f"127.0.0.1:{port}" for port in range(18081, 18089)])
        targets = re.findall(
            r"  - service: ([a-z-]+)\n    stage: ([a-z-]+)\n"
            r"    address: (127\.0\.0\.1:\d+)",
            worker_defaults,
        )
        self.assertEqual(
            targets,
            [
                ("scheduler", "scheduler", "127.0.0.1:18081"),
                ("fetcher", "fetch", "127.0.0.1:18082"),
                ("canonicalizer", "canonicalization", "127.0.0.1:18083"),
                ("enrichment", "enrichment", "127.0.0.1:18084"),
                ("approval", "approval", "127.0.0.1:18085"),
                ("translation", "translation", "127.0.0.1:18086"),
                ("persistence", "persistence", "127.0.0.1:18087"),
                ("publication", "publication", "127.0.0.1:18088"),
            ],
        )
        self.assertIn('prometheus.scrape "worker_uplift"', alloy)
        self.assertIn('metrics_path    = "/metrics"', alloy)
        self.assertIn('replacement  = "nutsnews-worker-uplift"', alloy)
        self.assertIn("sample_limit", alloy)
        self.assertIn("label_limit", alloy)
        self.assertIn("expected_active", alloy)
        self.assertIn("deployment_mode", alloy)
        worker_relabel = alloy.split('prometheus.relabel "worker_uplift"', 1)[1].split("{% endif %}", 1)[0]
        for identity_label in ("version", "revision", "deployment", "adapter"):
            self.assertIn(identity_label, worker_relabel)

    def test_worker_alert_ownership_is_derived_from_protected_cutover_state(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        alloy = ALLOY.read_text(encoding="utf-8")
        tasks = METRICS_TASKS.read_text(encoding="utf-8")
        protected_apply = PROTECTED_APPLY.read_text(encoding="utf-8")

        self.assertIn("backend_worker_api_uplift_cutover_state == 'cutover-approved'", defaults)
        self.assertIn("backend_worker_api_uplift_production_writes_enabled | bool", defaults)
        self.assertIn('uplift_owns_production = cutover_state == "cutover-approved"', protected_apply)
        self.assertIn('extra_vars["backend_metrics_worker_uplift_deployment_mode"]', protected_apply)
        self.assertIn('extra_vars["backend_metrics_worker_uplift_expected_active"]', protected_apply)
        self.assertIn("backend_metrics_worker_uplift_expected_active in ['0', '1']", tasks)
        self.assertIn("backend_metrics_worker_uplift_deployment_mode == 'production'", tasks)
        self.assertIn("selectattr('deployment_mode', 'defined')", tasks)
        self.assertIn("selectattr('expected_active', 'defined')", tasks)
        self.assertIn("backend_worker_api_uplift_cutover_state in ['shadow', 'cutover-approved']", tasks)
        self.assertIn("not (backend_worker_api_uplift_production_writes_enabled | bool)", tasks)
        self.assertNotIn("target.deployment_mode", alloy)
        self.assertNotIn("target.expected_active", alloy)

    def test_host_and_alloy_self_have_distinct_scrape_identities(self):
        alloy = ALLOY.read_text(encoding="utf-8")

        self.assertIn("forward_to      = [prometheus.relabel.backend_host.receiver]", alloy)
        self.assertIn("forward_to      = [prometheus.relabel.alloy_self.receiver]", alloy)
        self.assertIn('replacement  = "nutsnews-backend-host"', alloy)
        self.assertIn('replacement  = "nutsnews-backend-alloy"', alloy)
        self.assertIn('replacement  = "alloy"', alloy)
        host_relabel = alloy.split('prometheus.relabel "backend_host"', 1)[1].split(
            'prometheus.relabel "alloy_self"', 1
        )[0]
        self_relabel = alloy.split('prometheus.relabel "alloy_self"', 1)[1].split(
            "{% endif %}", 1
        )[0]
        for relabel in (host_relabel, self_relabel):
            self.assertIn('target_label = "service_namespace"', relabel)
            self.assertIn('replacement  = "nutsnews"', relabel)
        self.assertNotIn('prometheus.relabel "backend"', alloy)

    def test_backend_api_postgres_and_caddy_metrics_are_scraped(self):
        alloy = ALLOY.read_text(encoding="utf-8")

        self.assertIn('prometheus.scrape "backend_api"', alloy)
        self.assertIn('prometheus.exporter.postgres "backend"', alloy)
        self.assertIn('prometheus.scrape "postgres"', alloy)
        self.assertIn('prometheus.scrape "caddy"', alloy)
        backend_api_relabel = alloy.split('prometheus.relabel "backend_api"', 1)[1].split("{% endif %}", 1)[0]
        for identity_label in ("service_version", "revision", "mode"):
            self.assertIn(identity_label, backend_api_relabel)
        for collector in (
            "database_wraparound",
            "locks",
            "long_running_transactions",
            "replication_slot",
            "stat_activity_autovacuum",
            "stat_bgwriter",
            "stat_checkpointer",
            "stat_database",
            "stat_user_tables",
            "stat_wal_receiver",
            "statio_user_indexes",
            "statio_user_tables",
            "wal",
        ):
            self.assertIn(f'"{collector}"', alloy)
        self.assertIn("disable_settings_metrics = true", alloy)

        postgres_relabel = alloy.split('prometheus.relabel "postgres"', 1)[1].split(
            "{% endif %}", 1
        )[0]
        for family in (
            "stat_user_tables.*",
            "statio_user_indexes.*",
            "statio_user_tables.*",
        ):
            self.assertIn(family, postgres_relabel)
        keep_rule = re.search(
            r'source_labels = \["__name__"\]\n\s+'
            r'regex\s+= "([^"]+)"\n\s+action\s+= "keep"',
            postgres_relabel,
        )
        self.assertIsNotNone(keep_rule)
        metric_allowlist = re.compile(keep_rule.group(1))
        for metric_name in (
            "pg_stat_database_xact_commit_total",
            "pg_stat_database_xact_rollback_total",
            "pg_stat_database_deadlocks_total",
            "pg_stat_database_blks_hit_total",
            "pg_stat_database_blks_read_total",
            "pg_stat_bgwriter_buffers_alloc_total",
            "pg_stat_checkpointer_num_timed_total",
            "pg_stat_user_tables_n_dead_tup",
            "pg_wal_size_bytes",
            "pg_replication_lag_seconds",
            "pg_replication_slots_active",
            "pg_replication_slots_replay_lag_seconds",
        ):
            with self.subTest(metric_name=metric_name):
                self.assertIsNotNone(metric_allowlist.fullmatch(metric_name))
        for bounded_relation_label in ("schemaname", "relname", "indexrelname"):
            self.assertIn(bounded_relation_label, postgres_relabel)

    def test_postgres_exporter_uses_loopback_and_pg_monitor(self):
        alloy = ALLOY.read_text(encoding="utf-8")
        metrics_tasks = METRICS_TASKS.read_text(encoding="utf-8")
        postgres_tasks = POSTGRES_TASKS.read_text(encoding="utf-8")

        exporter = alloy.split('prometheus.exporter.postgres "backend"', 1)[1].split(
            'prometheus.scrape "postgres"', 1
        )[0]
        self.assertIn("@127.0.0.1:5432/", exporter)
        self.assertIn('encoding.url_encode(sys.env("NUTSNEWS_POSTGRES_EXPORTER_PASSWORD"))', exporter)
        self.assertIn("NUTSNEWS_POSTGRES_EXPORTER_PASSWORD", metrics_tasks)
        self.assertIn("no_log: true", metrics_tasks)
        self.assertIn("community.postgresql.postgresql_membership", postgres_tasks)
        self.assertIn("- pg_monitor", postgres_tasks)

        protected_apply = Path(".github/workflows/protected-backend-ansible-apply.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('extra_vars["backend_worker_api_build_revision"] = build_revision', protected_apply)

    def test_caddy_red_tls_and_safe_json_logs_are_enabled(self):
        alloy = ALLOY.read_text(encoding="utf-8")
        caddy = CADDY_TASKS.read_text(encoding="utf-8")

        self.assertIn("metrics {", caddy)
        self.assertIn("per_host", caddy)
        self.assertIn("log backend_access", caddy)
        self.assertIn("/var/log/caddy/access.log", caddy)
        self.assertIn("request>remote_ip delete", caddy)
        self.assertIn("request>client_ip delete", caddy)
        self.assertIn("request>headers delete", caddy)
        self.assertIn("resp_headers delete", caddy)
        self.assertIn("handle /livez", caddy)
        self.assertIn("handle /readyz", caddy)
        self.assertIn("health_uri /livez", caddy)
        self.assertIn("health_interval 30s", caddy)
        self.assertNotIn("health_uri /readyz", caddy)
        self.assertIn("reverse_proxy http://{{ backend_worker_api_bind }}:{{ backend_worker_api_port }}", caddy)
        self.assertIn('prometheus.exporter.blackbox "caddy_tls"', alloy)
        self.assertIn("backend_metrics_caddy_tls_probe_path", alloy)
        self.assertIn("ssl_earliest_cert_expiry", alloy)
        self.assertIn("caddy_http_.+", alloy)
        self.assertIn("caddy_reverse_proxy_.+", alloy)

    def test_alloy_and_caddy_candidates_are_validated_before_atomic_install(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        metrics = METRICS_TASKS.read_text(encoding="utf-8")
        caddy = CADDY_TASKS.read_text(encoding="utf-8")

        self.assertIn("backend_metrics_alloy_candidate_path", defaults)
        self.assertIn("backend_caddy_candidate_path", defaults)
        self.assertIn("backend_metrics_alloy_candidate_path != backend_metrics_alloy_config_path", metrics)
        self.assertIn("backend_caddy_candidate_path != backend_caddy_config_path", caddy)
        self.assertIn("| dirname", metrics)
        self.assertIn("| dirname", caddy)
        self.assertLess(
            metrics.index("Validate Alloy metrics configuration candidate"),
            metrics.index("Atomically install validated Alloy metrics configuration"),
        )
        self.assertIn('src: "{{ backend_metrics_alloy_candidate_path }}"', metrics)
        self.assertIn('dest: "{{ backend_metrics_alloy_config_path }}"', metrics)
        self.assertIn("remote_src: true", metrics)
        self.assertLess(
            caddy.index("Validate backend Caddy configuration candidate"),
            caddy.index("Atomically install validated backend Caddy configuration"),
        )
        self.assertIn('src: "{{ backend_caddy_candidate_path }}"', caddy)
        self.assertIn('dest: "{{ backend_caddy_config_path }}"', caddy)
        self.assertIn("--adapter", caddy)
        self.assertIn("remote_src: true", caddy)
        self.assertLess(
            caddy.index("Atomically install validated backend Caddy configuration"),
            caddy.index("Reload the validated Caddy configuration before probing it"),
        )
        self.assertIn("ansible.builtin.meta: flush_handlers", caddy)
        self.assertIn('"{{ backend_domain }}:443:127.0.0.1"', caddy)
        self.assertIn('"https://{{ backend_domain }}{{ backend_monitoring_health_path }}"', caddy)
        self.assertIn("backend_caddy_local_health.rc | default(1) == 0", caddy)
        self.assertIn("backend_caddy_local_health.stdout | default('') == backend_caddy_health_response", caddy)
        self.assertNotIn("failed_when: false", caddy)

    def test_production_worker_api_rejects_unknown_revision(self):
        tasks = WORKER_API_TASKS.read_text(encoding="utf-8")
        protected_apply = PROTECTED_APPLY.read_text(encoding="utf-8")

        self.assertIn("backend_worker_api_deployment_environment != 'production'", tasks)
        self.assertIn("backend_worker_api_deployment_environment in ['production', 'staging', 'test', 'development']", tasks)
        self.assertIn("backend_worker_api_build_revision is match('^[0-9a-f]{40}$')", tasks)
        self.assertIn('re.fullmatch(r"[0-9a-f]{40}", build_revision)', protected_apply)

    def test_loki_boundary_has_only_normalized_indexed_labels(self):
        alloy = ALLOY.read_text(encoding="utf-8")
        label_keep = alloy.split("stage.label_keep", 1)[1].split("}", 1)[0]
        expected = {"deployment_environment", "service", "service_version", "host", "source", "severity"}
        retained = set(re.findall(r'"([a-z_]+)"', label_keep))

        self.assertEqual(retained, expected)
        metadata = alloy.split("stage.structured_metadata", 1)[1].split("}", 1)[0]
        for field in (
            "request_id",
            "message_id",
            "correlation_id",
            "trace_id",
            "article_id",
            "feed_id",
            "pipeline_run_id",
            "idempotency_key",
            "traceparent",
            "revision",
            "image_digest",
        ):
            self.assertIn(field, metadata)
        self.assertIn('revision        = "deployed_revision"', metadata)
        self.assertIn('image_digest    = "deployed_image_digest"', metadata)
        self.assertNotIn("deployed_revision", label_keep)
        self.assertNotIn("deployed_image_digest", label_keep)

        process = alloy.split('loki.process "backend_logs"', 1)[1]
        for raw, canonical in (
            ("trace|debug", "debug"),
            ("info|notice", "info"),
            ("warn|warning", "warning"),
            ("err|error", "error"),
            ("fatal|panic|critical|crit|alert|emerg|emergency", "critical"),
        ):
            self.assertIn(raw, process)
            self.assertIn(f'values = {{ severity = "{canonical}" }}', process)
        self.assertIn('selector            = "{severity=\\"debug\\"}"', process)

    def test_required_systemd_sources_and_alloy_health_checks_are_managed(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        metrics_tasks = METRICS_TASKS.read_text(encoding="utf-8")

        for unit in (
            "nutsnews-worker-db-api.service",
            "nutsnews-supabase-sync-relay.service",
            "nutsnews-supabase-sync-relay.timer",
            "postgresql.service",
            "nutsnews-rabbitmq.service",
            "nutsnews-rabbitmq-canary.service",
            "nutsnews-rabbitmq-canary.timer",
            "nutsnews-metrics-textfile.timer",
        ):
            self.assertIn(f"unit: {unit}", defaults)
        self.assertIn('unit: "postgresql@{{ backend_postgres_major_version }}-main.service"', defaults)
        self.assertIn("http://127.0.0.1:12345/-/ready", metrics_tasks)
        self.assertIn("http://127.0.0.1:12345/-/healthy", metrics_tasks)


if __name__ == "__main__":
    unittest.main()
