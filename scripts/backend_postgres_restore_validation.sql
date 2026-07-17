\set ON_ERROR_STOP on

do $$
declare
  missing text[];
begin
  select array_agg(name order by name)
  into missing
  from (
    values
      ('articles'),
      ('rss_feeds'),
      ('article_ai_reviews'),
      ('article_summaries'),
      ('ai_usage_runs'),
      ('worker_runs'),
      ('feed_health')
  ) required(name)
  where not exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = required.name
  );

  if missing is not null then
    raise exception 'Missing restored tables: %', array_to_string(missing, ', ');
  end if;
end $$;

select 'articles' as object_name, count(*)::bigint as row_count from public.articles
union all
select 'rss_feeds', count(*)::bigint from public.rss_feeds
union all
select 'article_ai_reviews', count(*)::bigint from public.article_ai_reviews
union all
select 'article_summaries', count(*)::bigint from public.article_summaries
union all
select 'ai_usage_runs', count(*)::bigint from public.ai_usage_runs
union all
select 'worker_runs', count(*)::bigint from public.worker_runs
union all
select 'feed_health', count(*)::bigint from public.feed_health
order by object_name;

select count(*)::bigint as public_feed_snapshot_rows from public.public_feed_snapshot;
select count(*)::bigint as best_feed_rows from public.best_feeds;
select count(*)::bigint as bad_feed_rows from public.bad_feeds;
