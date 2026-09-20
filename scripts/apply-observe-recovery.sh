#!/usr/bin/env bash
set -euo pipefail
mode=${1:?check or apply}
root=${2:?source checkout}
[[ "$mode" == check || "$mode" == apply ]]
bash -n "$root/scripts/observe-recovery-export.sh"
# Validate a copy first, preserving every existing virtual host and directive.
candidate=$(mktemp --suffix=.Caddyfile)
trap 'rm -f "$candidate"' EXIT
awk '
/^fantasy.ramideltoro.com \{/ { print; print "    log {"; print "        output file /var/log/caddy/access.log {"; print "            mode 0640"; print "            roll_size 10MiB"; print "            roll_keep 5"; print "            roll_keep_for 336h"; print "        }"; print "        format filter {"; print "            wrap json"; print "            fields {"; print "                request>remote_ip delete"; print "                request>client_ip delete"; print "                request>remote_port delete"; print "                request>headers delete"; print "                request>uri delete"; print "                resp_headers delete"; print "            }"; print "        }"; print "    }"; next } {print}
' /etc/caddy/Caddyfile > "$candidate"
# Marker avoids duplicating the site log on subsequent applies.
if [[ -e /etc/caddy/.observe-fantasy-access-managed ]]; then cp /etc/caddy/Caddyfile "$candidate"; fi
caddy validate --config "$candidate" --adapter caddyfile >/dev/null
systemctl is-active --quiet caddy postgresql@18-main docker nutsnews-worker-db-api
if [[ "$mode" == check ]]; then echo 'Recovery-only preflight passed'; exit 0; fi
install -m 0755 "$root/scripts/observe-recovery-export.sh" /usr/local/sbin/observe-recovery-export
id observe-recovery-export >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/observe-recovery-export --shell /bin/sh observe-recovery-export
install -d -m 0700 -o observe-recovery-export -g observe-recovery-export /var/lib/observe-recovery-export/.ssh
{ printf 'restrict,command="sudo -n /usr/local/sbin/observe-recovery-export \\"$SSH_ORIGINAL_COMMAND\\"" '; cat "$root/config/recovery/reader.pub"; } > /var/lib/observe-recovery-export/.ssh/authorized_keys
chown observe-recovery-export:observe-recovery-export /var/lib/observe-recovery-export/.ssh/authorized_keys
chmod 0600 /var/lib/observe-recovery-export/.ssh/authorized_keys
printf 'observe-recovery-export ALL=(root) NOPASSWD: /usr/local/sbin/observe-recovery-export *\n' >/etc/sudoers.d/observe-recovery-export
chmod 0440 /etc/sudoers.d/observe-recovery-export
visudo -cf /etc/sudoers.d/observe-recovery-export
# Chain verification to successful backups; preserve the independent daily timer.
install -d -m 0755 /etc/systemd/system/nutsnews-backup.service.d
printf '[Service]\nExecStartPost=/usr/local/sbin/nutsnews-backup verify\n' >/etc/systemd/system/nutsnews-backup.service.d/observe-verification.conf
systemctl daemon-reload
if ! cmp -s "$candidate" /etc/caddy/Caddyfile; then
  cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-observe-recovery
  install -m 0644 "$candidate" /etc/caddy/Caddyfile
  systemctl reload caddy
fi
touch /etc/caddy/.observe-fantasy-access-managed
systemctl is-active --quiet caddy postgresql@18-main docker nutsnews-worker-db-api
echo 'Recovery-only configuration applied; application processes preserved'
