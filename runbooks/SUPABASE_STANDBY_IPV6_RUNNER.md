# Supabase Standby IPv6 One-Job Runner Runbook

This runbook covers backend issues #323-#331 for the isolated public-repository runner used by `ramideltoro/nutsnews` issue #496.

The runner exists for one purpose: run the protected `Supabase Standby Credential Readiness` job against the direct Supabase Postgres endpoint from a VM with real outbound IPv6. It is not a general CI runner.

## Security Boundary

Do not run a GitHub Actions runner, runner container, or nested runner VM on `65.75.201.18`. That host owns production backend state, production SSH trust, backend credentials, persistent service disks, Docker networks, and sensitive service reachability. Public-repository workflow code must never execute there.

The approved boundary is a separate disposable Ubuntu 24.04 LTS VM with IPv6 enabled at creation. The selected low-cost profile is DigitalOcean `nyc3`, Basic shared CPU, `x64`, 1 vCPU, 1 GiB RAM, 25 GiB SSD. DigitalOcean is selected only as the provider profile; no provider token, state file, SSH private key, or concrete VM address is committed here.

Supabase direct Postgres connections require IPv6 unless the project has the IPv4 add-on. This runner exists because GitHub-hosted Actions does not provide the required direct IPv6 path.

## Source-Controlled Assets

- Boundary record: `docs/supabase-standby-ipv6-runner-boundary.json`
- Ansible playbook: `ansible/playbooks/supabase_standby_ipv6_runner.yml`
- Example inventory: `ansible/inventories/supabase_standby_ipv6_runner/hosts.example.yml`
- Role: `ansible/roles/supabase_standby_ipv6_runner`
- Protected backend workflow: `.github/workflows/supabase-standby-ipv6-runner.yml`
- Shared docs target: `ramideltoro/nutsnews-docs/NUTSNEWS_SUPABASE_STANDBY_IPV6_RUNNER.md`

## Runner Contract

- Target repository: `https://github.com/ramideltoro/nutsnews`
- Scope: repository-level runner only
- Behavior: one job only with `--ephemeral`
- Labels: `--no-default-labels --labels supabase-standby-ipv6`
- Runner user: `github-runner`
- Sudo: no passwordless sudo, no sudo/admin supplementary group
- Runtime packages: `ca-certificates`, `curl`, `git`, `jq`, `netcat-openbsd`, `postgresql-client`
- Node.js 22: supplied by the reviewed `actions/setup-node` action into a runner-owned `RUNNER_TOOL_CACHE`; no `sudo` is needed at job runtime.
- Explicitly prohibited: Docker, compilers/build-essential, browsers, PostgreSQL/MySQL/MariaDB servers, production mounts, production SSH keys, cloud metadata credentials, and backend private networks.

Runner labels are routing selectors, not a security boundary. Authorization comes from `workflow_dispatch`, `main` ref guards, protected environments, least-privilege tokens, branch/ruleset controls, and VM destruction.

## Repository Controls Before Activation

Before registering a runner, verify `ramideltoro/nutsnews` has:

1. public repository self-hosted runner risk accepted only for this one-job path;
2. external-contributor workflow approval set to the strongest available option;
3. default `GITHUB_TOKEN` permissions set to read-only;
4. `main` protected by a reviewed branch protection or ruleset baseline;
5. `supabase-standby` environment requiring approval and allowing only `main`;
6. app regression passing so `supabase-standby-ipv6` appears only in `.github/workflows/supabase-standby-readiness.yml`;
7. readiness workflow merged to `main` with `preflight.runs-on: ubuntu-latest` and only `readiness.runs-on: [supabase-standby-ipv6]`.

As of 2026-07-24, `ramideltoro/nutsnews` also has repository-level selected-actions enforcement with GitHub-owned actions allowed, existing third-party actions/reusable workflows explicitly allow-listed, and full-SHA pinning required. If selected-actions blocks an existing reviewed workflow, temporarily restore `allowed_actions: all`, keep the source-controlled SHA-pinning guard active, add the missing exact allow pattern, and re-enable selected-actions.

## Lifecycle Order

1. Provision the disposable VM with IPv6 enabled. Do not reuse any production/backend host, SSH key, disk, Docker network, or credential.
2. Add the operator SSH key and record safe metadata only: timestamp, provider, region, image, architecture, and IPv6 capability.
3. Build a runtime inventory from `ansible/inventories/supabase_standby_ipv6_runner/hosts.example.yml`. Store the real inventory in the protected `supabase-standby-runner` GitHub Environment secret `NUTSNEWS_STANDBY_IPV6_RUNNER_HOSTS_YML`.
4. Run `.github/workflows/supabase-standby-ipv6-runner.yml` in `check` mode and review Ansible output.
5. Run the workflow in `apply` mode with confirmation `apply-supabase-standby-ipv6-runner-baseline`.
6. On the VM, run the redacted network probe as the runner account with `NUTSNEWS_STANDBY_SUPABASE_DB_URL` supplied only at runtime:

   ```bash
   sudo -u github-runner env NUTSNEWS_STANDBY_SUPABASE_DB_URL="$NUTSNEWS_STANDBY_SUPABASE_DB_URL" \
     /usr/local/bin/nutsnews-supabase-standby-ipv6-preflight
   ```

   Retain only pass/fail booleans. Do not print the URL, hostname, project ref, user, password, or resolved address.

7. Run the backend workflow in `register` mode with confirmation `register-one-supabase-standby-ipv6-runner`. The workflow obtains a short-lived registration token through `NUTSNEWS_APP_RUNNER_ADMIN_TOKEN`, masks it, and passes it to Ansible without committing or persisting it.
8. In `ramideltoro/nutsnews`, confirm exactly one online repository runner exists with only the `supabase-standby-ipv6` label. It must not advertise generic `self-hosted`, `linux`, `x64`, or `arm64` labels.
9. Dispatch `supabase-standby-readiness.yml` from the protected `main` commit with confirmation `verify-supabase-standby-readiness`.
10. Approve the `supabase-standby` environment.
11. Monitor the run. Confirm preflight used `ubuntu-latest` and readiness used `supabase-standby-ipv6`.
12. Review logs and summaries for leaked URLs, users, passwords, keys, project refs, row data, private SSH paths, provider details beyond safe metadata, or runner tokens.
13. Confirm the runner deregistered after the one job. If it did not, run backend workflow `remove` mode with confirmation `remove-supabase-standby-ipv6-runner` or delete the stale repository runner in GitHub settings.
14. Collect only redacted diagnostics required for the issue, then destroy/wipe the VM in the provider account.
15. Update backend issue #331 and app issue #496 with the safe run URL/result and explicitly state that lag <= 30 seconds, parity, schema, sequence, writer-pause, and split-brain gates remain separate before failover.

## Abort Conditions

Abort and destroy/wipe the VM if any of these occur:

- `curl -6 https://ifconfig.co/ip` fails;
- DNS cannot resolve an IPv6 address for the configured direct Supabase DB host;
- redacted `nc -6 -vz` to TCP/5432 fails after hardening;
- the runner advertises any generic label;
- the runner receives an unexpected job;
- the readiness workflow is not on the expected `main` commit;
- the environment approval is stale or unexpected;
- logs, summaries, or artifacts expose protected values;
- runner deregistration fails and forced removal cannot complete.

## Incident Response

For unexpected job assignment, secret exposure, runner persistence, or suspected VM compromise:

1. Cancel the workflow run.
2. Remove the runner from GitHub.
3. Destroy/wipe the VM.
4. Delete affected workflow logs if they contain protected values.
5. Rotate exposed Supabase, GitHub, provider, or SSH credentials.
6. File a private/security issue if sensitive values were exposed; otherwise comment on the implementation issue with redacted facts.
7. Revert `readiness.runs-on` to `ubuntu-latest` if the label path must be disabled.
8. Re-run the app regression that rejects unapproved `supabase-standby-ipv6` usage.

## Rollback

- Stop registration and do not create a replacement VM.
- Remove stale runners from `ramideltoro/nutsnews`.
- Destroy/wipe the VM and delete temporary SSH keys.
- Revert `ramideltoro/nutsnews/.github/workflows/supabase-standby-readiness.yml` so `readiness.runs-on` returns to `ubuntu-latest`.
- Keep `supabase-standby` environment secrets unless the owner separately approves removal or rotation.
- If the IPv6 VM model is abandoned, open a separate decision issue for the Supabase IPv4 add-on path.

## Safe Evidence Policy

Allowed evidence:

- run URL;
- timestamp;
- provider/region/image/architecture;
- boolean IPv6 HTTPS/DNS/TCP results;
- runner label/status/deregistration status;
- VM destruction confirmation;
- statement that no protected values were found in retained logs.

Forbidden evidence:

- database URL, host, project ref, database user, or password;
- Supabase service-role or anon key;
- GitHub registration/removal token;
- provider token;
- SSH private key;
- backend production credentials;
- table row data.
