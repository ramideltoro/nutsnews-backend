# Cloud-Init Provider Bootstrap Notes

This runbook covers backend issue #9 for `65.75.201.18`.

## Finding

Read-only verification shows:

```text
status: done
extended_status: degraded done
detail: DataSourceNoCloud [seed=/dev/sr0]
errors: []
```

Recoverable errors are deprecation warnings from the NoCloud seed:

```text
Deprecated cloud-config provided: chpasswd.list: Deprecated in version 22.2. Use users instead.
Config key 'lists' is deprecated in 22.3 and scheduled to be removed in 27.3. Use users instead.
The chpasswd multiline string is deprecated in 22.2 and scheduled to be removed in 27.2. Use string type instead.
```

These warnings were observed twice in `cloud-init status --long`.

## Interpretation

The backend repo does not currently own a cloud-init template. The degraded status appears to come from the provider NoCloud seed mounted from `/dev/sr0`, not from repo-managed bootstrap code.

No fatal cloud-init errors were reported, SSH access works, and the backend rebuild path is defined through Ansible plus the protected backend apply workflow rather than relying on provider cloud-init customization.

## Policy

- Treat the current cloud-init degraded status as provider image/bootstrap hygiene, not an app blocker.
- Do not add repo-owned cloud-init templates unless a future reviewed issue chooses that rebuild strategy.
- If this repo later adds cloud-init, it must use non-deprecated `users` and `chpasswd` syntax.
- Use `runbooks/BACKEND_BOOTSTRAP.md` and `runbooks/PROTECTED_BACKEND_APPLY.md` as the documented rebuild path for backend host state.

## Read-Only Verification

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'cloud-init status --long 2>&1 || true'
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'cloud-init status 2>&1 || true'
```

## When To Escalate

Escalate to provider support or choose a different image if future read-only checks show:

- fatal cloud-init `errors`;
- SSH/bootstrap failures caused by cloud-init;
- missing expected users or keys after rebuild;
- deprecated keys that are controlled by a repo-owned cloud-init template.

Until then, this item does not block backend app deployment. The pipeline still must apply and verify host desired state before production use.
