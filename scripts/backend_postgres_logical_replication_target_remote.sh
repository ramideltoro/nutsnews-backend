#!/usr/bin/env bash
set -euo pipefail

target_database="${TARGET_DATABASE:-${REHEARSAL_DATABASE:-}}"

case "$target_database" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe target database name." >&2
    exit 1
    ;;
esac

: "${SOURCE_DB_URL:?SOURCE_DB_URL is required}"
: "${PUBLICATION_NAME:?PUBLICATION_NAME is required}"
: "${SLOT_NAME:?SLOT_NAME is required}"
: "${SUBSCRIPTION_NAME:?SUBSCRIPTION_NAME is required}"

for identifier in "$PUBLICATION_NAME" "$SLOT_NAME" "$SUBSCRIPTION_NAME"; do
  case "$identifier" in
    ''|*[!a-z0-9_]*|[0-9]*)
      echo "Unsafe replication identifier." >&2
      exit 1
      ;;
  esac
done

q_publication="\"$PUBLICATION_NAME\""
q_subscription="\"$SUBSCRIPTION_NAME\""
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

sudo -n -u postgres pg_isready -q -d "$target_database"

target_table_count="$(sudo -n -u postgres psql -At -d "$target_database" <<'SQL'
select count(*)::int
from information_schema.tables
where table_schema = 'public';
SQL
)"

sub_exists="$(sudo -n -u postgres psql -At -d "$target_database" -v sub="$SUBSCRIPTION_NAME" <<'SQL'
select exists(select 1 from pg_subscription where subname = :'sub');
SQL
)"

if [[ "$sub_exists" == "t" ]]; then
  sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
    -v source_conn="$SOURCE_DB_URL" <<SQL
alter subscription $q_subscription disable;
alter subscription $q_subscription connection :'source_conn';
alter subscription $q_subscription set publication $q_publication with (refresh = false);
alter subscription $q_subscription enable;
alter subscription $q_subscription refresh publication with (copy_data = false);
SQL
else
  sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
    -v source_conn="$SOURCE_DB_URL" \
    -v slot_name="$SLOT_NAME" <<SQL
create subscription $q_subscription
connection :'source_conn'
publication $q_publication
with (
  slot_name = :'slot_name',
  create_slot = false,
  copy_data = false,
  enabled = true
);
SQL
fi

subscription_count="$(sudo -n -u postgres psql -At -d "$target_database" -v sub="$SUBSCRIPTION_NAME" <<'SQL'
select count(*)::int
from pg_subscription
where subname = :'sub';
SQL
)"

worker_present="$(sudo -n -u postgres psql -At -d "$target_database" -v sub="$SUBSCRIPTION_NAME" <<'SQL'
select exists(
  select 1
  from pg_stat_subscription
  where subname = :'sub'
    and pid is not null
);
SQL
)"

completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_AT="$started_at" \
COMPLETED_AT="$completed_at" \
TARGET_DATABASE="$target_database" \
SUBSCRIPTION_NAME="$SUBSCRIPTION_NAME" \
PUBLICATION_NAME="$PUBLICATION_NAME" \
SLOT_NAME="$SLOT_NAME" \
TARGET_TABLE_COUNT="$target_table_count" \
SUBSCRIPTION_COUNT="$subscription_count" \
WORKER_PRESENT="$worker_present" \
python3 - <<'PY'
import json
import os

print(json.dumps({
    "status": "pass" if os.environ["SUBSCRIPTION_COUNT"] == "1" else "blocked",
    "started_at_utc": os.environ["STARTED_AT"],
    "completed_at_utc": os.environ["COMPLETED_AT"],
    "target_database": os.environ["TARGET_DATABASE"],
    "rehearsal_database": os.environ["TARGET_DATABASE"],
    "subscription": os.environ["SUBSCRIPTION_NAME"],
    "publication": os.environ["PUBLICATION_NAME"],
    "slot": os.environ["SLOT_NAME"],
    "target_public_table_count": int(os.environ["TARGET_TABLE_COUNT"] or "0"),
    "subscription_count": int(os.environ["SUBSCRIPTION_COUNT"] or "0"),
    "subscription_worker_present": os.environ["WORKER_PRESENT"] == "t",
    "copy_data": False,
    "create_slot": False,
    "safe_metadata_only": True,
}, indent=2, sort_keys=True))
PY
