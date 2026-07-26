# Supabase Standby Sync Relay

Issue #499 adds a private backend-side relay that keeps the existing production
Supabase database synchronized from backend PostgreSQL primary. Backend
PostgreSQL remains the normal read/write database. Supabase is only the
backup/hot-standby target until a later owner-approved failover.

## Safety Boundary

- The relay runs on the backend host through systemd.
- Source reads use backend PostgreSQL over `127.0.0.1:5432`.
- Target writes go outbound to the existing production Supabase PostgreSQL URL.
- No inbound Supabase connection to backend PostgreSQL is required.
- App and worker services do not receive Supabase write credentials.
- The relay blocks before mutating Supabase if schema or table identity checks
  fail.
- Reports contain safe metadata only: check IDs, counts, hashes, status, and
  bounded object names.

## Installation

Use the protected backend apply workflow:

1. Set `NUTSNEWS_BACKEND_SUPABASE_SYNC_RELAY_ENABLED=true` in the
   `production-backend` environment.
2. Confirm `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL` and
   `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD` exist in the same
   protected environment.
3. Run `Protected Backend Ansible Apply` in `check` mode.
4. Review the check-mode output.
5. Run `Protected Backend Ansible Apply` in `apply` mode with
   `confirm_apply=backend.nutsnews.com`.

The workflow renders `/etc/nutsnews-supabase-sync-relay/relay.env` as
`root:nutsnews-standby-relay` with mode `0640`.

## Runtime

- Service: `nutsnews-supabase-sync-relay.service`
- Timer: `nutsnews-supabase-sync-relay.timer`
- Default interval: 30 seconds
- Last safe report: `/var/lib/nutsnews/supabase-sync-relay/last-run.json`

Useful backend-host checks:

```bash
systemctl status nutsnews-supabase-sync-relay.timer
systemctl status nutsnews-supabase-sync-relay.service
journalctl -u nutsnews-supabase-sync-relay.service -n 100 --no-pager
sudo -u nutsnews-standby-relay python3 /usr/local/libexec/nutsnews-backend-supabase-sync-relay.py \
  --contract /etc/nutsnews-supabase-sync-relay/contract.json \
  --mode dry-run \
  --enforce
```

## Acceptance Evidence

For #499, record:

- PR and merge SHA.
- Local validator/test output.
- Protected backend apply run URL for check and apply mode when enabled.
- Relay `last-run.json` summary showing `status=pass`, `safe_metadata_only=true`,
  `backend_postgresql_remains_primary=true`, and no app/worker Supabase write
  credential injection.

This relay does not approve failover. Later issues still gate failover on lag,
parity, schema, sequence safety, writer pause, and split-brain checks.
