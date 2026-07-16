# Redis And Valkey Decision

This runbook covers backend issue #27 for `65.75.201.18`.

## Acceptance Criteria

- The issue or PR identifies real workloads and rejects installation if there is no concrete use case.
- If installed, Redis/Valkey is reachable only from intended app containers/networks and never from the public internet.
- Resource limits, persistence mode, backup policy, health checks, and observability are documented.
- App behavior degrades safely if Redis/Valkey is unavailable.
- No `Protected Ansible Apply`, restart, or production mutation is run without separate explicit approval.

## Decision

Do not install Redis or Valkey now.

The current backend service baseline marks Redis/Valkey as not deployed, and the app evidence inspected on 2026-07-16 does not show a concrete Redis/Valkey workload or client dependency.

Machine-readable record:

```text
docs/backend-redis-valkey-decision.json
```

Validator:

```bash
python3 scripts/validate_redis_valkey_decision.py
```

The validator fails if the service baseline stops marking Redis/Valkey as not deployed while this decision still rejects installation.

## Workload Review

| Candidate workload | Current finding | Decision |
| --- | --- | --- |
| Queue or background retries | No backend-owned queue workload is deployed on `65.75.201.18`. | Do not install. |
| Cache | Current web caching uses Next, CDN headers, edge/Supabase fallback data, and public cache policies. | Do not install. |
| Rate limiter | Contact anti-abuse is currently route-level origin validation, Turnstile, and rate limiting. | Do not install. |
| Session-like state | Admin auth uses JWT sessions; no Redis session store was identified. | Do not install. |
| Feed job coordination | Feed automation is not currently owned by this backend host. | Do not install. |
| Admin task progress | No backend job-progress workload exists yet. | Do not install. |
| Durable retry buffer | No durable retry-buffer workload exists yet. | Do not install. |

## Future Install Gate

Before adding Redis or Valkey, a future PR must identify a concrete owner and workload in the app, worker, or backend repo.

Required design points:

- Bind only to localhost or a private Docker network.
- Never expose Redis or Valkey on a public TCP port.
- Require authentication or an equivalent private-network access boundary.
- Set `maxmemory`, CPU expectations, and an eviction policy.
- Use non-persistent mode for rebuildable cache and rate-limit data.
- Define a backup and restore policy before storing durable queue or retry data.
- Add health checks and app degraded-mode behavior before making Redis/Valkey required for readiness.
- Add low-cardinality observability for service health, memory, evictions, connected clients, and persistence errors.

## Validation

Current non-mutating checks:

```bash
python3 scripts/validate_redis_valkey_decision.py
python3 scripts/validate_service_baseline.py
```

Future implementation validation must include Compose or Ansible config checks and a read-only verification that no Redis/Valkey TCP port is public after approved apply.

## Rollback

No runtime rollback is required for this decision because no service is installed.

If Redis or Valkey is added later, the preferred rollback is a git revert followed by protected apply. Any break-glass stop or data removal must be documented and reconciled back into this repository.
