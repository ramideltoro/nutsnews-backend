#!/usr/bin/env bash
set -euo pipefail
mode="${1:-check}"
case "$mode" in check) apply=false ;; apply) apply=true ;; *) echo 'Expected check or apply' >&2; exit 2 ;; esac
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Fixed database and subscription only. Never accept connection URLs or identifiers
# from workflow inputs, never change the publisher, and never print subconninfo.
sudo -n -u postgres psql -X -q -v ON_ERROR_STOP=1 -v apply="$apply" \
  --dbname=nutsnews_restore_rehearsal \
  < "$root/pause-obsolete-rehearsal-subscription.sql"
