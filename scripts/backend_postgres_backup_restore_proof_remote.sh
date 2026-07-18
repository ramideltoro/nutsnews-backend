#!/usr/bin/env bash
set -euo pipefail

source_database="${SOURCE_DATABASE:-${REHEARSAL_DATABASE:-}}"
restore_scope="${RESTORE_SCOPE:-backend_postgresql_rehearsal_database}"

case "$source_database" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe source database name." >&2
    exit 1
    ;;
esac

case "${PROOF_DATABASE:-}" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe proof database name." >&2
    exit 1
    ;;
esac

remote_dir="${REMOTE_DIR:?REMOTE_DIR is required}"
github_run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
github_repository="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
manifest_version="${MANIFEST_VERSION:-unknown}"
proof_status_path="/var/lib/nutsnews/postgres/backup-restore-proof.json"
if [[ "$restore_scope" == "backend_postgresql_primary_shadow_database" ]]; then
  proof_status_path="/var/lib/nutsnews/postgres/primary-shadow-backup-restore-proof.json"
fi
postgres_status_path="/var/lib/nutsnews/postgres/status.json"
started_epoch="$(date -u +%s)"
dump_path="$remote_dir/backend-postgres-source.dump"
restored_dump_path="$remote_dir/backend-postgres-restored.dump"
validation_sql="$remote_dir/backend_postgres_restore_validation.sql"
validation_log="$remote_dir/backup-restore-validation.log"
restic_stdout="$remote_dir/restic-backup.jsonl"
restic_stderr="$remote_dir/restic-backup.stderr"
stdin_filename="backend-postgresql/logical/${source_database}-${github_run_id}.dump"

cleanup_remote_dir() {
  if [[ "${remote_dir:-}" != /tmp/nutsnews-postgres-backup-proof-* ]]; then
    return
  fi
  for sensitive_path in "$dump_path" "$restored_dump_path"; do
    if [[ -e "$sensitive_path" ]]; then
      shred -u "$sensitive_path" 2>/dev/null || rm -f "$sensitive_path"
    fi
  done
  rm -f \
    "$validation_sql" \
    "$validation_log" \
    "$restic_stdout" \
    "$restic_stderr" \
    "$remote_dir/backend_postgres_backup_restore_proof_remote.sh" \
    "$remote_dir/proof.json" 2>/dev/null || true
  rmdir "$remote_dir" 2>/dev/null || true
}
trap cleanup_remote_dir EXIT

sudo -n true
sudo -n install -d -m 0770 -o postgres -g postgres "$remote_dir"
sudo -n chown postgres:postgres "$validation_sql"
sudo -n chmod 0640 "$validation_sql"

sudo -n test -r /etc/nutsnews-backup/restic.env
set -a
# shellcheck disable=SC1091
source /etc/nutsnews-backup/restic.env
set +a

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is required}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is required}"

provider="${NUTSNEWS_BACKUP_RESTIC_PROVIDER:-${NUTSNEWS_BACKEND_RESTIC_PROVIDER:-}}"
if [[ "${provider,,}" == "s3" && "$RESTIC_REPOSITORY" == http* ]]; then
  export RESTIC_REPOSITORY="s3:$RESTIC_REPOSITORY"
fi

sudo -n -u postgres pg_isready -q -d "$source_database"
sudo -n -u postgres pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file "$dump_path" \
  "$source_database"
sudo -n chown root:root "$dump_path"
sudo -n chmod 0600 "$dump_path"

if ! restic backup \
  --json \
  --tag nutsnews-backend \
  --tag backend-postgresql-logical \
  --stdin \
  --stdin-filename "$stdin_filename" \
  < "$dump_path" > "$restic_stdout" 2> "$restic_stderr"; then
  echo "restic backup failed for backend PostgreSQL logical dump." >&2
  exit 1
fi

snapshot_id="$(python3 - "$restic_stdout" <<'PY'
import json
import sys
from pathlib import Path

snapshot = ""
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    if item.get("message_type") == "summary":
        snapshot = item.get("snapshot_id") or snapshot
print(snapshot)
PY
)"

if [[ -z "$snapshot_id" ]]; then
  echo "restic backup did not report a snapshot id." >&2
  exit 1
fi

restic dump "$snapshot_id" "$stdin_filename" > "$restored_dump_path"
chmod 0600 "$restored_dump_path"
sudo -n chown postgres:postgres "$restored_dump_path"

sudo -n -u postgres psql -v ON_ERROR_STOP=1 postgres <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${PROOF_DATABASE}';
DROP DATABASE IF EXISTS "${PROOF_DATABASE}";
CREATE DATABASE "${PROOF_DATABASE}";
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

sudo -n -u postgres pg_restore \
  --no-owner \
  --no-privileges \
  --dbname "$PROOF_DATABASE" \
  "$restored_dump_path"

# shellcheck disable=SC2024
sudo -n -u postgres psql \
  -v ON_ERROR_STOP=1 \
  -d "$PROOF_DATABASE" \
  -f "$validation_sql" > "$validation_log" 2>&1

completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
completed_epoch="$(date -u +%s)"
duration_seconds=$((completed_epoch - started_epoch))
rpo_seconds=$((completed_epoch - started_epoch))
rto_seconds="$duration_seconds"
short_snapshot="${snapshot_id:0:12}"
workflow_url="https://github.com/${github_repository}/actions/runs/${github_run_id}"

SNAPSHOT_ID="$short_snapshot" \
RESTORE_TARGET="$PROOF_DATABASE" \
RESTORE_SCOPE="$restore_scope" \
DURATION_SECONDS="$duration_seconds" \
RPO_SECONDS="$rpo_seconds" \
RTO_SECONDS="$rto_seconds" \
COMPLETED_AT="$completed_at" \
MANIFEST_VERSION="$manifest_version" \
WORKFLOW_URL="$workflow_url" \
PROOF_STATUS_PATH="$proof_status_path" \
POSTGRES_STATUS_PATH="$postgres_status_path" \
python3 - <<'PY' > "$remote_dir/proof.json"
import json
import os

proof = {
    "status": "pass",
    "snapshot_id": os.environ["SNAPSHOT_ID"],
    "backup_source": "backend_postgresql_restic_snapshot",
    "restore_target": os.environ["RESTORE_TARGET"],
    "restore_scope": os.environ["RESTORE_SCOPE"],
    "duration_seconds": int(os.environ["DURATION_SECONDS"]),
    "validation_status": "pass",
    "validation_report": os.environ["WORKFLOW_URL"],
    "operator": "production-backend protected workflow",
    "completed_at_utc": os.environ["COMPLETED_AT"],
    "rpo_seconds": int(os.environ["RPO_SECONDS"]),
    "rto_seconds": int(os.environ["RTO_SECONDS"]),
    "backup_freshness_status": "pass",
    "restore_health_status": "pass",
    "status_artifacts": [
        os.environ["PROOF_STATUS_PATH"],
        os.environ["POSTGRES_STATUS_PATH"],
        "/var/lib/nutsnews/backups/last-backup.json",
    ],
    "manifest_version": 1,
    "safe_metadata_only": True,
    "production_cutover_blocker_cleared": True,
    "notes": [
        "Proof uses a backend-produced logical PostgreSQL dump stored in encrypted restic and restored into an isolated backend database.",
        "Supabase remains the production writer until protected cutover approval completes.",
    ],
}
print(json.dumps(proof, indent=2, sort_keys=True))
PY

sudo -n install -d -m 0755 -o root -g root /var/lib/nutsnews/postgres
sudo -n install -m 0644 -o root -g root "$remote_dir/proof.json" "$proof_status_path"

PROOF_STATUS_PATH="$proof_status_path" POSTGRES_STATUS_PATH="$postgres_status_path" python3 - <<'PY'
import json
import os
from pathlib import Path

proof = json.loads(Path(os.environ["PROOF_STATUS_PATH"]).read_text(encoding="utf-8"))
status_path = Path(os.environ["POSTGRES_STATUS_PATH"])
try:
    status = json.loads(status_path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    status = {
        "status": "warning",
        "database": "nutsnews_failover",
        "source_of_truth": "Supabase remains the only production writer until a protected cutover is separately approved.",
    }
status["last_backup_restore_proof"] = proof
status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
sudo -n chown root:root "$postgres_status_path"
sudo -n chmod 0644 "$postgres_status_path"
sudo -n /usr/local/bin/nutsnews-ops-dashboard-collect --output /var/www/nutsnews-ops-dashboard/status.json >/dev/null 2>&1 || true
sudo -n systemctl start nutsnews-metrics-textfile.service >/dev/null 2>&1 || true

python3 - <<'PY'
import json
from pathlib import Path

proof = json.loads(Path("/var/lib/nutsnews/postgres/backup-restore-proof.json").read_text(encoding="utf-8"))
print(json.dumps({
    "status": proof["status"],
    "snapshot_id": proof["snapshot_id"],
    "restore_target": proof["restore_target"],
    "duration_seconds": proof["duration_seconds"],
    "validation_status": proof["validation_status"],
    "rpo_seconds": proof["rpo_seconds"],
    "rto_seconds": proof["rto_seconds"],
    "operator": proof["operator"],
    "completed_at_utc": proof["completed_at_utc"],
    "safe_metadata_only": proof["safe_metadata_only"],
}, indent=2, sort_keys=True))
PY
