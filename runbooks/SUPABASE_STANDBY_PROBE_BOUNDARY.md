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

## Secret and Variable Contract

Backend `production-backend` protected inputs:

- `NUTSNEWS_STANDBY_PROBE_SSH_PUBLIC_KEY`
- `NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_PROJECT_REF`
- `NUTSNEWS_STANDBY_PROBE_EXPECTED_SUPABASE_HOST`

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
