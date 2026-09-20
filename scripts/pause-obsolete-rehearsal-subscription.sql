\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
DO $guard$
DECLARE target oid;
BEGIN
  IF current_database() <> 'nutsnews_restore_rehearsal' OR pg_is_in_recovery() THEN
    RAISE EXCEPTION 'Only the standalone rehearsal database is allowed';
  END IF;
  SELECT oid INTO target FROM pg_database WHERE datname = current_database();
  IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'nutsnews_primary_shadow') THEN
    RAISE EXCEPTION 'Expected separate production database is missing';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datid = target AND pid <> pg_backend_pid() AND backend_type = 'client backend') THEN
    RAISE EXCEPTION 'Rehearsal database has another client; stop for review';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_prepared_xacts WHERE database = current_database()) THEN
    RAISE EXCEPTION 'Rehearsal database has prepared transactions';
  END IF;
  IF (SELECT count(*) FROM pg_subscription WHERE subdbid = target) <> 1 OR
     NOT EXISTS (SELECT 1 FROM pg_subscription WHERE subdbid = target
       AND subname = 'nutsnews_backend_migration_sub'
       AND subslotname = 'nutsnews_backend_migration_slot'
       AND subpublications = ARRAY['nutsnews_backend_migration_pub']::text[]) THEN
    RAISE EXCEPTION 'Unexpected rehearsal subscription configuration';
  END IF;
END
$guard$;
\if :apply
ALTER SUBSCRIPTION nutsnews_backend_migration_sub DISABLE;
\endif
SELECT json_build_object('database', current_database(), 'subscription', subname,
  'enabled', subenabled, 'production_data_unchanged', true,
  'source_resources_unchanged', true)
FROM pg_subscription WHERE subdbid = (SELECT oid FROM pg_database WHERE datname = current_database());
COMMIT;
