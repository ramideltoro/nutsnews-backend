#!/usr/bin/env bash
set -euo pipefail
# Fixed read-only exports. This account cannot select paths, SQL, or commands.
case "${1:-}" in
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
