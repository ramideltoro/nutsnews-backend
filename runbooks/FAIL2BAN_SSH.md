# Fail2ban SSH Runbook

This runbook covers backend issue #4 for `65.75.201.18`.

## Acceptance Criteria

- `fail2ban-client status sshd` shows an active SSH jail.
- Repeated failed SSH attempts are detected and banned in a controlled test.
- The operator can still log in with `ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18`.
- A rollback or unban command is documented.
- The configuration is lightweight enough for this VPS.

## Repo-Managed Desired State

Ansible role: `ansible/roles/backend_baseline`

The fail2ban tasks:

- install `fail2ban`;
- write `/etc/fail2ban/jail.d/nutsnews-sshd.local`;
- enable the `sshd` jail with the systemd backend;
- use a conservative `maxretry` of 5 over 10 minutes and a 1 hour ban;
- keep localhost addresses ignored;
- enable and start the service;
- capture `fail2ban-client status sshd` in workflow logs.

## Apply Path

Run only through the protected backend workflow:

1. Merge the reviewed fail2ban PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Review the jail file and service changes.
4. Run `apply` mode after the `production-backend` approval gate.
5. Verify the jail status and operator login before considering #4 complete.

## Verification

Privileged status check:

```bash
sudo fail2ban-client status sshd
```

Operator login check:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'hostname && whoami'
```

Controlled ban test:

1. Use a disposable test source IP if possible.
2. Attempt repeated failed SSH logins that do not use the operator key.
3. Confirm `sudo fail2ban-client status sshd` lists the test source IP as banned.
4. Unban the test IP immediately.

## Unban And Rollback

Unban one IP:

```bash
sudo fail2ban-client set sshd unbanip <ip-address>
```

Stop fail2ban in break-glass recovery only:

```bash
sudo systemctl stop fail2ban
sudo systemctl disable fail2ban
```

Preferred rollback is a git revert followed by protected apply.

Record any break-glass command and reconcile the final state back into this repository.
