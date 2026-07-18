# Backend PostgreSQL Benchmark And Tuning

Issue: #112

## Purpose

Confirm the backend PostgreSQL host can handle NutsNews workload before it
becomes primary.

Offline benchmark contract:

```bash
python3 scripts/backend_postgres_benchmark_tuning.py --offline
```

Live benchmark against a restored rehearsal database:

```bash
NUTSNEWS_BACKEND_TARGET_DB_URL=postgresql://... \
  python3 scripts/backend_postgres_benchmark_tuning.py --output benchmark.json
```

Protected workflow:

```bash
gh workflow run backend-postgres-benchmark-tuning.yml \
  --repo ramideltoro/nutsnews-backend \
  --ref db-primary-migration-benchmark-tuning \
  -f mode=benchmark-staging
```

## Managed Settings

The Ansible PostgreSQL baseline manages:

- `log_min_duration_statement = 500`
- `log_autovacuum_min_duration = 10s`
- `track_io_timing = on`
- `autovacuum = on`

## Evidence Required Before Cutover

- database size and public relation/index sizes;
- connection count;
- critical smoke-test query timings;
- backup duration from restore proof;
- slow-query and autovacuum logging enabled;
- capacity risks and upgrade triggers.

Live benchmark results are blocked until the restored backend PostgreSQL
rehearsal database exists.
