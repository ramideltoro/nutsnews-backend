#!/usr/bin/env bash
set -euo pipefail

target_database="${TARGET_DATABASE:?TARGET_DATABASE is required}"
expected_head="${EXPECTED_MIGRATION_HEAD:?EXPECTED_MIGRATION_HEAD is required}"
expected_schema_version="${EXPECTED_SCHEMA_VERSION:?EXPECTED_SCHEMA_VERSION is required}"
source_commit="${SOURCE_COMMIT:?SOURCE_COMMIT is required}"
bundle_dir="${BUNDLE_DIR:?BUNDLE_DIR is required}"
backup_proof_url="${EXPECTED_BACKUP_PROOF_URL:?EXPECTED_BACKUP_PROOF_URL is required}"
report_path="$bundle_dir/backend-postgres-release-migration-report.json"
schema_snapshot="$bundle_dir/pre-migration-public-schema.sql"
apply_log="$bundle_dir/migration-apply.log"
selection_file="$bundle_dir/selected-migrations.txt"
roles_file="$bundle_dir/required-roles.txt"

if [[ "$target_database" != "nutsnews_primary_shadow" ]]; then
  echo "Release migration target must be nutsnews_primary_shadow." >&2
  exit 1
fi
if [[ ! "$expected_head" =~ ^[0-9]{14}$ || ! "$expected_schema_version" =~ ^[0-9]{14}$ ]]; then
  echo "Release migration contract identity is invalid." >&2
  exit 1
fi
if [[ ! "$source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Release migration source commit is invalid." >&2
  exit 1
fi
if [[ ! "$bundle_dir" =~ ^/tmp/nutsnews-backend-release-migration\.[A-Za-z0-9]+$ ]]; then
  echo "Release migration bundle path is outside the fixed temporary boundary." >&2
  exit 1
fi
if [[ ! "$backup_proof_url" =~ ^https://github\.com/ramideltoro/nutsnews-backend/actions/runs/[1-9][0-9]*$ ]]; then
  echo "Expected backup proof URL is invalid." >&2
  exit 1
fi

cleanup() {
  if [[ "${bundle_dir:-}" =~ ^/tmp/nutsnews-backend-release-migration\.[A-Za-z0-9]+$ ]]; then
    for path in "$schema_snapshot" "$apply_log"; do
      if [[ -f "$path" ]]; then
        shred -u "$path" 2>/dev/null || rm -f "$path"
      fi
    done
    rm -f \
      "$bundle_dir"/*.sql \
      "$bundle_dir"/*.json \
      "$bundle_dir"/*.txt \
      "$bundle_dir/backend_postgres_release_migrate_remote.sh" \
      2>/dev/null || true
    rmdir "$bundle_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sudo -n true
sudo -n -u postgres pg_isready -q -d "$target_database"

python3 - "$bundle_dir/bundle-manifest.json" "$source_commit" "$expected_head" "$expected_schema_version" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
source_commit, expected_head, expected_schema_version = sys.argv[2:]
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit("Migration bundle manifest is missing or invalid.") from exc

if (
    manifest.get("version") != 1
    or manifest.get("safe_metadata_only") is not True
    or manifest.get("source_repository") != "ramideltoro/nutsnews"
    or manifest.get("source_commit") != source_commit
    or manifest.get("target_database") != "nutsnews_primary_shadow"
    or manifest.get("migration_head") != expected_head
    or manifest.get("schema_version") != expected_schema_version
    or not re.fullmatch(r"[0-9]{14}", str(manifest.get("baseline_head") or ""))
    or not isinstance(manifest.get("baseline_contract"), dict)
):
    raise SystemExit("Migration bundle identity does not match the protected request.")

previous = manifest["baseline_head"]
items = manifest.get("migrations")
if not isinstance(items, list) or not items:
    raise SystemExit("Migration bundle contains no reviewed forward migrations.")
for item in items:
    filename = str(item.get("filename") or "")
    version = str(item.get("version") or "")
    if (
        not re.fullmatch(r"[0-9]{14}_[a-z0-9][a-z0-9_]*[a-z0-9]\.sql", filename)
        or not re.fullmatch(r"[0-9]{14}", version)
        or not filename.startswith(f"{version}_")
        or item.get("previous_head") != previous
        or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or ""))
    ):
        raise SystemExit("Migration bundle is not a continuous reviewed chain.")
    path = manifest_path.parent / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit("Migration bundle SQL does not match its reviewed SHA-256.")
    previous = version
if previous != expected_head:
    raise SystemExit("Migration bundle does not reach the protected requested head.")
PY

current_contract="$(sudo -n -u postgres psql -X -v ON_ERROR_STOP=1 -At -F $'\t' -d "$target_database" <<'SQL'
select
  legacy_schema_version,
  migration_head,
  expected_schema_fingerprint,
  actual_schema_fingerprint
from public.nutsnews_migration_schema_contract();
SQL
)"
IFS=$'\t' read -r current_schema_version current_head current_expected_fingerprint current_actual_fingerprint <<< "$current_contract"
if [[ ! "$current_head" =~ ^[0-9]{14}$ || ! "$current_schema_version" =~ ^[0-9]{14}$ ]]; then
  echo "Active backend did not return a valid existing migration contract." >&2
  exit 1
fi
if [[ ! "$current_expected_fingerprint" =~ ^[a-f0-9]{32}$ || ! "$current_actual_fingerprint" =~ ^[a-f0-9]{32}$ ]]; then
  echo "Active backend did not return valid existing schema fingerprints." >&2
  exit 1
fi

python3 - "$bundle_dir/bundle-manifest.json" "$current_schema_version" "$current_head" "$current_expected_fingerprint" "$current_actual_fingerprint" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
schema_version, current_head, expected_fingerprint, actual_fingerprint = sys.argv[2:]
if schema_version != manifest["schema_version"]:
    raise SystemExit("Active backend legacy schema marker does not match the approved release.")
if current_head == manifest["baseline_head"]:
    baseline = manifest["baseline_contract"]
    if (
        schema_version != baseline.get("schema_version")
        or expected_fingerprint != baseline.get("expected_schema_fingerprint")
        or actual_fingerprint != baseline.get("actual_schema_fingerprint")
    ):
        raise SystemExit("Active backend baseline contract differs from the exact reviewed pre-migration state.")
elif expected_fingerprint != actual_fingerprint:
    raise SystemExit("Active backend has unreviewed schema drift outside the exact baseline repair state.")
PY

python3 - "$bundle_dir/bundle-manifest.json" "$current_head" "$expected_head" > "$selection_file" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
current_head, expected_head = sys.argv[2:]
known_heads = [manifest["baseline_head"], *[item["version"] for item in manifest["migrations"]]]
if current_head not in known_heads:
    raise SystemExit("Active backend migration head is outside the reviewed forward chain.")
if known_heads.index(current_head) > known_heads.index(expected_head):
    raise SystemExit("Automatic reverse migrations are prohibited.")
for item in manifest["migrations"]:
    if current_head < item["version"] <= expected_head:
        print(item["filename"])
PY
mapfile -t selected_migrations < "$selection_file"

python3 - "$bundle_dir/bundle-manifest.json" "$current_head" "$expected_head" > "$roles_file" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
roles = set()
for item in manifest["migrations"]:
    if sys.argv[2] < item["version"] <= sys.argv[3]:
        roles.update(item["required_roles"])
print("\n".join(sorted(roles)))
PY
while IFS= read -r role; do
  [[ -z "$role" ]] && continue
  if ! sudo -n -u postgres psql -X -v ON_ERROR_STOP=1 -At postgres -v role="$role" <<'SQL' | grep -qx '1'; then
select count(*) from pg_catalog.pg_roles where rolname = :'role';
SQL
    echo "Required PostgreSQL role is missing; no migration was applied." >&2
    exit 1
  fi
done < "$roles_file"

proof_path="/var/lib/nutsnews/postgres/primary-shadow-backup-restore-proof.json"
sudo -n test -r "$proof_path"
sudo -n cat "$proof_path" | python3 -c '
import datetime as dt
import json
import sys

expected_url = sys.argv[1]
proof = json.load(sys.stdin)
completed = dt.datetime.fromisoformat(str(proof.get("completed_at_utc", "")).replace("Z", "+00:00"))
now = dt.datetime.now(dt.timezone.utc)
if (
    proof.get("status") != "pass"
    or proof.get("safe_metadata_only") is not True
    or proof.get("backup_source") != "backend_postgresql_restic_snapshot"
    or proof.get("restore_scope") != "backend_postgresql_primary_shadow_database"
    or proof.get("restore_target") != "nutsnews_primary_shadow_backup_restore_proof"
    or proof.get("validation_status") != "pass"
    or proof.get("backup_freshness_status") != "pass"
    or proof.get("restore_health_status") != "pass"
    or proof.get("validation_report") != expected_url
    or completed > now
    or now - completed > dt.timedelta(hours=2)
):
    raise SystemExit("Fresh exact backend PostgreSQL backup restore proof is required.")
' "$backup_proof_url"

# The encrypted logical backup/restore proof above is the durable rollback
# source. This local schema-only snapshot is never uploaded; only its checksum
# is retained as pre-change evidence.
umask 077
sudo -n -u postgres pg_dump -s --no-owner --no-privileges -d "$target_database" \
  | sudo -n tee "$schema_snapshot" > /dev/null
pre_schema_sha256="$(sudo -n sha256sum "$schema_snapshot" | awk '{print $1}')"
if [[ ! "$pre_schema_sha256" =~ ^[a-f0-9]{64}$ ]]; then
  echo "Unable to capture safe pre-migration schema evidence." >&2
  exit 1
fi

if (( ${#selected_migrations[@]} > 0 )); then
  sudo -n chown root:postgres "$bundle_dir"
  sudo -n chmod 0750 "$bundle_dir"
  psql_args=(
    -X
    -q
    -1
    -v ON_ERROR_STOP=1
    -d "$target_database"
    -c "set lock_timeout = '15s'; set statement_timeout = '10min'; select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('nutsnews:backend-release-migration', 0));"
  )
  for filename in "${selected_migrations[@]}"; do
    if [[ ! "$filename" =~ ^[0-9]{14}_[a-z0-9][a-z0-9_]*[a-z0-9]\.sql$ ]]; then
      echo "Migration bundle selected an unsafe filename." >&2
      exit 1
    fi
    sudo -n chown root:postgres "$bundle_dir/$filename"
    sudo -n chmod 0640 "$bundle_dir/$filename"
    psql_args+=(-f "$bundle_dir/$filename")
  done
  if ! sudo -n -u postgres psql "${psql_args[@]}" 2>&1 | sudo -n tee "$apply_log" > /dev/null; then
    echo "Transactional backend PostgreSQL release migration failed and was rolled back." >&2
    exit 1
  fi
fi

final_contract="$(sudo -n -u postgres psql -X -v ON_ERROR_STOP=1 -At -F $'\t' -d "$target_database" <<'SQL'
select
  legacy_schema_version,
  migration_head,
  expected_schema_fingerprint,
  actual_schema_fingerprint
from public.nutsnews_migration_schema_contract();
SQL
)"
IFS=$'\t' read -r final_schema_version final_head final_expected_fingerprint final_actual_fingerprint <<< "$final_contract"
if [[ "$final_schema_version" != "$expected_schema_version" ]] ||
  [[ "$final_head" != "$expected_head" ]] ||
  [[ ! "$final_expected_fingerprint" =~ ^[a-f0-9]{32}$ ]] ||
  [[ "$final_expected_fingerprint" != "$final_actual_fingerprint" ]]; then
  echo "Backend PostgreSQL post-migration schema contract verification failed." >&2
  exit 1
fi

CURRENT_HEAD="$current_head" \
FINAL_HEAD="$final_head" \
SCHEMA_VERSION="$final_schema_version" \
SOURCE_COMMIT="$source_commit" \
PRE_SCHEMA_SHA256="$pre_schema_sha256" \
BACKUP_PROOF_URL="$backup_proof_url" \
MIGRATION_COUNT="${#selected_migrations[@]}" \
python3 - <<'PY' > "$report_path"
import datetime as dt
import json
import os

report = {
    "version": 1,
    "status": "pass",
    "safe_metadata_only": True,
    "target_database": "nutsnews_primary_shadow",
    "source_commit": os.environ["SOURCE_COMMIT"],
    "starting_migration_head": os.environ["CURRENT_HEAD"],
    "migration_head": os.environ["FINAL_HEAD"],
    "schema_version": os.environ["SCHEMA_VERSION"],
    "applied_migration_count": int(os.environ["MIGRATION_COUNT"]),
    "pre_schema_sha256": os.environ["PRE_SCHEMA_SHA256"],
    "backup_proof_url": os.environ["BACKUP_PROOF_URL"],
    "transactional": True,
    "advisory_lock": "nutsnews:backend-release-migration",
    "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "rollback": "restore the exact encrypted backup proof snapshot or apply a reviewed forward repair",
}
print(json.dumps(report, indent=2, sort_keys=True))
PY

cat "$report_path"
