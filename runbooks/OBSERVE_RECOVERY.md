# Observe recovery integration

Observe's local server performs sequential disposable restores under a shared lock,
2 CPU quota, 8 GiB RAM limit, and a network namespace with only loopback. PostgreSQL
production databases, queues, Docker sockets and application credentials are never
available to the restored processes. Recovery changes do not enable shadow workers.

The protected `Observe recovery configuration` workflow validates a Caddy candidate
and fixed export helper before applying only this integration. It preserves existing
virtual hosts and reloads Caddy. It does not restart PostgreSQL, Docker or the API.
Do not substitute the full baseline for this targeted rollout on a host with adopted
application configuration. The normal baseline must first reconcile that configuration.

The `observe-recovery-export` system account has a forced-command key and a sudo rule
for one root-owned allowlisted export helper. It can read only the fixed backend
application/configuration/dependencies and a consistent logical database backup, or
the deployed Fantasy application/configuration and its newest saved database backup.
It cannot choose SQL, paths, hosts, writes, restore targets or shell commands.
Exports travel over pinned SSH to the local server, are encrypted and copied to the
existing encrypted cloud destination, then downloaded before isolated restoration.
The client private key stays root-only on the local server; only its public key is
versioned here. Revocation removes this account's authorized key and sudo rule.

A service drop-in runs the existing snapshot verifier immediately after successful
backup. The independent verification timer and all retention policies remain intact.
On 2026-09-20, snapshot `9b3b0868` passed verification: the prior failure was a delay
between the 03:17 backup and 04:17 verification, rather than identified corruption.
A verification failure remains a failure and does not establish a restore result.

Fantasy's existing Caddy virtual host gains sanitized access logging to the existing
Alloy-readable access file. Client addresses, headers, URI and response headers are
removed. Observe queries the exact Fantasy host within that stream. Empty error
queries do not establish coverage; a fresh source event is required.

Rollback: remove `observe-verification.conf` from the backup service drop-in directory
and reload systemd; restore the saved Caddy configuration, validate it, and reload
Caddy; revoke the export key/sudo rule. Preserve all backups and evidence. No password
changes are part of this procedure. Shared documentation belongs in
`ramideltoro/nutsnews-docs`, `OBSERVE_RECOVERY.md` and its audience/diagram companions.
