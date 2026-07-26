#!/usr/bin/env python3
"""Validate the worker-uplift PostgreSQL shadow data model without exposing data."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ansible/roles/backend_baseline/templates/worker-uplift-shadow-data-model.sql.j2"
DEFAULTS = ROOT / "ansible/roles/backend_baseline/defaults/main.yml"
POSTGRES_TASKS = ROOT / "ansible/roles/backend_baseline/tasks/postgres.yml"
PROTECTED_APPLY_WORKFLOW = ROOT / ".github/workflows/protected-backend-ansible-apply.yml"

STAGES = [
    ("scheduler", "nutsnews_worker_uplift_scheduler", "worker_uplift_scheduler"),
    ("fetcher", "nutsnews_worker_uplift_fetcher", "worker_uplift_fetcher"),
    ("canonicalizer", "nutsnews_worker_uplift_canonicalizer", "worker_uplift_canonicalizer"),
    ("enrichment", "nutsnews_worker_uplift_enrichment", "worker_uplift_enrichment"),
    ("approval", "nutsnews_worker_uplift_approval", "worker_uplift_approval"),
    ("translation", "nutsnews_worker_uplift_translation", "worker_uplift_translation"),
    ("persistence", "nutsnews_worker_uplift_persistence", "worker_uplift_persistence"),
    ("publication", "nutsnews_worker_uplift_publication", "worker_uplift_publication"),
]
FINAL_SCHEMA = "worker_uplift_final"
VIEWS_SCHEMA = "worker_uplift_views"
WORKER_API_ROLE = "nutsnews_worker_api"
PUBLIC_DOMAIN_TABLES = [
    "public.articles",
    "public.article_ai_reviews",
    "public.article_summaries",
    "public.ai_usage_runs",
    "public.feed_health",
    "public.public_feed_snapshot",
    "public.worker_runs",
]
COMMON_TABLES = ["inbox", "outbox", "attempts", "transition_ledger", "reconciliation_watermarks"]
STAGE_SPECIFIC_TABLES = {
    "worker_uplift_scheduler": ["feed_schedules", "feed_leases"],
    "worker_uplift_fetcher": ["fetch_versions", "feed_health_projections"],
    "worker_uplift_canonicalizer": ["article_identities", "article_aliases"],
    "worker_uplift_enrichment": ["enrichment_records"],
    "worker_uplift_approval": ["approval_decisions"],
    "worker_uplift_translation": ["translation_records"],
    "worker_uplift_persistence": ["write_requests"],
    "worker_uplift_publication": ["publication_readiness", "publication_decisions"],
    FINAL_SCHEMA: ["article_shadow_aggregates", "api_command_receipts", "stage_health_projections"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_psql(db_url: str, query: str, *, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "psql_not_installed"
    except subprocess.TimeoutExpired:
        return 124, "", "query_timeout"
    return proc.returncode, proc.stdout, proc.stderr


def check_static_files() -> list[dict]:
    template = TEMPLATE.read_text(encoding="utf-8")
    defaults = DEFAULTS.read_text(encoding="utf-8")
    tasks = POSTGRES_TASKS.read_text(encoding="utf-8")
    workflow = PROTECTED_APPLY_WORKFLOW.read_text(encoding="utf-8")
    failures: list[str] = []

    for stage, role, schema in STAGES:
        for token in (stage, role, schema):
            if token not in defaults and token not in template and token not in workflow:
                failures.append(f"missing_static_token:{token}")
        for table in COMMON_TABLES + STAGE_SPECIFIC_TABLES[schema]:
            if table not in template:
                failures.append(f"missing_table_template:{schema}.{table}")

    for token in (
        "article_shadow_aggregates",
        "CREATE TABLE IF NOT EXISTS",
        "UNIQUE (message_id)",
        "UNIQUE (idempotency_key)",
        "transition_ledger",
        "redact_after",
        "payload_ref",
        "payload_digest",
        "sanitized_error_message",
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public",
        "GRANT SELECT, INSERT, UPDATE ON TABLE %I.article_shadow_aggregates",
        "api_command_receipts",
        "stage_health_projections",
        "stage_health_projection",
        "UNIQUE (idempotency_key)",
    ):
        if token not in template:
            failures.append(f"missing_template_guardrail:{token}")

    for token in (
        "backend_worker_uplift_postgres_enabled: false",
        'backend_worker_uplift_postgres_database: "{{ backend_postgres_primary_shadow_database }}"',
        f"backend_worker_uplift_postgres_final_schema: {FINAL_SCHEMA}",
        f"backend_worker_uplift_postgres_views_schema: {VIEWS_SCHEMA}",
        "backend_worker_uplift_postgres_stage_roles:",
    ):
        if token not in defaults:
            failures.append(f"missing_default:{token}")

    for token in (
        "Validate worker-uplift PostgreSQL stage role identifiers",
        "Ensure worker-uplift PostgreSQL stage roles exist",
        "Allow worker-uplift stage roles to connect to the future-primary shadow database",
        "Install worker-uplift shadow PostgreSQL data model",
        "worker-uplift-shadow-data-model.sql.j2",
    ):
        if token not in tasks:
            failures.append(f"missing_task:{token}")

    for token in (
        "NUTSNEWS_WORKER_UPLIFT_POSTGRES_ENABLED",
        "NUTSNEWS_WORKER_UPLIFT_POSTGRES_SCHEDULER_PASSWORD",
        '"backend_worker_uplift_postgres_database"] = extra_vars["backend_postgres_primary_shadow_database"]',
        "backend_worker_uplift_postgres_{stage_name}_password",
    ):
        if token not in workflow:
            failures.append(f"missing_workflow_wiring:{token}")

    forbidden_payload_markers = ["article_body", "full_prompt", "raw_provider_response", "bearer_token"]
    for marker in forbidden_payload_markers:
        if marker in template:
            failures.append(f"forbidden_payload_marker:{marker}")

    return [
        {
            "name": "static_worker_uplift_shadow_model",
            "status": "fail" if failures else "pass",
            "failure_count": len(failures),
            "failures": failures,
        }
    ]


def configured_stages(env_prefix: str) -> list[tuple[str, str, str]]:
    stages: list[tuple[str, str, str]] = []
    for stage, default_role, schema in STAGES:
        env_name = f"{env_prefix}_{stage.upper()}_DB_URL"
        db_url = os.environ.get(env_name, "")
        parsed_role = urlparse(db_url).username if db_url else None
        stages.append((stage, parsed_role or default_role, schema))
    return stages


def live_catalog_query(stages: list[tuple[str, str, str]]) -> str:
    schema_csv = ",".join([schema for _stage, _role, schema in stages] + [FINAL_SCHEMA, VIEWS_SCHEMA])
    table_values = []
    for _stage, _role, schema in stages:
        for table in COMMON_TABLES + STAGE_SPECIFIC_TABLES[schema]:
            table_values.append(f"('{schema}','{table}')")
    for table in STAGE_SPECIFIC_TABLES[FINAL_SCHEMA]:
        table_values.append(f"('{FINAL_SCHEMA}','{table}')")
    role_schema_values = ",".join(f"('{stage}','{role}','{schema}')" for stage, role, schema in stages)
    role_csv = ",".join(role for _stage, role, _schema in stages)
    domain_table_values = ",".join(f"('{table}')" for table in PUBLIC_DOMAIN_TABLES)
    return f"""
select 'schema=' || n.nspname
from pg_namespace n
where n.nspname = any(string_to_array('{schema_csv}', ','))
order by n.nspname;

with expected(schema_name, table_name) as (values {",".join(table_values)})
select 'table=' || e.schema_name || '.' || e.table_name
from expected e
join pg_class c on c.relname = e.table_name and c.relkind in ('r','p')
join pg_namespace n on n.oid = c.relnamespace and n.nspname = e.schema_name
order by e.schema_name, e.table_name;

with expected(schema_name, table_name) as (values {",".join(table_values)})
select 'unique=' || e.schema_name || '.' || e.table_name || ':' || count(con.oid)::text
from expected e
left join pg_namespace n on n.nspname = e.schema_name
left join pg_class c on c.relname = e.table_name and c.relnamespace = n.oid
left join pg_constraint con on con.conrelid = c.oid and con.contype in ('p','u')
group by e.schema_name, e.table_name
order by e.schema_name, e.table_name;

with expected(schema_name, table_name) as (values {",".join(table_values)})
select 'redact=' || e.schema_name || '.' || e.table_name
from expected e
join information_schema.columns col
  on col.table_schema = e.schema_name
 and col.table_name = e.table_name
 and col.column_name = 'redact_after'
order by e.schema_name, e.table_name;

with expected(stage, role_name, schema_name) as (values {role_schema_values})
select 'own_grant=' || e.stage || ':' || e.role_name || ':' || e.schema_name
  || ':usage=' || has_schema_privilege(e.role_name, e.schema_name, 'USAGE')::text
  || ':inbox_insert=' || has_table_privilege(e.role_name, e.schema_name || '.inbox', 'INSERT')::text
  || ':outbox_insert=' || has_table_privilege(e.role_name, e.schema_name || '.outbox', 'INSERT')::text
from expected e
order by e.stage;

with domain_tables(table_name) as (values {domain_table_values})
select 'public_write=' || r.rolname || ':' || d.table_name
  || ':insert=' || case when to_regclass(d.table_name) is null then 'missing_table' else has_table_privilege(r.rolname, d.table_name, 'INSERT')::text end
  || ':update=' || case when to_regclass(d.table_name) is null then 'missing_table' else has_table_privilege(r.rolname, d.table_name, 'UPDATE')::text end
  || ':delete=' || case when to_regclass(d.table_name) is null then 'missing_table' else has_table_privilege(r.rolname, d.table_name, 'DELETE')::text end
from pg_roles r
cross join domain_tables d
where r.rolname = any(string_to_array('{role_csv}', ','))
order by r.rolname, d.table_name;

select 'final_grant=' || r.rolname
  || ':insert=' || has_table_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates', 'INSERT')::text
  || ':update=' || has_table_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates', 'UPDATE')::text
from pg_roles r
where r.rolname = any(string_to_array('{role_csv}', ','))
order by r.rolname;

select 'worker_api_final_grant=' || r.rolname
  || ':aggregate_select=' || case when to_regclass('{FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates', 'SELECT')::text end
  || ':aggregate_insert=' || case when to_regclass('{FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates', 'INSERT')::text end
  || ':aggregate_update=' || case when to_regclass('{FINAL_SCHEMA}.article_shadow_aggregates') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates', 'UPDATE')::text end
  || ':receipt_select=' || case when to_regclass('{FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.api_command_receipts', 'SELECT')::text end
  || ':receipt_insert=' || case when to_regclass('{FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.api_command_receipts', 'INSERT')::text end
  || ':receipt_update=' || case when to_regclass('{FINAL_SCHEMA}.api_command_receipts') is null then 'missing_table' else has_table_privilege(r.rolname, '{FINAL_SCHEMA}.api_command_receipts', 'UPDATE')::text end
  || ':aggregate_sequence_usage=' || case when to_regclass('{FINAL_SCHEMA}.article_shadow_aggregates_id_seq') is null then 'missing_sequence' else has_sequence_privilege(r.rolname, '{FINAL_SCHEMA}.article_shadow_aggregates_id_seq', 'USAGE')::text end
  || ':receipt_sequence_usage=' || case when to_regclass('{FINAL_SCHEMA}.api_command_receipts_id_seq') is null then 'missing_sequence' else has_sequence_privilege(r.rolname, '{FINAL_SCHEMA}.api_command_receipts_id_seq', 'USAGE')::text end
from pg_roles r
where r.rolname = '{WORKER_API_ROLE}';
"""


def parse_bool_token(token: str) -> bool:
    return token.strip().lower() in {"1", "on", "t", "true", "yes"}


def check_live_catalog(db_url: str, stages: list[tuple[str, str, str]]) -> list[dict]:
    code, stdout, stderr = run_psql(db_url, live_catalog_query(stages))
    if code != 0:
        return [{"name": "live_worker_uplift_catalog", "status": "fail", "error": "psql_failed", "returncode": code}]

    expected_schemas = {schema for _stage, _role, schema in stages} | {FINAL_SCHEMA, VIEWS_SCHEMA}
    expected_tables = {
        f"{schema}.{table}"
        for _stage, _role, schema in stages
        for table in COMMON_TABLES + STAGE_SPECIFIC_TABLES[schema]
    } | {f"{FINAL_SCHEMA}.{table}" for table in STAGE_SPECIFIC_TABLES[FINAL_SCHEMA]}
    expected_redaction_tables = expected_tables - {
        f"{schema}.transition_ledger" for _stage, _role, schema in stages
    }
    expected_redaction_tables |= {f"{schema}.transition_ledger" for _stage, _role, schema in stages}
    persistence_role = next(role for stage, role, _schema in stages if stage == "persistence")

    schemas = {line.split("=", 1)[1] for line in stdout.splitlines() if line.startswith("schema=")}
    tables = {line.split("=", 1)[1] for line in stdout.splitlines() if line.startswith("table=")}
    redaction_tables = {line.split("=", 1)[1] for line in stdout.splitlines() if line.startswith("redact=")}
    unique_failures = []
    own_grant_failures = []
    public_write_failures = []
    final_grant_failures = []
    worker_api_final_grant_failures = []

    for line in stdout.splitlines():
        if line.startswith("unique="):
            item, count_text = line.split("=", 1)[1].split(":", 1)
            if int(count_text) < 1:
                unique_failures.append(item)
        elif line.startswith("own_grant="):
            payload = line.split("=", 1)[1]
            parts = payload.split(":")
            values = [part.split("=", 1)[1] for part in parts[3:]]
            if not all(parse_bool_token(value) for value in values):
                own_grant_failures.append(":".join(parts[:3]))
        elif line.startswith("public_write="):
            payload = line.split("=", 1)[1]
            role_name, table_name, insert_value, update_value, delete_value = payload.split(":")
            values = [insert_value.split("=", 1)[1], update_value.split("=", 1)[1], delete_value.split("=", 1)[1]]
            if any(value == "missing_table" for value in values):
                public_write_failures.append(f"{role_name}:{table_name}:missing_table")
            elif any(parse_bool_token(value) for value in values):
                public_write_failures.append(f"{role_name}:{table_name}:write_allowed")
        elif line.startswith("final_grant="):
            payload = line.split("=", 1)[1]
            role_name, insert_value, update_value = payload.split(":")
            insert_allowed = parse_bool_token(insert_value.split("=", 1)[1])
            update_allowed = parse_bool_token(update_value.split("=", 1)[1])
            if role_name == persistence_role:
                if not (insert_allowed and update_allowed):
                    final_grant_failures.append(f"{role_name}:missing_insert_update")
            elif insert_allowed or update_allowed:
                final_grant_failures.append(f"{role_name}:unexpected_insert_update")
        elif line.startswith("worker_api_final_grant="):
            payload = line.split("=", 1)[1]
            parts = payload.split(":")
            values = [part.split("=", 1)[1] for part in parts[1:]]
            if not values or any(value.startswith("missing_") or not parse_bool_token(value) for value in values):
                worker_api_final_grant_failures.append(parts[0])

    failures = {
        "missing_schemas": sorted(expected_schemas - schemas),
        "missing_tables": sorted(expected_tables - tables),
        "missing_redact_after": sorted(expected_redaction_tables - redaction_tables),
        "missing_unique_or_primary_constraints": unique_failures,
        "own_grant_failures": own_grant_failures,
        "public_write_failures": public_write_failures,
        "final_grant_failures": final_grant_failures,
        "worker_api_final_grant_failures": worker_api_final_grant_failures,
    }
    failed = any(failures.values())
    return [
        {
            "name": "live_worker_uplift_catalog",
            "status": "fail" if failed else "pass",
            "schema_count": len(schemas),
            "table_count": len(tables),
            **failures,
            "stderr_present": bool(stderr.strip()) if failed else False,
        }
    ]


def own_insert_query(schema: str) -> str:
    suffix = uuid4().hex
    return f"""
begin;
insert into {schema}.inbox (
  message_id, pipeline_run_id, stage_execution_id, source_stage, source_message_id,
  entity_kind, entity_id, schema_version, operation_version, idempotency_key, payload_ref, payload_digest
) values (
  'permission-{suffix}-in', 'pipeline-{suffix}', 'stage-{suffix}', 'permission_test', 'source-{suffix}',
  'article', 'entity-{suffix}', 1, 1, 'idem-{suffix}-in', 'payload://permission/{suffix}/in', 'sha256:{suffix}'
);
insert into {schema}.outbox (
  outbox_message_id, pipeline_run_id, stage_execution_id, destination_stage, routing_key,
  entity_kind, entity_id, schema_version, operation_version, idempotency_key, payload_ref, payload_digest
) values (
  'permission-{suffix}-out', 'pipeline-{suffix}', 'stage-{suffix}', 'permission_test', 'permission.test',
  'article', 'entity-{suffix}', 1, 1, 'idem-{suffix}-out', 'payload://permission/{suffix}/out', 'sha256:{suffix}'
);
rollback;
"""


def final_insert_query() -> str:
    suffix = uuid4().hex
    return f"""
begin;
insert into {FINAL_SCHEMA}.article_shadow_aggregates (
  article_identity_hash, canonical_url_hash, original_url_hash, aggregate_version, payload_ref, payload_digest
) values (
  'identity-{suffix}', 'canonical-{suffix}', 'original-{suffix}', 1, 'payload://permission/{suffix}/final', 'sha256:{suffix}'
);
rollback;
"""


def forbidden_insert_query(schema: str) -> str:
    suffix = uuid4().hex
    return f"""
begin;
insert into {schema}.inbox (
  message_id, pipeline_run_id, stage_execution_id, source_stage, source_message_id,
  entity_kind, entity_id, schema_version, operation_version, idempotency_key, payload_ref, payload_digest
) values (
  'forbidden-{suffix}', 'pipeline-{suffix}', 'stage-{suffix}', 'permission_test', 'source-{suffix}',
  'article', 'entity-{suffix}', 1, 1, 'forbidden-{suffix}', 'payload://permission/{suffix}', 'sha256:{suffix}'
);
rollback;
"""


def public_insert_query() -> str:
    return "begin; insert into public.articles default values; rollback;"


def check_role_permissions(env_prefix: str) -> list[dict]:
    checks: list[dict] = []
    missing_env = []
    urls: dict[str, str] = {}
    for stage, _role, _schema in STAGES:
        env_name = f"{env_prefix}_{stage.upper()}_DB_URL"
        value = os.environ.get(env_name, "")
        if value:
            urls[stage] = value
        else:
            missing_env.append(env_name)
    if missing_env:
        return [
            {
                "name": "live_worker_uplift_role_permissions",
                "status": "fail",
                "error": "missing_stage_db_url_env",
                "missing_env": missing_env,
            }
        ]

    for index, (stage, _role, schema) in enumerate(STAGES):
        db_url = urls[stage]
        next_schema = STAGES[(index + 1) % len(STAGES)][2]
        own_code, _own_stdout, _own_stderr = run_psql(db_url, own_insert_query(schema))
        cross_code, _cross_stdout, _cross_stderr = run_psql(db_url, forbidden_insert_query(next_schema))
        public_code, _public_stdout, _public_stderr = run_psql(db_url, public_insert_query())
        final_code, _final_stdout, _final_stderr = run_psql(db_url, final_insert_query())
        final_expected_success = stage == "persistence"
        checks.append(
            {
                "name": f"role_permission_{stage}",
                "status": "pass"
                if own_code == 0
                and cross_code != 0
                and public_code != 0
                and ((final_code == 0) == final_expected_success)
                else "fail",
                "own_inbox_outbox_write": own_code == 0,
                "cross_stage_write_denied": cross_code != 0,
                "public_domain_write_denied": public_code != 0,
                "final_shadow_write_expected": final_expected_success,
                "final_shadow_write_matched": (final_code == 0) == final_expected_success,
            }
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db-url-env", default="NUTSNEWS_BACKEND_WORKER_UPLIFT_VALIDATION_DB_URL")
    parser.add_argument("--stage-db-url-env-prefix", default="NUTSNEWS_WORKER_UPLIFT_POSTGRES")
    parser.add_argument("--output", default="")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--permission-checks", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    checks = check_static_files()
    if args.offline:
        checks.append({"name": "live_worker_uplift_catalog", "status": "skipped_with_reason", "reason": "offline mode"})
        if args.permission_checks:
            checks.append({"name": "live_worker_uplift_role_permissions", "status": "skipped_with_reason", "reason": "offline mode"})
    else:
        stages = configured_stages(args.stage_db_url_env_prefix)
        target_db_url = os.environ.get(args.target_db_url_env, "")
        if not target_db_url:
            checks.append({"name": "live_worker_uplift_catalog", "status": "fail", "error": "missing_target_db_url_env"})
        else:
            checks.extend(check_live_catalog(target_db_url, stages))
        if args.permission_checks:
            checks.extend(check_role_permissions(args.stage_db_url_env_prefix))

    failed = [check["name"] for check in checks if check["status"] == "fail"]
    skipped = [check["name"] for check in checks if check["status"] == "skipped_with_reason"]
    report = {
        "status": "fail" if failed else "pass",
        "checked_at_utc": utc_now(),
        "tracking_issue": 86,
        "implementation_repo": "ramideltoro/nutsnews-backend",
        "database_boundary": "backend_postgres_primary_shadow",
        "public_domain_writes_allowed": False,
        "safe_metadata_only": True,
        "check_count": len(checks),
        "failed_checks": failed,
        "skipped_checks": skipped,
        "checks": checks,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.enforce and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
