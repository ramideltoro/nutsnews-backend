#!/usr/bin/env bash
set -euo pipefail

target_database="${TARGET_DATABASE:-${REHEARSAL_DATABASE:-}}"
operation="${OPERATION:-setup}"

case "$target_database" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Unsafe target database name." >&2
    exit 1
    ;;
esac

: "${PUBLICATION_NAME:?PUBLICATION_NAME is required}"
: "${SLOT_NAME:?SLOT_NAME is required}"
: "${SUBSCRIPTION_NAME:?SUBSCRIPTION_NAME is required}"

case "$operation" in
  setup|teardown-dry-run|teardown) ;;
  *)
    echo "Unsupported operation." >&2
    exit 1
    ;;
esac

if [[ "$operation" == "setup" ]]; then
  : "${SOURCE_DB_URL:?SOURCE_DB_URL is required}"
fi

for identifier in "$PUBLICATION_NAME" "$SLOT_NAME" "$SUBSCRIPTION_NAME"; do
  case "$identifier" in
    ''|*[!a-z0-9_]*|[0-9]*)
      echo "Unsafe replication identifier." >&2
      exit 1
      ;;
  esac
done

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

subscription_present=false
existing_enabled=""
existing_slot=""
existing_publications=""
if [[ "$sub_exists" == "t" || "$sub_exists" == "true" || "$sub_exists" == "1" ]]; then
  subscription_present=true
  echo '{"phase":"verify_existing_subscription","safe_metadata_only":true}' >&2
  existing_subscription_state="$(sudo -n -u postgres psql -v ON_ERROR_STOP=1 -At -d "$target_database" \
    -v subscription="$SUBSCRIPTION_NAME" <<'SQL'
select subenabled::text || '|' || coalesce(subslotname, '') || '|' || array_to_string(subpublications, ',')
from pg_subscription
where subname = :'subscription';
SQL
)"
  IFS='|' read -r existing_enabled existing_slot existing_publications <<< "$existing_subscription_state"
fi

if [[ "$operation" == "teardown-dry-run" ]]; then
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  STARTED_AT="$started_at" \
  COMPLETED_AT="$completed_at" \
  TARGET_DATABASE="$target_database" \
  SUBSCRIPTION_NAME="$SUBSCRIPTION_NAME" \
  PUBLICATION_NAME="$PUBLICATION_NAME" \
  SLOT_NAME="$SLOT_NAME" \
  TARGET_TABLE_COUNT="$target_table_count" \
  SUBSCRIPTION_PRESENT="$subscription_present" \
  EXISTING_ENABLED="$existing_enabled" \
  EXISTING_SLOT="$existing_slot" \
  python3 - <<'PY'
import json
import os

print(json.dumps({
    "status": "pass",
    "operation": "teardown-dry-run",
    "started_at_utc": os.environ["STARTED_AT"],
    "completed_at_utc": os.environ["COMPLETED_AT"],
    "target_database": os.environ["TARGET_DATABASE"],
    "subscription": os.environ["SUBSCRIPTION_NAME"],
    "publication": os.environ["PUBLICATION_NAME"],
    "slot": os.environ["SLOT_NAME"],
    "target_public_table_count": int(os.environ["TARGET_TABLE_COUNT"] or "0"),
    "subscription_count": 1 if os.environ["SUBSCRIPTION_PRESENT"] == "true" else 0,
    "subscription_enabled": os.environ["EXISTING_ENABLED"] in {"1", "t", "true"},
    "subscription_slot_matches": os.environ["EXISTING_SLOT"] == os.environ["SLOT_NAME"],
    "planned_actions": [
        "disable subscription if enabled",
        "detach subscription from source slot",
        "drop backend subscription",
    ],
    "safe_metadata_only": True,
}, indent=2, sort_keys=True))
PY
  exit 0
fi

if [[ "$operation" == "teardown" ]]; then
  if [[ "$subscription_present" == "true" ]]; then
    if [[ "$existing_enabled" == "t" || "$existing_enabled" == "true" || "$existing_enabled" == "1" ]]; then
      sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
        -v subscription="$SUBSCRIPTION_NAME" <<'SQL'
ALTER SUBSCRIPTION :"subscription" DISABLE;
SQL
    fi
    if [[ -n "$existing_slot" ]]; then
      sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
        -v subscription="$SUBSCRIPTION_NAME" <<'SQL'
ALTER SUBSCRIPTION :"subscription" SET (slot_name = NONE);
SQL
    fi
    sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
      -v subscription="$SUBSCRIPTION_NAME" <<'SQL'
DROP SUBSCRIPTION IF EXISTS :"subscription";
SQL
  fi
  subscription_count="$(sudo -n -u postgres psql -At -d "$target_database" -v sub="$SUBSCRIPTION_NAME" <<'SQL'
select count(*)::int
from pg_subscription
where subname = :'sub';
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
  python3 - <<'PY'
import json
import os

subscription_count = int(os.environ["SUBSCRIPTION_COUNT"] or "0")
print(json.dumps({
    "status": "pass" if subscription_count == 0 else "blocked",
    "operation": "teardown",
    "started_at_utc": os.environ["STARTED_AT"],
    "completed_at_utc": os.environ["COMPLETED_AT"],
    "target_database": os.environ["TARGET_DATABASE"],
    "subscription": os.environ["SUBSCRIPTION_NAME"],
    "publication": os.environ["PUBLICATION_NAME"],
    "slot": os.environ["SLOT_NAME"],
    "target_public_table_count": int(os.environ["TARGET_TABLE_COUNT"] or "0"),
    "subscription_count": subscription_count,
    "safe_metadata_only": True,
}, indent=2, sort_keys=True))
PY
  exit 0
fi

if [[ "$subscription_present" == "true" ]]; then
  if [[ "$existing_enabled" != "t" && "$existing_enabled" != "true" && "$existing_enabled" != "1" ]]; then
    echo "Existing subscription is not enabled." >&2
    exit 1
  fi
  if [[ "$existing_slot" != "$SLOT_NAME" ]]; then
    echo "Existing subscription does not use the intended slot." >&2
    exit 1
  fi
  if [[ ",$existing_publications," != *",$PUBLICATION_NAME,"* ]]; then
    echo "Existing subscription does not use the intended publication." >&2
    exit 1
  fi
else
  echo '{"phase":"create_subscription","safe_metadata_only":true}' >&2
  sudo -n -u postgres psql -v ON_ERROR_STOP=1 -d "$target_database" \
    -v source_conn="$SOURCE_DB_URL" \
    -v publication="$PUBLICATION_NAME" \
    -v slot_name="$SLOT_NAME" \
    -v subscription="$SUBSCRIPTION_NAME" <<'SQL'
CREATE SUBSCRIPTION :"subscription"
CONNECTION :'source_conn'
PUBLICATION :"publication"
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
    "subscription_worker_present": os.environ["WORKER_PRESENT"] in {"1", "t", "true"},
    "copy_data": False,
    "create_slot": False,
    "safe_metadata_only": True,
}, indent=2, sort_keys=True))
PY
