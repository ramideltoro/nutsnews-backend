#!/usr/bin/env bash
set -euo pipefail
mode=${1:?check, apply or rollback}
[[ "$mode" == check || "$mode" == apply || "$mode" == rollback ]]
unit=nutsnews-metrics-textfile.timer
directory=/etc/systemd/system/$unit.d
file=$directory/observe-freshness.conf
systemctl cat "$unit" >/dev/null
systemctl is-active --quiet "$unit"
systemd-analyze calendar '*-*-* *:*:00' >/dev/null
if [[ "$mode" == check ]]; then echo 'Metrics cadence preflight passed'; exit 0; fi
if [[ "$mode" == rollback ]]; then
  rm -f "$file"
else
  install -d -m 0755 "$directory"
  candidate=$(mktemp)
  trap 'rm -f "$candidate"' EXIT
  printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:*:00\nAccuracySec=5s\n' > "$candidate"
  if cmp -s "$candidate" "$file"; then echo 'Metrics cadence already configured'; exit 0; fi
  install -m 0644 "$candidate" "$file"
fi
systemctl daemon-reload
# Restart only the timer; never restart a production application or collector.
systemctl restart "$unit"
systemctl is-active --quiet "$unit"
echo 'Metrics timer updated; application services unchanged'
