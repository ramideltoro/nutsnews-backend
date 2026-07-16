# SSH Hardening Runbook

This runbook covers backend issue #1 for `65.75.201.18`.

## Acceptance Criteria

- Effective SSH config reports:
  - `permitrootlogin no`
  - `passwordauthentication no`
  - `kbdinteractiveauthentication no`
  - `pubkeyauthentication yes`
- Password-only SSH probes no longer advertise password auth for `rami` or `root`.
- Key login with `ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18` still works.
- A privileged recovery path stays available until the new login test passes.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The role:

- comments managed auth directives in `/etc/ssh/sshd_config` so main-file defaults cannot override the drop-in;
- writes `/etc/ssh/sshd_config.d/00-nutsnews-hardening.conf`;
- validates the SSH daemon config with `sshd -t` before reload;
- reloads `ssh` only after validation passes.

The drop-in is intentionally named `00-nutsnews-hardening.conf` so cloud-init drop-ins such as `50-cloud-init.conf` cannot win by appearing earlier in include order.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed SSH hardening PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review the diff.
4. Keep provider console or an existing privileged session available.
5. Run `apply` mode after the `production-backend` approval gate.
6. Run the verification commands below before considering #1 complete.

## Verification

Privileged effective-config check:

```bash
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
```

Password-only probes:

```bash
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -o BatchMode=yes rami@65.75.201.18 true
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -o BatchMode=yes root@65.75.201.18 true
```

Expected result: the probes fail without `password` being offered in the authentication methods.

Key-login check:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
```

## Rollback

Preferred rollback is a git revert followed by protected apply.

Break-glass rollback, only if locked out or SSH is unhealthy:

```bash
sudo rm -f /etc/ssh/sshd_config.d/00-nutsnews-hardening.conf
sudo sshd -t
sudo systemctl reload ssh
```

Record any break-glass command and reconcile the final state back into this repository.
