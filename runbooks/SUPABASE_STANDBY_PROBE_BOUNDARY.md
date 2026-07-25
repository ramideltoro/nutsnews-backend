# Supabase Standby Forced-Command Probe Boundary

This runbook records the replacement architecture for backend issues #333-#340
and app issue `ramideltoro/nutsnews#496`. It supersedes the paid
DigitalOcean/self-hosted-runner design from #323-#331.

## Decision

Use the existing backend host at `65.75.201.18` only as a restricted
forced-command SSH probe. Both app readiness jobs stay on GitHub-hosted
`ubuntu-latest`. The backend must never run a GitHub self-hosted runner for
this path.

The probe proves only protected credential readiness and direct Supabase
PostgreSQL connectivity over the existing backend IPv6 path. It does not approve failover. Lag, parity, schema, sequence, writer-pause, and split-brain gates remain separate.

## Boundary

The dedicated identity is `nutsnews-standby-probe`.

- Password locked.
- No sudo, admin, Docker, or production runtime groups.
- No interactive shell.
- No PTY.
- No agent forwarding.
- No TCP forwarding.
- No X11 forwarding.
- No user-controlled environment.
- No production-file access.
- `authorized_keys` uses `command=/usr/local/libexec/nutsnews-standby-supabase-probe` plus `restrict`.
- `sshd_config` also uses a `Match User nutsnews-standby-probe` boundary as
  defense in depth.

The forced command must reject any non-empty `SSH_ORIGINAL_COMMAND`.

## Fixed Probe Contract

The root-owned probe program lives at:

```text
/usr/local/libexec/nutsnews-standby-supabase-probe
```

It accepts exactly one bounded PostgreSQL URL through stdin. It rejects empty,
multiline, or oversized input. It validates:

- protocol is `postgres` or `postgresql`;
- host is the exact protected expected `db.<project-ref>.supabase.co`;
- port is exactly `5432`;
- database is exactly `postgres`;
- credentials are present;
- `sslmode=require` is present;
- the expected Supabase project ref matches the protected backend input.

GitHub never sends SQL, executable names, hosts, command-line arguments, or
shell commands. The backend owns the fixed read-only query.

The probe invokes `psql` without placing the database URI or password in argv.
It uses connection and statement timeouts, serializes execution with `flock`,
imposes a hard timeout, captures and discards raw `psql` output, and returns
only:

```text
READY
```

Failures return only a generic failure. The probe must never log credentials,
the URL, Supabase host/project metadata, PostgreSQL errors, or row data. It
must not persist the database URL on the backend.

## Repository-Managed Provisioning

All backend host mutations for this boundary are owned by Ansible and must run
through the protected `production-backend` environment. Do not configure the
probe account manually over SSH.

Source-controlled assets:

- protected workflow: `.github/workflows/supabase-standby-probe.yml`
- playbook: `ansible/playbooks/supabase_standby_probe.yml`
- role: `ansible/roles/supabase_standby_probe`
- probe source copied by Ansible:
  `scripts/nutsnews_standby_supabase_probe.py`
- sshd defense-in-depth template:
  `ansible/roles/supabase_standby_probe/templates/standby-probe-sshd.conf.j2`
- expected Supabase target template:
  `ansible/roles/supabase_standby_probe/templates/probe.conf.j2`

Run check mode first and review the output before any apply. Rollback uses the
same role with `supabase_standby_probe_state=absent`; it removes the forced key,
fixed probe program, protected target config, sshd drop-in, dedicated user, and
dedicated group through Ansible.

The dedicated workflow is `Supabase Standby Probe Boundary`.

- Trigger: `workflow_dispatch` only.
- Required branch: `main`.
- Environment: protected `production-backend`.
- Permissions: `contents: read`.
- Inputs:
  - `run_mode`: `check` or `apply`; default `check`.
  - `probe_state`: `present` or `absent`; default `present`.
  - `confirm_apply`: must be exactly `backend.nutsnews.com` for apply mode.

Use `run_mode=check, probe_state=present` before installing the probe. Use
`run_mode=apply, probe_state=present` only after reviewing check mode and
approving the specific `production-backend` environment gate for that run.
Rollback is `run_mode=check, probe_state=absent`, followed by
`run_mode=apply, probe_state=absent` after review.

## Secret and Variable Contract

Backend `production-backend` protected inputs:

- `NUTSNEWS_STANDBY_PROBE_SSH_PUBLIC_KEY`
- `NUTSNEWS_PRODUCTION_SUPABASE_PROJECT_REF`
- `NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_PROJECT_REF` optional override
- `NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_HOST` optional exact-host override

The default expected target is the existing production Supabase project required
by `ramideltoro/nutsnews#496`. The workflow derives the exact direct host as
`db.<project-ref>.supabase.co` unless the optional exact-host override is
present and matches the same project ref. This keeps the forced command scoped
to one protected target without requiring the owner to retype a write-only
GitHub secret value.

The workflow also requires the existing protected backend SSH inputs:

- `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY`
- `NUTSNEWS_BACKEND_KNOWN_HOSTS`

Optional existing protected backend inputs:

- `NUTSNEWS_BACKEND_ANSIBLE_USER`
- `NUTSNEWS_BACKEND_BECOME_PASSWORD`

App `supabase-standby` protected secrets:

- `NUTSNEWS_STANDBY_SUPABASE_PROJECT_REF`
- `NUTSNEWS_STANDBY_SUPABASE_URL`
- `NUTSNEWS_STANDBY_SUPABASE_DB_URL`
- `NUTSNEWS_STANDBY_SUPABASE_SERVICE_ROLE_KEY`
- `NUTSNEWS_STANDBY_SUPABASE_ANON_KEY`
- `NUTSNEWS_STANDBY_PROBE_SSH_PRIVATE_KEY`
- `NUTSNEWS_STANDBY_PROBE_KNOWN_HOSTS`

App variables:

- `NUTSNEWS_STANDBY_PROBE_HOST`
- `NUTSNEWS_STANDBY_PROBE_USER`

Generate a new dedicated keypair for this probe. Never print either key. Store
only the public key in the backend protected input and only the private key in
the app protected environment secret. Store only an independently verified
backend host key in `NUTSNEWS_STANDBY_PROBE_KNOWN_HOSTS`; do not trust a fresh
`ssh-keyscan` result unless it matches the existing trusted backend host key.
Delete transient local key files immediately after confirming the GitHub
secrets and deployed public key are correct.

## GitHub Controls to Preserve

Keep the useful public-repository controls from #328:

- default workflow token permissions read-only;
- all external contributors require workflow approval;
- selected Actions enabled;
- SHA pinning required;
- protected `supabase-standby` environment with `main` branch policy.

Do not reintroduce a mandatory non-author PR approval rule unless the owner
explicitly requests it.

## Rollback

Rollback is repository-managed:

1. Remove the app probe SSH private key and known-hosts secrets.
2. Disable or revert the app readiness probe step.
3. Run the backend protected Ansible workflow in rollback/remove mode once
   implemented.
4. Remove the probe user, authorized key, sshd `Match User` drop-in, and probe
   program through Ansible.
5. Confirm the app repository still has zero self-hosted GitHub runners.

Safe evidence is limited to workflow URLs, GitHub-hosted runner labels, the
`READY` token, boolean direct-connectivity success, boolean negative SSH
boundary results, and runner count. Forbidden evidence includes database URLs,
Supabase project refs, Supabase hostnames, database users/passwords, Supabase
API keys, SSH private keys, PostgreSQL errors, and row data.
