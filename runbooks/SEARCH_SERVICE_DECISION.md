# Search Service Decision

This runbook covers backend issue #29 for `65.75.201.18`.

## Acceptance Criteria

- A decision record recommends a search path with resource estimates and rollback/fallback guidance.
- If implementation is included, the service is private-only, resource-capped, observable, and GitOps-managed.
- Search index backup/rebuild strategy is explicit.
- App-side work is tracked separately in `ramideltoro/nutsnews` when needed.
- No production install, `Protected Ansible Apply`, restart, or traffic switch is run without separate explicit approval.

## Decision

Keep the existing Supabase/Postgres full-text search path. Do not install Meilisearch, Typesense, OpenSearch, or Elasticsearch on the backend host now.

The app already has:

- a generated `tsvector` search column;
- a GIN index;
- `public.search_articles`;
- `/api/search` with bounded query, page, and limit inputs;
- a `reader_archive_search` runtime feature flag;
- public 60-second search cache headers.

Public backend HTTP/HTTPS health routing does not change this decision; no
dedicated search daemon or public search TCP port is installed.

Machine-readable record:

```text
docs/backend-search-service-decision.json
```

Validator:

```bash
python3 scripts/validate_search_service_decision.py
```

The validator fails if the service baseline stops marking search service as not deployed while this decision still rejects a dedicated service.

## Product Need

| Search need | Current status | Decision |
| --- | --- | --- |
| Public article search | Implemented through Postgres full-text search. | Keep current path. |
| Admin article search | Admin pages query Supabase directly; no separate daemon need identified. | Keep current path. |
| Moderation/review search | No standalone workflow identified. | Do not install. |
| Typo tolerance, facets, semantic ranking | Not required by current app evidence. | Re-evaluate only with product need and measurements. |

## Option Comparison

| Option | Resource estimate | Backup or rebuild | Decision |
| --- | --- | --- | --- |
| Postgres full-text search | No additional always-on backend VPS service; uses existing database CPU, disk, and GIN index storage. | Rebuildable from article rows; covered by database backup/restore. | Recommended now. |
| Meilisearch | Adds an always-on daemon and separate index disk. Official docs say search speed depends on RAM-to-database-size ratio and indexing may use up to two thirds of available memory by default. | Prefer rebuild from Postgres rows; snapshot only if rebuild time becomes unacceptable. | Defer. |
| Typesense | Adds an always-on daemon and separate index disk. Official guidance sizes memory from dataset size, and semantic/hybrid search can require multiple GB of extra RAM. | Prefer rebuild from Postgres rows; snapshot only if rebuild time becomes unacceptable. | Defer. |
| OpenSearch/Elasticsearch | Too heavy for the current small backend host without a separate capacity plan. | Not applicable in this phase. | Reject for current VPS. |

## Future Install Gate

Add a dedicated search service only after measured evidence shows the Postgres path is insufficient.

Accepted triggers:

- sustained search latency above the product threshold after database tuning;
- search-specific database CPU or IO pressure;
- product requirements for typo tolerance, facets, synonyms, or semantic ranking;
- admin workflows that cannot be handled safely by Supabase/Postgres queries.

Future requirements:

- Bind search only to localhost or a private backend app network.
- Do not expose a public search TCP port.
- Document memory, CPU, disk, index size, and rebuild time before protected apply.
- Keep indexes rebuildable from source database rows unless snapshot backup is explicitly justified.
- Define health checks and app degraded-mode behavior before the app depends on the service.
- Add low-cardinality observability for service health, index age, document count, query errors, latency, disk, and memory.
- Open a concrete `ramideltoro/nutsnews` issue for app integration before wiring traffic to the service.

## External References

- PostgreSQL text-search index docs: https://www.postgresql.org/docs/current/textsearch-indexes.html
- Meilisearch hardware FAQ: https://meilisearch.com/docs/resources/help/faq
- Meilisearch memory configuration: https://meilisearch.com/docs/resources/self_hosting/configuration/reference
- Typesense system requirements: https://typesense.org/docs/guide/system-requirements.html
- Typesense memory sizing guidance: https://cloud-help-center.typesense.org/article/22-choosing-how-much-memory-you-need

## Validation

Current non-mutating checks:

```bash
python3 scripts/validate_search_service_decision.py
python3 scripts/validate_service_baseline.py
```

Future implementation validation must include Compose or Ansible config checks and a read-only verification that no search service TCP port is public after approved apply.

## Rollback

No runtime rollback is required for this decision because no service is installed.

If a search service is added later, the preferred rollback is:

1. route app search back to Postgres full-text search;
2. disable search-service traffic with the app feature flag or config switch;
3. revert the backend service PR;
4. run protected apply;
5. verify search and public health routes read-only.
