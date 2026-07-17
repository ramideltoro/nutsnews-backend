#!/usr/bin/env bash
set -euo pipefail

case "${REHEARSAL_DATABASE:-}" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe rehearsal database name." >&2
    exit 1
    ;;
esac

remote_dir="${REMOTE_DIR:?REMOTE_DIR is required}"
source_ref="${SOURCE_REF:?SOURCE_REF is required}"
run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"

sudo -n true
sudo -n -u postgres psql -v ON_ERROR_STOP=1 postgres <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${REHEARSAL_DATABASE}';
DROP DATABASE IF EXISTS "${REHEARSAL_DATABASE}";
CREATE DATABASE "${REHEARSAL_DATABASE}";
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

sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$REHEARSAL_DATABASE" <<SQL
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$REHEARSAL_DATABASE" -f "$remote_dir/public-schema.sql" > "$remote_dir/schema-restore.log" 2>&1
sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$REHEARSAL_DATABASE" -f "$remote_dir/public-data.sql" > "$remote_dir/data-restore.log" 2>&1
sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$REHEARSAL_DATABASE" -f "$remote_dir/backend_postgres_restore_validation.sql" > "$remote_dir/validation.log" 2>&1

row_counts=$(sudo -n -u postgres psql -At -d "$REHEARSAL_DATABASE" <<'SQL'
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

completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ROW_COUNTS="$row_counts" COMPLETED_AT="$completed_at" python3 - <<'PY' > "$remote_dir/status.json"
import json
import os

status = {
    "status": "healthy",
    "database": "nutsnews_failover",
    "rehearsal_database": os.environ["REHEARSAL_DATABASE"],
    "source_of_truth": "Supabase remains the only production writer until a protected cutover is separately approved.",
    "last_restore_drill": {
        "status": "healthy",
        "source": "supabase_staging_public_schema",
        "source_project_ref": os.environ["SOURCE_REF"],
        "completed_at_utc": os.environ["COMPLETED_AT"],
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
        "row_counts": json.loads(os.environ["ROW_COUNTS"] or "{}"),
    },
    "replication": {
        "mode": "manual_dump_restore",
        "lag_status": "not_configured",
        "split_brain_guard": "no multi-writer topology",
    },
    "dashboard": {
        "tool": "Adminer",
        "access_boundary": "loopback_only_ssh_tunnel",
        "url": "http://127.0.0.1:8082/",
    },
}
print(json.dumps(status, indent=2, sort_keys=True))
PY

sudo -n install -m 0644 -o root -g root "$remote_dir/status.json" /var/lib/nutsnews/postgres/status.json
sudo -n /usr/local/bin/nutsnews-ops-dashboard-collect --output /var/www/nutsnews-ops-dashboard/status.json >/dev/null 2>&1 || true
sudo -n systemctl start nutsnews-metrics-textfile.service >/dev/null 2>&1 || true
for dump_path in "$remote_dir/public-schema.sql" "$remote_dir/public-data.sql"; do
  if [[ -e "$dump_path" ]]; then
    shred -u "$dump_path" 2>/dev/null || rm -f "$dump_path"
  fi
done

python3 - <<'PY'
import json
from pathlib import Path

status = json.loads(Path("/var/lib/nutsnews/postgres/status.json").read_text())
print("restore_status=" + status["last_restore_drill"]["status"])
print("completed_at_utc=" + status["last_restore_drill"]["completed_at_utc"])
print("rehearsal_database=" + status["rehearsal_database"])
print("replication_lag=" + status["replication"]["lag_status"])
print("row_count_keys=" + ",".join(sorted(status["last_restore_drill"]["row_counts"])))
PY
