# Worker Uplift Legacy Parity Baseline

Tracking issue: `ramideltoro/nutsnews-worker#68`
Capture timestamp: `2026-07-23T02:22:18Z`
Capture mode: read-only evidence collection. No Cloudflare Worker source file, secret, schedule, binding, route, DNS setting, deployment, or failover path was changed.

## Evidence Sources

- Live Cloudflare account queried with Wrangler while authenticated as `rami.deltoro@gmail.com`.
- Cloudflare docs retrieved before inspection:
  - `https://developers.cloudflare.com/secrets-store/integrations/workers/`
  - `https://developers.cloudflare.com/workers/configuration/secrets/`
  - `https://developers.cloudflare.com/workers/configuration/environment-variables/`
  - `https://developers.cloudflare.com/workers/wrangler/commands/secrets-store/`
  - `https://developers.cloudflare.com/workers/runtime-apis/bindings/`
- Legacy source evidence: `ramideltoro/nutsnews-worker@7770ee5b8f7fde24c1860186d3c869d28fe1525f`.
- Backend source evidence: `ramideltoro/nutsnews-backend@de90314a509d259dd4e0707b67ea9e6cede29929`.
- Live controller version evidence: `nutsnews-controller` version `b9cab8b3-3f5c-4cb1-b217-139571101a36`, version number `47`, created `2026-07-22T18:12:02.228232Z`, triggered by a secret change.
- Live shard evidence: `nutsnews-worker-0` through `nutsnews-worker-24`, all with the same deployed binding shape, latest versions created from `2026-07-22T06:59:23.525858Z` through `2026-07-22T07:01:10.847708Z`.
- Detailed sanitized credential and binding map: `docs/worker-uplift-cloudflare-inventory.json`.

## Live Configuration Baseline

The deployed Cloudflare state, not the generated configs in the legacy repository, is the parity source of truth for replacement behavior.

| Area | Live evidence |
| --- | --- |
| Shard count | 25 deployed shard Workers, `nutsnews-worker-0` through `nutsnews-worker-24`. |
| Shard schedule | Repository config maps shards to even-minute hourly cron slots from minute `0` through `48`; controller cron is every five minutes and calls one automatic shard per run. |
| Controller schedule | `nutsnews-controller` has `*/5 * * * *`. |
| Feed distribution | Live shard binding shape has `FEEDS_PER_SHARD=20` and one `FEED_SHARD_INDEX` per shard. |
| Backend DB mode | Live shards expose `NUTSNEWS_DATABASE_PROVIDER_MODE=backend_postgres_primary` and `NUTSNEWS_BACKEND_POSTGRES_PRIMARY_CONFIRMATION=enable-backend-postgres-primary`. |
| Backend API | Live shards use `NUTSNEWS_BACKEND_API_URL=https://backend.nutsnews.com` as a non-secret variable and `NUTSNEWS_BACKEND_API_TOKEN` as a Secrets Store binding. |
| AI provider | Live shards expose `AI_PROVIDER=local`, `LOCAL_AI_MODEL=qwen2.5:3b`, `LOCAL_AI_URL=https://ai.nutsnews.com`, `LOCAL_AI_TIMEOUT_MS=15000`, and `LOCAL_AI_API_KEY` as a Secrets Store binding. |
| AI fallback | Live shards expose `AI_PROVIDER_FALLBACK_TO_OPENAI=true`, `OPENAI_API_KEY` as a Secrets Store binding, and `OPENAI_TIMEOUT_MS=30000`. |
| AI limits | Live shards expose `AI_REVIEW_CONCURRENCY=1`; source hard cap is 18 AI reviews per run. Controller live version exposes `MAX_AI_REVIEWS_PER_SHARD=3`. |
| Fetch limits | Live shards expose `RSS_FEED_FETCH_TIMEOUT_MS=15000`, `ARTICLE_PAGE_FETCH_TIMEOUT_MS=10000`, and `FEEDS_PER_SHARD=20`. Source caps each feed to 35 items and each run to 300 candidates. |
| Translation | Live shards expose `ENABLED_SUMMARY_LANGUAGES=fr,ja,de-CH,de,el`, `SUMMARY_TRANSLATION_LIMIT=5`, and `HOLD_ARTICLES_FOR_TRANSLATIONS=true`. Controller has `TRANSLATION_BACKLOG_ENABLED=true`. |
| Publication gate | Live shards expose `NUTSNEWS_PRODUCTION_WRITES_PAUSED=false`; source holds accepted articles as `translation_pending` when translation hold is enabled, then publishes after all configured summary languages exist. |
| KV fallback | Live shards bind `NUTSNEWS_KV`; source uses KV for recent processed URL cache, run state, last successful run state, and public-feed edge snapshot fallback/status endpoints. |
| Redis locks | Upstash Redis code exists in source for worker locks, AI review locks, manual rate limits, and counters, but no Upstash bindings were present on all 25 deployed shard versions. |
| Observability | Live shards bind Better Stack host/token secrets for legacy log delivery. Uplift observability ownership is not Better Stack by default; Grafana Cloud resources belong to `ramideltoro/nutsnews-infra`. |
| Controller/failover | Controller owns shard orchestration and DNS failover state. It also binds Durable Object `FAILOVER_CONTROLLER_STATE` and classic secret `NUTSNEWS_FAILOVER_STATUS_HMAC_SECRET`. |

## Source vs Live Disagreements

These are blockers for any implementation that tries to infer defaults from repository files alone.

| Disagreement | Evidence | Required handling |
| --- | --- | --- |
| Supabase bindings | `worker/generated-wrangler/*.jsonc` includes `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, but all 25 live shard versions captured here do not expose those bindings. | Treat backend PostgreSQL primary as production. Do not automatically carry Supabase service credentials into uplift containers. |
| OpenAI fallback | Generated shard configs show `AI_PROVIDER_FALLBACK_TO_OPENAI=false`; all 25 live shard versions expose `AI_PROVIDER_FALLBACK_TO_OPENAI=true`. | Preserve as a legacy parity/fallback fact. Do not add OpenAI fallback to uplift services without a later explicit decision. |
| Controller AI review limit | Controller repo config defaults `MAX_AI_REVIEWS_PER_SHARD=12`; live controller version exposes `MAX_AI_REVIEWS_PER_SHARD=3`. | Use live value for parity load tests and shadow comparisons until reconciled. |
| Backend API URL storage | Secrets Store contains `NUTSNEWS_BACKEND_API_URL`, but deployed shards expose it as plain text. | Treat it as a required non-secret variable for uplift runtime config. |
| Upstash | Source has optional Upstash lock/backpressure paths and Secrets Store contains Upstash names; deployed shards do not bind them. | Do not assume Upstash is a live production lock owner. RabbitMQ/runtime issues must make a new lock/backpressure decision. |

## Responsibility And Owner Matrix

| Production responsibility | Legacy owner today | Uplift owner or retained owner | Parity decision |
| --- | --- | --- | --- |
| Feed scheduling/sharding | `nutsnews-controller` plus 25 shard Workers | `nutsnews-worker-feed-scheduler` for ingestion scheduling; controller keeps failover/orchestration only until retired | Preserve 25-shard cadence as baseline evidence; RabbitMQ scheduler may intentionally replace shard mechanics if parity metrics pass. |
| RSS/Atom fetch and parse | Shard Workers | `nutsnews-worker-feed-fetcher` | Preserve conditional fetch behavior, timeout behavior, malformed feed handling, parser error reporting, item caps, and feed health metrics. |
| URL dedupe and already-reviewed lookup | Shard Workers with backend/Supabase lookup and KV recent processed URL cache | `nutsnews-worker-article-canonicalizer` plus runtime storage decision | Preserve duplicate suppression by original URL and reviewed/published lookbacks; KV is legacy fallback, not automatic uplift storage. |
| Image hydration and no-thumbnail rejection | Shard Workers | `nutsnews-worker-article-enrichment` and `nutsnews-worker-article-approval` split | Preserve article-page image lookup cap, no-thumbnail rejection reason, and retry-after policy. |
| Local prefilter | Shard Workers | `nutsnews-worker-article-approval` | Preserve hard-negative rejection, source-specific strict prefilter behavior, and positive escape handling. |
| Qwen/local AI review | Shard Workers | `nutsnews-worker-article-approval` | Preserve local provider first, bounded concurrency, timeout/retry, structured decision validation, and traceable provider/model metadata. |
| OpenAI review fallback | Shard Workers | Retained only if explicitly approved by a later AI fallback issue | Live fallback is enabled; uplift must not silently inherit it. |
| Summary translation | Shard Workers plus `/translate-backlog` mode | `nutsnews-worker-article-translation` | Preserve configured languages, recovery-first backlog drain, per-run translation task cap, quality validation, and failed sample reporting. |
| Persistence | Shard Workers through backend Worker DB API / legacy REST paths | `nutsnews-worker-article-persistence` | Preserve idempotent article/review/summary/feed health/run metric saves and backend PostgreSQL primary behavior. |
| Publication gate | Shard Workers | `nutsnews-worker-article-publication` after readiness | Preserve `translation_pending` hold when translations are required and publish only after all required summary languages exist. |
| Public feed snapshot | Shard Workers and KV | Retained backend/worker publication owner to be decided; KV remains legacy fallback | Preserve snapshot refresh RPC/API behavior, KV edge snapshot payload, status endpoint, pagination, category filter, cache headers, miss/error statuses. |
| Worker run metrics | Shard Workers | Runtime/persistence/observability split | Preserve success/failure run rows, AI usage counters, provider split, token/cost estimates, Redis/KV status flags where applicable. |
| Locks/backpressure | Optional Upstash code, not live-bound in captured deployed shards | Runtime issue must decide | Do not model Upstash as live production dependency; preserve manual rate-limit and lock cases as parity fixtures only if intentionally retained. |
| Controller ingestion orchestration | Controller automatic/manual shard calls | Scheduler owns future ingestion; controller remains failover/controller owner until separate retirement | Separate ingestion orchestration from DNS failover state and actions. |
| DNS failover controller | `nutsnews-controller` Durable Object, routes/status/actions endpoints, live-origin checks | Retained Cloudflare/controller owner until separate failover migration | Not part of RabbitMQ ingestion uplift. Preserve status/action/failover parity as non-ingestion retained behavior. |
| Observability | Better Stack legacy logs; backend/infrastructure docs for Grafana | `ramideltoro/nutsnews-infra` owns Grafana Cloud resources; service repos emit logs/metrics | Do not carry Better Stack into uplift by default. |

## Parity Matrix

| Case | Legacy behavior to preserve or decide | Required fixture/evidence |
| --- | --- | --- |
| Successful feed refresh | Controller selects shard, shard fetches configured feeds, parses candidates, dedupes already-reviewed URLs, reviews eligible articles, saves reviews/articles/summaries/feed health/run metrics, refreshes public feed snapshot, updates KV run state. | Sanitized feed fixture with valid RSS/Atom items, image URLs, local AI approved response, backend API success, expected counters. |
| Duplicate article candidate | Previously reviewed/published/or KV-cached URL is skipped before AI review. | Fixture with repeated URL/GUID and existing reviewed row; assert no duplicate review or article save. |
| Partial/held translation | Accepted article remains `translation_pending` while required language summaries are missing; publication waits for full configured language set. | Fixture with one missing language; assert no publish until all `fr`, `ja`, `de-CH`, `de`, `el` summaries exist. |
| Translation backlog drain | `/translate-backlog` and scheduled controller backlog mode recover older missing translations before new article tasks. | Fixture with older published/translation_pending article missing summaries; assert recovery-first ordering and task cap. |
| Malformed feed | Feed fetch records failure status/error, contributes to feed health counters, does not block unrelated feeds. | Malformed XML fixture and parser error fixture. |
| Fetch timeout | RSS/article-page timeout is bounded by live config and recorded as feed or image hydration failure. | Timeout fixture with `RSS_FEED_FETCH_TIMEOUT_MS=15000` and `ARTICLE_PAGE_FETCH_TIMEOUT_MS=10000`. |
| AI retry/fallback | Local AI timeout/invalid response is retried; live legacy can fall back to OpenAI when configured. | Local timeout fixture; OpenAI fallback fixture marked legacy-only unless future uplift issue approves fallback. |
| Backend API failure | Review/article/summary/feed health/run saves fail safely, record status/error samples, and do not publish partial data as success. | Backend 500/timeout fixtures for every write path. |
| Snapshot success | Snapshot refresh and KV publish produce status headers and paginated public-feed response. | Sanitized snapshot rows and expected KV payload. |
| Snapshot miss/unbound | `/public-feed-snapshot/status` distinguishes unbound, miss, empty, and hit states. | KV unbound/miss fixtures. |
| Lock skipped | When worker lock is active, manual/scheduled refresh is skipped and returns/logs lock state. | Redis lock fixture only if Upstash is intentionally retained; otherwise mark retired. |
| Manual rate limit | Manual refresh can return 429 when Upstash rate limit is enabled. | Legacy fixture only; not uplift default because Upstash is not live-bound. |
| Failover controller status | Controller status/action endpoints, HMAC signing, Durable Object state, DNS readback, live-origin readiness, and alert rate limit remain separate from ingestion. | Failover controller fixture set; retained owner must be explicit before legacy controller retirement. |

## Sanitized Fixture Requirements

- No fixture may contain production URLs that embed tokens, secret fragments, credentials, private API keys, or generated passwords.
- Feed fixtures must use synthetic hostnames or public sample feeds.
- AI fixtures must store model/provider metadata and structured JSON decisions without real prompts that contain private incident data.
- Backend API fixtures must use fake bearer tokens and fake article IDs.
- Snapshot fixtures must preserve shape and pagination behavior, not production article content.
- Failover fixtures must use synthetic DNS/readiness responses and fake HMAC material.

## Approved Retirements And Blockers

No production behavior is approved for retirement by this baseline alone. The following items require an explicit later decision:

- Whether OpenAI fallback remains available in any uplift service.
- Whether Upstash Redis remains part of locks/backpressure/rate-limit behavior.
- Whether Better Stack remains after Grafana Cloud observability is fully owned by `ramideltoro/nutsnews-infra`.
- Whether Cloudflare KV remains a publication/snapshot fallback after backend publication is introduced.
- When controller ingestion orchestration can be retired separately from DNS failover controller duties.

## Guardrails

- The legacy Cloudflare Worker path remains production owner until the shadow pipeline proves parity and cutover is approved.
- Controller failover duties are not RabbitMQ ingestion duties.
- Backend PostgreSQL is the current production primary for worker persistence evidence.
- Repository defaults and stale generated configs are not sufficient evidence when live deployed config disagrees.
