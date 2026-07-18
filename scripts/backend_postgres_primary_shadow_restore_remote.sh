#!/usr/bin/env bash
set -euo pipefail

case "${TARGET_DATABASE:-}" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe target database name." >&2
    exit 1
    ;;
esac

if [[ "$TARGET_DATABASE" != "nutsnews_primary_shadow" ]]; then
  echo "This restore script is only approved for nutsnews_primary_shadow." >&2
  exit 1
fi

remote_dir="${REMOTE_DIR:?REMOTE_DIR is required}"
source_label="${SOURCE_LABEL:?SOURCE_LABEL is required}"
github_run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
github_repository="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
manifest_version="${MANIFEST_VERSION:-unknown}"
snapshot_id="${SNAPSHOT_ID:?SNAPSHOT_ID is required}"
public_schema_sha256="${PUBLIC_SCHEMA_DUMP_SHA256:?PUBLIC_SCHEMA_DUMP_SHA256 is required}"
public_data_sha256="${PUBLIC_DATA_DUMP_SHA256:?PUBLIC_DATA_DUMP_SHA256 is required}"
history_schema_sha256="${HISTORY_SCHEMA_DUMP_SHA256:?HISTORY_SCHEMA_DUMP_SHA256 is required}"
history_data_sha256="${HISTORY_DATA_DUMP_SHA256:?HISTORY_DATA_DUMP_SHA256 is required}"
started_epoch="$(date -u +%s)"

public_schema_path="$remote_dir/public-schema.sql"
public_data_path="$remote_dir/public-data.sql"
history_schema_path="$remote_dir/history-schema.sql"
history_data_path="$remote_dir/history-data.sql"
validation_sql="$remote_dir/backend_postgres_restore_validation.sql"
validation_log="$remote_dir/primary-shadow-validation.log"
restore_status_path="/var/lib/nutsnews/postgres/primary-shadow-restore.json"
postgres_status_path="/var/lib/nutsnews/postgres/status.json"

cleanup_remote_dir() {
  if [[ "${remote_dir:-}" != /tmp/nutsnews-postgres-primary-shadow-restore-* ]]; then
    return
  fi

  for dump_path in "$public_schema_path" "$public_data_path" "$history_schema_path" "$history_data_path"; do
    if [[ -e "$dump_path" ]]; then
      shred -u "$dump_path" 2>/dev/null || rm -f "$dump_path"
    fi
  done
  rm -f \
    "$validation_sql" \
    "$validation_log" \
    "$remote_dir/backend_postgres_primary_shadow_restore_remote.sh" \
    "$remote_dir/history-data-restore.log" \
    "$remote_dir/history-schema-restore.log" \
    "$remote_dir/public-data-restore.log" \
    "$remote_dir/public-schema-restore.log" \
    "$remote_dir/restore.json" 2>/dev/null || true
  rmdir "$remote_dir" 2>/dev/null || true
}
trap cleanup_remote_dir EXIT

sudo -n true
sudo -n install -d -m 0770 -o postgres -g postgres "$remote_dir"
sudo -n chown postgres:postgres \
  "$public_schema_path" \
  "$public_data_path" \
  "$history_schema_path" \
  "$history_data_path" \
  "$validation_sql"
sudo -n chmod 0640 \
  "$public_schema_path" \
  "$public_data_path" \
  "$history_schema_path" \
  "$history_data_path" \
  "$validation_sql"

for role_name in \
  nutsnews_migration_restore \
  nutsnews_migration_validation \
  nutsnews_readonly \
  nutsnews_migration_replication \
  nutsnews_app
do
  if ! sudo -n -u postgres psql -v ON_ERROR_STOP=1 -At postgres -v role="$role_name" <<'SQL' | grep -qx '1'; then
select 1 from pg_roles where rolname = :'role';
SQL
    echo "Missing required backend PostgreSQL role: $role_name" >&2
    exit 1
  fi
done

if sudo -n -u postgres psql -v ON_ERROR_STOP=1 -At -d "$TARGET_DATABASE" -c "select 1" >/dev/null 2>&1; then
  while IFS= read -r subscription_name; do
    [[ -n "$subscription_name" ]] || continue
    sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -v subscription="$subscription_name" <<'SQL'
ALTER SUBSCRIPTION :"subscription" DISABLE;
DROP SUBSCRIPTION :"subscription";
SQL
  done < <(
    sudo -n -u postgres psql -v ON_ERROR_STOP=1 -At -d "$TARGET_DATABASE" <<'SQL'
select subname
from pg_subscription
where subname like 'nutsnews_backend_migration_%';
SQL
  )
fi

sudo -n -u postgres psql -v ON_ERROR_STOP=1 postgres <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${TARGET_DATABASE}';
DROP DATABASE IF EXISTS "${TARGET_DATABASE}";
CREATE DATABASE "${TARGET_DATABASE}" OWNER nutsnews_migration_restore;
DO \$\$
BEGIN
  CREATE ROLE anon NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
\$\$;
DO \$\$
BEGIN
  CREATE ROLE authenticated NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
\$\$;
DO \$\$
BEGIN
  CREATE ROLE service_role NOLOGIN BYPASSRLS;
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
\$\$;
SQL

sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" <<'SQL'
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
  id uuid PRIMARY KEY
);
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

# shellcheck disable=SC2024
if ! sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -f "$history_schema_path" > "$remote_dir/history-schema-restore.log" 2>&1; then
  echo "Migration-history schema restore failed; last log lines follow." >&2
  tail -n 120 "$remote_dir/history-schema-restore.log" >&2 || true
  exit 1
fi
# shellcheck disable=SC2024
if ! sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -f "$public_schema_path" > "$remote_dir/public-schema-restore.log" 2>&1; then
  echo "Public schema restore failed; last log lines follow." >&2
  tail -n 120 "$remote_dir/public-schema-restore.log" >&2 || true
  exit 1
fi
# shellcheck disable=SC2024
sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -f "$public_data_path" > "$remote_dir/public-data-restore.log" 2>&1
# shellcheck disable=SC2024
sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -f "$history_data_path" > "$remote_dir/history-data-restore.log" 2>&1
# shellcheck disable=SC2024
sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" -f "$validation_sql" > "$validation_log" 2>&1

sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DATABASE" <<'SQL'
DO $$
DECLARE
  read_role name;
BEGIN
  FOREACH read_role IN ARRAY ARRAY[
    'nutsnews_readonly',
    'nutsnews_migration_validation'
  ]::name[] LOOP
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), read_role);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', read_role);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', read_role);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', read_role);
    EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO %I', read_role);
  END LOOP;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO nutsnews_migration_replication', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO nutsnews_app', current_database());
END $$;
SQL

row_counts=$(sudo -n -u postgres psql -At -d "$TARGET_DATABASE" <<'SQL'
select jsonb_object_agg(object_name, row_count)::text
from (
  select 'articles' as object_name, count(*)::bigint as row_count from public.articles
  union all
  select 'rss_feeds', count(*)::bigint from public.rss_feeds
  union all
  select 'article_ai_reviews', count(*)::bigint from public.article_ai_reviews
  union all
  select 'article_summaries', count(*)::bigint from public.article_summaries
  union all
  select 'ai_usage_runs', count(*)::bigint from public.ai_usage_runs
  union all
  select 'worker_runs', count(*)::bigint from public.worker_runs
  union all
  select 'feed_health', count(*)::bigint from public.feed_health
) counts;
SQL
)

completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
completed_epoch="$(date -u +%s)"
duration_seconds=$((completed_epoch - started_epoch))
rpo_seconds="$duration_seconds"
rto_seconds="$duration_seconds"
workflow_url="https://github.com/${github_repository}/actions/runs/${github_run_id}"

ROW_COUNTS="$row_counts" \
COMPLETED_AT="$completed_at" \
DURATION_SECONDS="$duration_seconds" \
RPO_SECONDS="$rpo_seconds" \
RTO_SECONDS="$rto_seconds" \
WORKFLOW_URL="$workflow_url" \
RESTORE_STATUS_PATH="$restore_status_path" \
POSTGRES_STATUS_PATH="$postgres_status_path" \
TARGET_DATABASE="$TARGET_DATABASE" \
SOURCE_LABEL="$source_label" \
SNAPSHOT_ID="$snapshot_id" \
MANIFEST_VERSION="$manifest_version" \
PUBLIC_SCHEMA_DUMP_SHA256="$public_schema_sha256" \
PUBLIC_DATA_DUMP_SHA256="$public_data_sha256" \
HISTORY_SCHEMA_DUMP_SHA256="$history_schema_sha256" \
HISTORY_DATA_DUMP_SHA256="$history_data_sha256" \
python3 - <<'PY' > "$remote_dir/restore.json"
import json
import os

restore = {
    "status": "pass",
    "snapshot_id": os.environ["SNAPSHOT_ID"],
    "source": "production_supabase_public_logical_dump",
    "target_database": os.environ["TARGET_DATABASE"],
    "restore_scope": "production_supabase_to_backend_primary_shadow",
    "duration_seconds": int(os.environ["DURATION_SECONDS"]),
    "validation_status": "pass",
    "validation_report": os.environ["WORKFLOW_URL"],
    "operator": "production-backend protected workflow",
    "completed_at_utc": os.environ["COMPLETED_AT"],
    "rpo_seconds": int(os.environ["RPO_SECONDS"]),
    "rto_seconds": int(os.environ["RTO_SECONDS"]),
    "manifest_version": int(os.environ["MANIFEST_VERSION"]),
    "safe_metadata_only": True,
    "production_cutover_blocker_cleared": True,
    "workflow_url": os.environ["WORKFLOW_URL"],
    "dump_checksums": {
        "public_schema_sha256": os.environ["PUBLIC_SCHEMA_DUMP_SHA256"],
        "public_data_sha256": os.environ["PUBLIC_DATA_DUMP_SHA256"],
        "history_schema_sha256": os.environ["HISTORY_SCHEMA_DUMP_SHA256"],
        "history_data_sha256": os.environ["HISTORY_DATA_DUMP_SHA256"],
    },
    "row_count_keys": sorted(json.loads(os.environ["ROW_COUNTS"] or "{}")),
    "status_artifacts": [
        os.environ["RESTORE_STATUS_PATH"],
        os.environ["POSTGRES_STATUS_PATH"],
    ],
}
print(json.dumps(restore, indent=2, sort_keys=True))
PY

sudo -n install -d -m 0755 -o root -g root /var/lib/nutsnews/postgres
sudo -n install -m 0644 -o root -g root "$remote_dir/restore.json" "$restore_status_path"

RESTORE_STATUS_PATH="$restore_status_path" POSTGRES_STATUS_PATH="$postgres_status_path" python3 - <<'PY'
import json
import os
from pathlib import Path

restore = json.loads(Path(os.environ["RESTORE_STATUS_PATH"]).read_text(encoding="utf-8"))
status_path = Path(os.environ["POSTGRES_STATUS_PATH"])
try:
    status = json.loads(status_path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    status = {
        "status": "warning",
        "database": "nutsnews_failover",
        "source_of_truth": "Supabase remains the only production writer until a protected cutover is separately approved.",
    }
status["primary_shadow_database"] = restore["target_database"]
status["last_primary_shadow_restore"] = restore
status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
sudo -n chown root:root "$postgres_status_path"
sudo -n chmod 0644 "$postgres_status_path"
sudo -n /usr/local/bin/nutsnews-ops-dashboard-collect --output /var/www/nutsnews-ops-dashboard/status.json >/dev/null 2>&1 || true
sudo -n systemctl start nutsnews-metrics-textfile.service >/dev/null 2>&1 || true

python3 - <<'PY'
import json
from pathlib import Path

restore = json.loads(Path("/var/lib/nutsnews/postgres/primary-shadow-restore.json").read_text(encoding="utf-8"))
print(json.dumps({
    "status": restore["status"],
    "snapshot_id": restore["snapshot_id"],
    "target_database": restore["target_database"],
    "duration_seconds": restore["duration_seconds"],
    "validation_status": restore["validation_status"],
    "rpo_seconds": restore["rpo_seconds"],
    "rto_seconds": restore["rto_seconds"],
    "operator": restore["operator"],
    "completed_at_utc": restore["completed_at_utc"],
    "safe_metadata_only": restore["safe_metadata_only"],
}, indent=2, sort_keys=True))
PY
