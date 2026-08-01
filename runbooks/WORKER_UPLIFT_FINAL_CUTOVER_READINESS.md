# Worker-uplift final cutover readiness

Issue `nutsnews-worker#166` is the non-mutating gate for the exact protected
execution in `nutsnews-worker#127`. It does not cut over, enable uplift
production writes, disable legacy ingestion, or change DNS/failover.

## Standing bounded authorization

The owner authorization is recorded at
https://github.com/ramideltoro/nutsnews-worker/issues/166#issuecomment-5151195619
and pinned by body SHA-256 and scope SHA-256 in
`docs/worker-uplift-final-cutover-authorization.json`. No recurring owner prompt
is required for a release, first execution, or routine protected-environment
wait when the exact source-controlled scope and every invariant validate.

This is not a risk waiver. The validator must fail closed if the candidate,
scope, safety state, workflow, typed confirmation, evidence set, watermark,
rollback limits, observation window, or exclusions change. A scope change needs
a reviewed source update and a new owner authorization; an unchanged exact
candidate revision does not.

## Evidence classes

- Read-only: current runtime status, queues/DLQs, parity, complete-window soak,
  security dispositions, dependency recovery, backup/restore, authenticated
  admin, observability, and Cloudflare failover analytics proof. Inspect the
  downloaded JSON and checksum, not only the workflow conclusion.
- Value-free dry run: build the exact candidate and watermark inputs and run
  the fixed `dry-run` control operation. It cannot reach production targets.
- Isolated rehearsal: run `rehearse` for the exact inputs, including all fixed
  injected failure points. It cannot reach production targets or mutate state.
- Protected read-only: run `preflight` and `verify` through the
  `production-backend` environment. Exact waits within the standing scope may
  be approved through the GitHub API.
- Protected mutation: only #127 may run `apply` or `rollback`, from `main`,
  with the exact candidate, watermark, absolute deadline, and typed
  confirmation frozen by #166.

## Validation and decision

Run:

```bash
python3 scripts/validate_worker_uplift_final_cutover_readiness.py
python3 -m unittest tests.test_worker_uplift_final_cutover_readiness
```

The committed decision may remain `NO-GO` while evidence is incomplete. Before
#127, `--require-go` must pass. GO is valid only when the exact eight-service
candidate is immutable, every evidence category passes, blockers are empty,
the legacy worker is still the sole production ingestion owner, uplift remains
shadow-only with `production_writes_enabled=false`, and the fixed execution,
rollback, observation, threshold, and ownership records are complete.

## Frozen #166 GO inputs

The current source-controlled GO authorizes only the fixed protected #127
workflow during `2026-08-01T19:00:00Z` through `2026-08-01T21:00:00Z`:

- candidate manifest SHA-256:
  `71b0303705093ad398458083547a86e9e61f50458e8799ace38de4f2404859df`;
- exact eight-stage watermark artifact SHA-256:
  `e9b0ff2b129b76ec54589f32ade782b90aadaff54124344c2541e429d4d5d022`;
- absolute rollback deadline: `2026-08-03T21:00:00Z`;
- observation window: `2026-08-01T21:00:00Z` through
  `2026-08-03T21:00:00Z`;
- 17-threshold canonical SHA-256:
  `6823d92447f75a452c5ea7f65e50cf83ee4b303d0c0b6ab43cd2697a78931cb9`;
- primary operator and evidence custodian: `ramideltoro`; no independent human
  backup is claimed, so owner unavailability requires aborting into the last
  validated single-writer safe state.

The decision was made while `legacy_shards` remained the active owner, legacy
dispatch remained enabled, all eight uplift services remained in `shadow`, and
`production_writes_enabled=false`. Current protected preflight
`30709164722` and verify `30709204722` prove that state without mutation. The
full isolated rollback rehearsal `30709065035` and injected failure runs
`30709095571`, `30709108042`, `30709127075`, and `30709141781` prove all fixed
failure branches are single-writer safe, cannot reach production targets, and
finish far inside the 900-second recovery target.

Do not substitute another image, source revision, watermark, deadline, window,
threshold set, workflow, typed confirmation, or owner plan. Any substitution
invalidates GO. The execution operator must inspect the immutable #127 apply
artifact before beginning the observation clock.

## Drift and recovery

Any missing or stale evidence, source hash mismatch, candidate mismatch,
unexpected owner/write state, time-window mismatch, or expanded authority is
an automatic `NO-GO`. Keep legacy dispatch enabled and uplift production writes
false. Do not change DNS, Cloudflare, failover behavior, queues, schemas, or
legacy-worker code to clear the gate.

After #127, preserve the immutable apply artifact and execute the 48-hour
observation plan. Rollback is allowed only through the fixed protected workflow
before the frozen absolute deadline. If the deadline passes, use the documented
forward-recovery path; do not improvise a rollback.
