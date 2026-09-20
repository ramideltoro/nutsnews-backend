#!/usr/bin/env bash
set -euo pipefail
# Fixed read-only exports. This account cannot select paths, SQL, or commands.
case "${1:-}" in
  worker-scheduler-*|worker-translation-*|worker-fetcher-*|worker-canonicalizer-*|worker-enrichment-*|worker-approval-*|worker-persistence-*|worker-publication-*)
    action=${1#worker-}; service=${action%%-*}; action=${action#*-}
    container="nutsnews-worker-uplift-${service}-1"
    case "$action" in
      revision) { docker inspect --format '{{.Image}} {{json .Config.Env}}' "$container"; sha256sum /etc/nutsnews-rabbitmq/worker-uplift-topology.json; } | sha256sum | cut -d' ' -f1 ;;
      app) exec docker cp "$container":/app/. - ;;
      config) exec docker inspect --format '{{json .Config.Env}}' "$container" ;;
      *) echo 'Unsupported worker export' >&2; exit 64 ;;
    esac ;;
  worker-database) exec runuser -u postgres -- /usr/bin/pg_dump --format=custom nutsnews_primary_shadow ;;
  worker-roles) exec runuser -u postgres -- /usr/bin/pg_dumpall --roles-only --no-role-passwords ;;
  broker-runtime) exec docker export nutsnews-rabbitmq ;;
  broker-definitions)
    file=/var/lib/nutsnews/rabbitmq-recovery/definitions.sanitized.json
    [[ $(stat -c %Y "$file") -ge $(($(date +%s)-691200)) ]] || exit 1
    exec cat "$file" ;;
  broker-queues) exec docker exec nutsnews-rabbitmq rabbitmqctl -q list_queues -p nutsnews-worker-uplift name messages ;;
  backend-revision) sha256sum /opt/nutsnews-worker-db-api/nutsnews_worker_db_api.py /etc/nutsnews-worker-db-api.env | sha256sum | cut -d' ' -f1 ;;
  backend-app) tar cf - -C /opt/nutsnews-worker-db-api . ;;
  backend-config) cat /etc/nutsnews-worker-db-api.env ;;
  backend-dependencies) tar cf - -C /usr/lib/python3/dist-packages psycopg2 ;;
  backend-database) exec runuser -u postgres -- /usr/bin/pg_dump --format=custom --no-owner --no-acl nutsnews_primary_shadow ;;
  fantasy-revision) docker inspect --format '{{.Image}}' fantasy-edge-v2-web-1 ;;
  fantasy-app) docker exec fantasy-edge-v2-web-1 tar cf - --exclude=node_modules -C /app . ;;
  fantasy-config) docker inspect --format '{{json .Config.Env}}' fantasy-edge-v2-web-1 ;;
  fantasy-database)
    backup=$(find /var/backups/fantasy-football-edge -type f -name database-before.dump -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)
    [[ -n "$backup" && $(stat -c %Y "$backup") -ge $(($(date +%s)-691200)) ]] || exit 1
    exec cat "$backup" ;;
  *) echo 'Unsupported recovery export' >&2; exit 64 ;;
esac
