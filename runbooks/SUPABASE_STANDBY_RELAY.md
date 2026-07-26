# Backend-to-Supabase Standby Relay

Issue: `ramideltoro/nutsnews#499`

## Decision

Use a backend-local trigger ledger relay to keep the existing production
Supabase project synchronized from private backend PostgreSQL.

Supabase must not connect inbound to backend PostgreSQL. Backend PostgreSQL
remains loopback/private, and the relay runs on the backend host through
repository-managed Ansible and systemd.

## What The Relay Does

- Source: backend PostgreSQL primary.
- Target: existing production Supabase standby.
- Direction: backend PostgreSQL to Supabase.
- Captures: insert, update, delete.
- sequence readiness: advances Supabase target sequences so the next value is
  above the source next value and target table max id.
- Evidence: safe metadata only.

The relay does not approve failover. It only keeps standby state moving.
Lag, parity, schema, sequence, writer-pause, split-brain fencing, and the final
GO/NO-GO decision remain separate issues under #521.

## Source Boundary

The Ansible role installs:

- private schema: `nutsnews_standby_relay`
- private event table: `nutsnews_standby_relay.events`
- trigger name: `nutsnews_standby_relay_capture`
- trigger function: `nutsnews_standby_relay.record_change()`
- read/ack/status functions for the relay role

The source ledger stores row payloads because it must replicate data, but the
relay must never print row data, SQL errors, database URLs, passwords, tokens,
or Supabase host/project metadata.

Backend public TCP/5432 remains forbidden. No inbound Supabase connection to
backend PostgreSQL is required.

## Runtime Boundary

Systemd units:

```text
nutsnews-supabase-standby-relay.service
nutsnews-supabase-standby-relay.timer
```

Runtime identity:

```text
nutsnews-standby-relay
```

Runtime files:

```text
/usr/local/libexec/nutsnews-supabase-standby-relay
/etc/nutsnews-supabase-standby-relay/manifest.json
/etc/nutsnews-supabase-standby-relay/relay.env
/var/lib/nutsnews/supabase-standby-relay/status.json
```

The environment file is root-owned, group-readable only by the relay group, and
mode `0640` so systemd can run continuously without printing or re-fetching
secrets.

## Protected Install

Run check mode before apply:

```bash
gh workflow run backend-supabase-standby-relay.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=check \
  -f relay_state=present
```

Apply only after reviewing check mode:

```bash
gh workflow run backend-supabase-standby-relay.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=apply \
  -f relay_state=present \
  -f confirm_apply=backend.nutsnews.com
```

The workflow uses the protected `production-backend` Environment and requires:

- `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY`
- `NUTSNEWS_BACKEND_KNOWN_HOSTS`
- `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD`
- `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL`

## Verification

Read-only backend verification:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'systemctl is-enabled nutsnews-supabase-standby-relay.timer && systemctl is-active nutsnews-supabase-standby-relay.timer'

ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'sudo -n systemctl status nutsnews-supabase-standby-relay.service --no-pager'

ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 \
  'sudo -n test -s /var/lib/nutsnews/supabase-standby-relay/status.json && echo relay_status_present'
```

Review logs for leaks. Acceptable evidence is limited to status, counts,
blocker codes, workflow URLs, and systemd state. Forbidden evidence includes
database URLs, passwords, row data, SQL text generated from row data, Supabase
project refs/hosts, and PostgreSQL error text.

## Rollback

Run rollback check:

```bash
gh workflow run backend-supabase-standby-relay.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=check \
  -f relay_state=absent
```

Apply rollback after review:

```bash
gh workflow run backend-supabase-standby-relay.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref main \
  -f run_mode=apply \
  -f relay_state=absent \
  -f confirm_apply=backend.nutsnews.com
```

Rollback removes the source triggers, private relay schema, systemd units,
runtime script, environment file, manifest, state directory, lock directory,
and relay OS identity through Ansible.

## Local Validation

```bash
python3 scripts/validate_backend_supabase_standby_relay.py
python3 scripts/backend_supabase_standby_relay.py --mode offline --enforce
python3 -m unittest tests.test_backend_supabase_standby_relay
cd ansible
ansible-playbook playbooks/backend_supabase_standby_relay.yml --syntax-check -i inventories/production/hosts.yml
```
