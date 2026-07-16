# Backend Cloudflare Routing

This runbook covers backend issue #11 for `backend.nutsnews.com`.

## Routing Model

The backend uses a Cloudflare-managed DNS-only `A` record:

| Name | Type | Target | Proxied |
| --- | --- | --- | --- |
| `backend.nutsnews.com` | `A` | `65.75.201.18` | `false` |

DNS-only is intentional for this phase. It keeps SSH separate from Cloudflare
HTTP proxying, lets Caddy manage a public origin certificate, and avoids
claiming Cloudflare Full Strict edge proxying before the backend application and
edge policy are reviewed.

## Origin Readiness

The backend baseline installs Caddy and serves:

```text
/healthz -> ok
```

All other paths return `backend application not deployed` with `404` until a
reviewed backend app deployment owns those routes.

Before DNS apply, the protected workflow verifies direct-origin health with:

```bash
curl --resolve backend.nutsnews.com:80:65.75.201.18 http://backend.nutsnews.com/healthz
```

## Protected Workflow

Workflow: `.github/workflows/backend-cloudflare-routing.yml`

The workflow is manual-only and uses the `production-backend` GitHub
Environment. It requires the Environment secrets:

| Secret | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Scoped token with DNS edit access to the `nutsnews.com` zone |
| `CLOUDFLARE_ZONE_ID` | Cloudflare zone id for `nutsnews.com` |

Run modes:

| Mode | Effect |
| --- | --- |
| `check` | Reads Cloudflare and prints the planned create/update/noop/delete action without mutation |
| `apply` | Requires `confirm_apply=backend.nutsnews.com`, verifies origin health, then creates or updates the DNS record |
| `rollback` | Requires `confirm_apply=backend.nutsnews.com`, deletes the managed `A` record if present |

## Apply Order

1. Merge the reviewed routing PR.
2. Run `Protected Backend Ansible Apply` in `check` mode.
3. Approve and run `Protected Backend Ansible Apply` in `apply` mode.
4. Verify direct-origin health with `curl --resolve`.
5. Run `Backend Cloudflare Routing` in `check` mode.
6. Approve and run `Backend Cloudflare Routing` in `apply` mode.
7. Verify DNS, TLS, and health from outside the host.

## Verification

DNS:

```bash
dig +short backend.nutsnews.com A
```

Health:

```bash
curl -fsS https://backend.nutsnews.com/healthz
```

TLS:

```bash
curl -Iv https://backend.nutsnews.com/healthz
```

Firewall:

```bash
ssh -i ~/.ssh/servercheap_65_75_201_18 rami@65.75.201.18 'ss -tulpen 2>/dev/null || ss -tulpn'
```

Expected public listeners after routing are SSH, HTTP, and HTTPS only.

## Rollback

Preferred rollback:

1. Run `Backend Cloudflare Routing` with `run_mode=rollback` and
   `confirm_apply=backend.nutsnews.com`.
2. Verify `backend.nutsnews.com` no longer resolves to `65.75.201.18`.
3. If the origin listener must also be removed, revert the routing PR and run
   the protected backend Ansible check/apply path.

Break-glass manual Cloudflare dashboard changes must be documented afterward and
reconciled back into this workflow.
