#!/usr/bin/env python3
from __future__ import annotations

import json
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


def alloy_backend_logs_process() -> str:
    return ALLOY.read_text(encoding="utf-8").split('loki.process "backend_logs"', 1)[1]


def alloy_stage_body(process: str, stage: str) -> str:
    return process.split(f"stage.{stage} {{", 1)[1].split("\n  }", 1)[0]


def alloy_string_assignments(body: str) -> dict[str, str]:
    return dict(re.findall(r'^\s+([a-z_]+)\s+=\s+"([^"]*)",?$', body, re.MULTILINE))


def alloy_metadata_validators(process: str) -> dict[str, tuple[str, str]]:
    validators: dict[str, tuple[str, str]] = {}
    for body in re.findall(r"  stage[.]regex [{]\n(.*?)\n  [}]", process, re.DOTALL):
        source = re.search(r'^\s+source\s+=\s+"([^"]+)"$', body, re.MULTILINE)
        expression = re.search(r'^\s+expression\s+=\s+"([^"]+)"$', body, re.MULTILINE)
        if source and expression:
            capture = re.search(r"[(][?]P<([a-z_]+)>", expression.group(1))
            if capture:
                validators[source.group(1)] = (capture.group(1), expression.group(1))
    return validators


def alloy_regex_stages(process: str) -> list[tuple[str, str]]:
    stages: list[tuple[str, str]] = []
    for body in re.findall(r"  stage[.]regex [{]\n(.*?)\n  [}]", process, re.DOTALL):
        source = re.search(r'^\s+source\s+=\s+"([^"]+)"$', body, re.MULTILINE)
        expression = re.search(r'^\s+expression\s+=\s+"([^"]+)"$', body, re.MULTILINE)
        if source and expression:
            stages.append((source.group(1), expression.group(1)))
    return stages


def apply_alloy_metadata_contract(process: str, source: str, value: str) -> dict[str, str]:
    extracted = {source: value}
    for stage_source, expression in alloy_regex_stages(process):
        candidate = extracted.get(stage_source)
        if candidate is None:
            continue
        match = re.fullmatch(expression, candidate)
        if match:
            extracted.update({key: matched for key, matched in match.groupdict().items() if matched is not None})
    return extracted


def alloy_line_redaction_expressions(process: str) -> list[str]:
    expressions: list[str] = []
    for body in re.findall(r"  stage[.]replace [{]\n(.*?)\n  [}]", process, re.DOTALL):
        expression_line = next(
            line.strip() for line in body.splitlines() if line.strip().startswith('expression = "')
        )
        encoded = expression_line.removeprefix('expression = "')[:-1]
        expressions.append(json.loads(f'"{encoded}"'))
    return expressions


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
        self.assertIn("observability_contract", alloy)
        self.assertIn("backend_metrics_worker_uplift_contract_status", alloy)
        self.assertNotIn("expected_active", alloy)
        self.assertNotIn("deployment_mode", alloy)
        worker_relabel = alloy.split('prometheus.relabel "worker_uplift"', 1)[1].split("{% endif %}", 1)[0]
        self.assertIn(
            'target_label = "service_namespace"\n    replacement  = "nutsnews"',
            worker_relabel,
        )
        labelkeep = re.search(
            r'regex\s+= "\^\(([^"]+)\)\$"\n\s+action = "labelkeep"',
            worker_relabel,
        )
        self.assertIsNotNone(labelkeep)
        self.assertIn("service_namespace", labelkeep.group(1).split("|"))
        for identity_label in ("version", "revision", "deployment", "adapter"):
            self.assertIn(identity_label, worker_relabel)

    def test_worker_alert_ownership_comes_only_from_authoritative_control_row(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        alloy = ALLOY.read_text(encoding="utf-8")
        tasks = METRICS_TASKS.read_text(encoding="utf-8")
        protected_apply = PROTECTED_APPLY.read_text(encoding="utf-8")
        exporter = (ROOT / "ansible/roles/backend_baseline/files/nutsnews_metrics_textfile.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("from worker_uplift_final.cutover_control", exporter)
        self.assertIn("where control_id = 'production'", exporter)
        self.assertIn("WORKER_UPLIFT_CONTROL_STATE_TUPLES", exporter)
        self.assertIn("cutover_state = os.environ", protected_apply)
        self.assertIn('cutover_state != "shadow"', protected_apply)
        self.assertIn('uplift_production_writes != "false"', protected_apply)
        self.assertIn("backend_metrics_worker_uplift_contract_status: awaiting-qualified-v1", defaults)
        self.assertIn("backend_metrics_worker_uplift_contract_enabled: false", defaults)
        self.assertIn("selectattr('deployment_mode', 'defined')", tasks)
        self.assertIn("selectattr('expected_active', 'defined')", tasks)
        self.assertNotIn("NUTSNEWS_WORKER_UPLIFT_EXPECTED_ACTIVE", exporter)
        self.assertNotIn("NUTSNEWS_WORKER_UPLIFT_DEPLOYMENT_MODE", exporter)
        self.assertNotIn("backend_metrics_worker_uplift_expected_active", tasks)
        self.assertNotIn("backend_metrics_worker_uplift_deployment_mode", tasks)
        self.assertNotIn("expected_active", alloy)
        self.assertNotIn("deployment_mode", alloy)

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
            "wal",
        ):
            self.assertIn(f'"{collector}"', alloy)
        for excluded_collector in ("process_idle", "statio_user_indexes", "statio_user_tables"):
            self.assertNotIn(f'"{excluded_collector}"', alloy)
        self.assertIn("disable_settings_metrics = true", alloy)

        postgres_relabel = alloy.split('prometheus.relabel "postgres"', 1)[1].split(
            "{% endif %}", 1
        )[0]
        self.assertIn("stat_user_tables.*", postgres_relabel)
        self.assertNotIn("process_idle.*", postgres_relabel)
        self.assertNotIn("statio_user_indexes.*", postgres_relabel)
        self.assertNotIn("statio_user_tables.*", postgres_relabel)
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
        for bounded_relation_label in ("schemaname", "relname"):
            self.assertIn(bounded_relation_label, postgres_relabel)

    def test_postgres_scrape_capacity_has_bounded_relation_headroom(self):
        defaults = DEFAULTS.read_text(encoding="utf-8")
        metrics_tasks = METRICS_TASKS.read_text(encoding="utf-8")

        values = {
            key: int(value)
            for key, value in re.findall(
                r"^(backend_metrics_postgres_(?:relation_budget|samples_per_relation_budget|fixed_sample_budget|sample_limit)):\s+(\d+)$",
                defaults,
                re.MULTILINE,
            )
        }
        self.assertEqual(
            values,
            {
                "backend_metrics_postgres_relation_budget": 64,
                "backend_metrics_postgres_samples_per_relation_budget": 32,
                "backend_metrics_postgres_fixed_sample_budget": 512,
                "backend_metrics_postgres_sample_limit": 4096,
            },
        )
        required = (
            values["backend_metrics_postgres_relation_budget"]
            * values["backend_metrics_postgres_samples_per_relation_budget"]
            + values["backend_metrics_postgres_fixed_sample_budget"]
        )
        self.assertGreaterEqual(values["backend_metrics_postgres_relation_budget"], 42)
        self.assertGreaterEqual(values["backend_metrics_postgres_sample_limit"], required)
        self.assertGreaterEqual(values["backend_metrics_postgres_sample_limit"] - required, 1536)
        self.assertIn("backend_metrics_postgres_sample_limit | int >=", metrics_tasks)
        self.assertIn("backend_metrics_postgres_sample_limit | int <= 4096", metrics_tasks)

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
        self.assertIn("request>uri delete", caddy)
        self.assertIn("resp_headers delete", caddy)
        self.assertIn("handle /livez", caddy)
        self.assertIn("handle /readyz", caddy)
        self.assertIn("health_uri /livez", caddy)
        self.assertIn("health_interval 30s", caddy)
        self.assertNotIn("health_uri /readyz", caddy)
        self.assertIn("reverse_proxy http://{{ backend_worker_api_bind }}:{{ backend_worker_api_port }}", caddy)
        self.assertIn('prometheus.exporter.blackbox "caddy_tls"', alloy)
        self.assertIn("backend_metrics_caddy_tls_probe_path", alloy)
        defaults = DEFAULTS.read_text(encoding="utf-8")
        self.assertIn("backend_metrics_caddy_tls_probe_path: /healthz", defaults)
        self.assertIn("ssl_earliest_cert_expiry", alloy)
        self.assertIn("caddy_http_.+", alloy)
        self.assertIn("caddy_reverse_proxy_.+", alloy)

    def test_worker_api_is_loopback_only_and_readiness_is_a_deployment_gate(self):
        worker_api = Path("ansible/roles/backend_baseline/tasks/worker_api.yml").read_text(encoding="utf-8")
        caddy = CADDY_TASKS.read_text(encoding="utf-8")

        self.assertIn("backend_worker_api_bind == '127.0.0.1'", worker_api)
        self.assertNotIn("failed_when: false", worker_api)
        self.assertIn('search(\'"ready"[ ]*:[ ]*true\')', worker_api)
        self.assertIn("backend_worker_api_build_revision", worker_api)
        self.assertIn("Capture Caddy-routed Worker database API readiness", caddy)
        self.assertIn('"https://{{ backend_domain }}/readyz"', caddy)

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

    def test_loki_structured_metadata_source_contract_is_closed(self):
        process = alloy_backend_logs_process()
        metadata = alloy_string_assignments(alloy_stage_body(process, "structured_metadata"))

        self.assertEqual(
            metadata,
            {
                "request_id": "safe_request_id",
                "correlation_id": "safe_correlation_id",
                "causation_id": "safe_causation_id",
                "message_id": "safe_message_id",
                "idempotency_key": "safe_idempotency_key",
                "trace_id": "safe_trace_id",
                "article_id": "safe_article_id",
                "feed_id": "safe_feed_id",
                "pipeline_run_id": "safe_pipeline_run_id",
                "traceparent": "safe_traceparent",
                "queue": "safe_queue",
                "outcome": "safe_outcome",
                "revision": "deployed_revision",
                "image_digest": "deployed_image_digest",
            },
        )
        self.assertNotIn("tracestate", metadata)
        self.assertFalse(any(key.startswith("raw_") or value.startswith("raw_") for key, value in metadata.items()))

        labels = set(re.findall(r'"([a-z_]+)"', alloy_stage_body(process, "label_keep")))
        self.assertEqual(
            labels,
            {"deployment_environment", "service", "service_version", "host", "source", "severity"},
        )
        self.assertTrue(set(metadata).isdisjoint(labels))

    def test_loki_metadata_uses_only_raw_candidates_and_validated_captures(self):
        process = alloy_backend_logs_process()
        extracted = alloy_string_assignments(alloy_stage_body(process, "json"))
        expected_json = {
            "raw_severity": "level || severity",
            "raw_request_id": "requestId || request_id || attributes.requestId || attributes.request_id",
            "raw_correlation_id": "correlationId || correlation_id",
            "raw_causation_id": "causationId || causation_id",
            "raw_message_id": "messageId || message_id",
            "raw_idempotency_key": "idempotencyKey || idempotency_key",
            "raw_article_id": "articleId || article_id || attributes.articleId || attributes.article_id || attributes.canonicalArticleId || attributes.articleIdentityHash",
            "raw_feed_id": "feedId || feed_id || attributes.feedId || attributes.feed_id",
            "raw_pipeline_run_id": "pipelineRunId || pipeline_run_id",
            "raw_traceparent": "traceparent",
            "raw_queue": "queue",
            "raw_outcome": "outcome",
        }
        self.assertEqual(extracted, expected_json)

        expected_captures = {
            "raw_request_id": "safe_request_id",
            "raw_correlation_id": "safe_correlation_id",
            "raw_causation_id": "safe_causation_id",
            "raw_message_id": "safe_message_id",
            "raw_idempotency_key": "candidate_idempotency_key",
            "candidate_idempotency_key": "safe_idempotency_key",
            "raw_article_id": "safe_article_id",
            "raw_feed_id": "safe_feed_id",
            "raw_pipeline_run_id": "safe_pipeline_run_id",
            "raw_traceparent": "candidate_traceparent",
            "candidate_traceparent": "safe_traceparent",
            "raw_queue": "safe_queue",
            "raw_outcome": "safe_outcome",
        }
        validators = alloy_metadata_validators(process)
        self.assertEqual({source: capture for source, (capture, _) in validators.items()}, expected_captures)
        for source, (capture, expression) in validators.items():
            with self.subTest(source=source):
                self.assertTrue(expression.startswith("^") and expression.endswith("$"))
                self.assertNotIn("(?:", expression, "RE2 source contracts must not use non-capturing groups")
                self.assertEqual(capture, expected_captures[source])

    def test_loki_metadata_source_contract_rejects_sensitive_and_oversized_values(self):
        # Alloy is not available in this test environment. Compiling and exercising
        # the checked-in expressions with Python is a deterministic source-contract
        # check, not a claim that the rendered Alloy configuration ran here.
        process = alloy_backend_logs_process()
        valid_values = (
            ("raw_request_id", "enrichment-req-001", "safe_request_id"),
            ("raw_correlation_id", "123e4567-e89b-42d3-a456-426614174000", "safe_correlation_id"),
            ("raw_causation_id", "123e4567-e89b-42d3-a456-426614174000", "safe_causation_id"),
            ("raw_message_id", "123e4567-e89b-42d3-a456-426614174000", "safe_message_id"),
            ("raw_idempotency_key", "canonicalizer:enrichment:candidate-world-001", "safe_idempotency_key"),
            ("raw_idempotency_key", "feed:world:story-001", "safe_idempotency_key"),
            ("raw_article_id", "article-001", "safe_article_id"),
            ("raw_feed_id", "feed-world", "safe_feed_id"),
            ("raw_pipeline_run_id", "123e4567-e89b-42d3-a456-426614174000", "safe_pipeline_run_id"),
            ("raw_traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "safe_traceparent"),
            ("raw_queue", "nutsnews.worker.fetch.v1.retry-30s", "safe_queue"),
            ("raw_outcome", "success", "safe_outcome"),
        )
        safe_keys = {safe_key for _, _, safe_key in valid_values}
        for source, value, safe_key in valid_values:
            with self.subTest(source=source, value=value):
                extracted = apply_alloy_metadata_contract(process, source, value)
                self.assertEqual(extracted.get(safe_key), value)
        trace = apply_alloy_metadata_contract(
            process,
            "raw_traceparent",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )
        self.assertEqual(trace.get("safe_trace_id"), "4bf92f3577b34da6a3ce929d0e0e4736")

        for source, value, safe_key in (
            ("raw_idempotency_key", "feed:" + "a" * 252, "safe_idempotency_key"),
            ("raw_feed_id", "feed-" + "a" * 65, "safe_feed_id"),
        ):
            with self.subTest(source=source, fixture="maximum_plus_one"):
                self.assertNotIn(safe_key, apply_alloy_metadata_contract(process, source, value))

        sensitive_values = (
            "authorization=Bearer abcdef0123456789",
            "https://example.test/private?access_token=abcdef0123456789",
            "person@example.com",
            "-----BEGIN PRIVATE KEY-----",
            "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
            "secret=abcdefghijklmnopqrstuvwxyz0123456789",
            "a" * 300,
        )
        raw_sources = {source for source, _, _ in valid_values}
        for source in raw_sources:
            for value in sensitive_values:
                with self.subTest(source=source, fixture=value[:24]):
                    extracted = apply_alloy_metadata_contract(process, source, value)
                    self.assertTrue(safe_keys.isdisjoint(extracted))

    def test_loki_quoted_json_secrets_are_redacted_from_the_shipped_line(self):
        # This translates Alloy's POSIX whitespace class only so the checked-in
        # source expressions can be exercised deterministically with Python.
        # It does not substitute for validating a rendered config with Alloy.
        expressions = [
            expression.replace("[[:space:]]", r"\s").replace("[:space:]", r"\s")
            for expression in alloy_line_redaction_expressions(alloy_backend_logs_process())
        ]
        self.assertEqual(len(expressions), 5)
        fixtures = {
            "authorization": '{"authorization":"Bearer bearer-secret-value","event":"request"}',
            "cookie": '{"cookie":"session=private-cookie; theme=dark","event":"request"}',
            "password": '{"password":"correct horse battery staple","event":"login"}',
            "api_key": '{"apiKey":"private-api-key-value","event":"provider"}',
            "access_token": '{"access_token":"private-access-token-value","event":"oauth"}',
            "refresh_token": '{"refreshToken":"private-refresh-token-value","event":"oauth"}',
            "oauth_code": '{"oauthCode":"private-oauth-code-value","event":"oauth"}',
            "session": '{"session":"private-session-value","event":"login"}',
            "token": '{"token":"github_pat_private-token-value","event":"provider"}',
        }
        for field, line in fixtures.items():
            redacted = line
            for expression in expressions:
                redacted = re.sub(expression, "[REDACTED]", redacted)
            with self.subTest(field=field):
                json_field = {
                    "api_key": "apiKey",
                    "refresh_token": "refreshToken",
                    "oauth_code": "oauthCode",
                }.get(field, field)
                private_value = json.loads(line)[json_field]
                self.assertNotIn(private_value, redacted)
                self.assertIn("[REDACTED]", redacted)

        metadata = alloy_string_assignments(
            alloy_stage_body(alloy_backend_logs_process(), "structured_metadata")
        )
        extracted = alloy_string_assignments(alloy_stage_body(alloy_backend_logs_process(), "json"))
        for forbidden in fixtures:
            with self.subTest(forbidden_metadata_key=forbidden):
                self.assertNotIn(forbidden, metadata)
                self.assertNotIn(f"raw_{forbidden}", extracted)

    def test_loki_metadata_bounds_and_redaction_precede_export(self):
        process = alloy_backend_logs_process()
        first_truncate = process.index("  stage.truncate {")
        metadata_truncate = process.index("  stage.truncate {", first_truncate + 1)
        first_validator = process.index('    source     = "raw_request_id"')
        structured_metadata = process.index("  stage.structured_metadata {")
        label_keep = process.index("  stage.label_keep {")
        ordered_markers = (
            'drop_counter_reason = "private_key_marker"',
            'drop_counter_reason = "oversized_log_line"',
            "  stage.json {",
            'drop_counter_reason = "debug_trace_log_level"',
            "authorization",
            "(cookie|set-cookie)",
            "(api[_-]?key|access[_-]?token",
            "([?][^[:space:]",
            "([A-Za-z0-9._%+",
        )
        positions = [process.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))
        replace_positions = [match.start() for match in re.finditer(r"^  stage[.]replace [{]$", process, re.MULTILINE)]
        self.assertEqual(len(replace_positions), 5)
        self.assertTrue(all(positions[3] < position < first_truncate for position in replace_positions))
        self.assertLess(first_truncate, first_validator)
        self.assertLess(first_validator, metadata_truncate)
        self.assertLess(metadata_truncate, structured_metadata)
        self.assertLess(structured_metadata, label_keep)

        metadata_bounds = process[metadata_truncate:structured_metadata]
        bounded_sources: dict[str, str] = {}
        for sources, limit in re.findall(
            r'source_type = "extracted"\n\s+sources\s+= \[([^]]+)\]\n\s+limit\s+= "([0-9]+B)"',
            metadata_bounds,
        ):
            for source in re.findall(r'"([a-z_]+)"', sources):
                self.assertNotIn(source, bounded_sources)
                bounded_sources[source] = limit
        self.assertEqual(
            bounded_sources,
            {
                "safe_request_id": "64B",
                "safe_correlation_id": "64B",
                "safe_causation_id": "64B",
                "safe_message_id": "64B",
                "safe_trace_id": "64B",
                "safe_pipeline_run_id": "64B",
                "safe_traceparent": "64B",
                "safe_queue": "64B",
                "safe_idempotency_key": "256B",
                "safe_article_id": "96B",
                "safe_feed_id": "96B",
                "safe_outcome": "24B",
            },
        )
        self.assertTrue(all(source.startswith("safe_") for source in bounded_sources))

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
