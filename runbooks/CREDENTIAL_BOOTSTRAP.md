# Backend Credential Bootstrap

This runbook sets up the credential management path for `production-backend`.

## What Is Already Repo-Managed

Credential inventory:

```text
docs/backend-credential-inventory.json
```

Inventory validator:

```bash
python3 scripts/validate_backend_credential_inventory.py
```

Secret readiness checker:

```bash
python3 scripts/check_backend_credential_readiness.py
```

Local bootstrap helper:

```bash
scripts/bootstrap_production_backend_environment.sh --dry-run
scripts/bootstrap_production_backend_environment.sh --apply
```

GitHub Actions readiness workflow:

```text
.github/workflows/backend-credential-readiness.yml
```

## GitHub Environment

Environment:

```text
production-backend
```

Required properties:

- reviewer approval required;
- deployments limited to `main`;
- admin bypass disabled;
- secrets and variables stored in the GitHub Environment, not in git.

The bootstrap helper creates or updates this Environment and the `main` deployment branch policy.
Self-review prevention is left disabled while `ramideltoro` is the only required reviewer, so that account can approve deployments it starts.

## Local Secret Input

Do not paste secrets into chat.

Put secret values on your local machine as either:

- environment variables with the exact secret names; or
- files under `.secrets/production-backend/<SECRET_NAME>`.

The `.secrets/` directory must stay local and untracked.
The backend repo ignores `.secrets/` so local provider values are not accidentally staged.

Dry-run first:

```bash
scripts/bootstrap_production_backend_environment.sh --dry-run
```

Apply when the local values are ready:

```bash
scripts/bootstrap_production_backend_environment.sh --apply
```

The script prints only names that were set or missing. It never prints values. `--apply` exits non-zero until all required values for the selected restic provider are available locally.

## Required Provider Access

Cloudflare:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_ZONE_ID`

Use a scoped API token limited to the `nutsnews.com` zone. The token needs DNS edit capability for backend DNS automation and zone read capability for validation.

Grafana Cloud:

- `GRAFANA_CLOUD_PROMETHEUS_URL`
- `GRAFANA_CLOUD_PROMETHEUS_USERNAME`
- `GRAFANA_CLOUD_PROMETHEUS_PASSWORD`
- `GRAFANA_CLOUD_LOKI_URL`
- `GRAFANA_CLOUD_LOKI_USERNAME`
- `GRAFANA_CLOUD_LOKI_PASSWORD`

Use access-policy tokens scoped to backend metrics/log ingestion. Grafana folders, dashboards, alert rules, contact points, quota guardrails, Synthetic Monitoring, and service-account credentials are managed from `ramideltoro/nutsnews-infra`, not this backend repository.

As of the worker-uplift Grafana handoff on 2026-07-23, `GRAFANA_URL` and `GRAFANA_SERVICE_ACCOUNT_TOKEN` are not backend credentials. They were removed from `production-backend` after the backend workflows stopped consuming them. If Grafana import verification fails, fix the infra OpenTofu import or datasource configuration and rerun the protected infra plan/apply; do not restore backend Grafana mutation credentials without a new reviewed ownership issue.

Supabase:

- `SUPABASE_ACCESS_TOKEN`
- `NUTSNEWS_PRODUCTION_SUPABASE_PROJECT_REF`
- `NUTSNEWS_PRODUCTION_SUPABASE_URL`
- `NUTSNEWS_PRODUCTION_SUPABASE_ANON_KEY`
- `NUTSNEWS_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY`
- `NUTSNEWS_PRODUCTION_SUPABASE_DB_URL`
- `NUTSNEWS_STAGING_SUPABASE_DB_URL`

Use a Supabase personal access token for CLI and Management API automation. Keep the service role key and database URL only in protected secrets.

Backend PostgreSQL migration roles:

- `NUTSNEWS_BACKEND_POSTGRES_APP_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_READONLY_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_RESTORE_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_VALIDATION_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_REPLICATION_PASSWORD`
- `NUTSNEWS_BACKEND_POSTGRES_MIGRATION_APP_REHEARSAL_PASSWORD`

These passwords are runtime-scoped or migration-only. Rotate or revoke restore,
validation, replication, and app rehearsal credentials after staging rehearsal,
production cutover, or rollback according to
[DB_MIGRATION_ACCESS.md](DB_MIGRATION_ACCESS.md).

Restic backups:

- `RESTIC_REPOSITORY`
- `RESTIC_PASSWORD`

Set `NUTSNEWS_BACKEND_RESTIC_PROVIDER` to `s3` or `b2`.

For S3-compatible storage:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- optional `AWS_DEFAULT_REGION`

For Backblaze B2:

- `B2_ACCOUNT_ID`
- `B2_ACCOUNT_KEY`

Email/reporting:

- `NUTSNEWS_REPORT_SMTP_HOST`
- `NUTSNEWS_REPORT_SMTP_USERNAME`
- `NUTSNEWS_REPORT_SMTP_PASSWORD`
- `NUTSNEWS_REPORT_EMAIL_FROM`
- `NUTSNEWS_REPORT_EMAIL_TO`

Worker-uplift Qwen gateway:

- `LOCAL_AI_API_KEY`

The source secret is retained in `production-backend`. Protected apply maps it
to the approval and translation service-specific runtime credential names; no
app repository stores the value.

Non-secret defaults are stored as GitHub Environment variables:

- `NUTSNEWS_BACKEND_HOST=65.75.201.18`
- `NUTSNEWS_BACKEND_DOMAIN=backend.nutsnews.com`
- `NUTSNEWS_BACKEND_ENVIRONMENT=production`
- `NUTSNEWS_BACKEND_RESTIC_PROVIDER=s3`
- `NUTSNEWS_REPORT_SMTP_PORT=587`
- `NUTSNEWS_REPORT_SMTP_STARTTLS=true`
- `LOCAL_AI_URL=https://ai.nutsnews.com`

## Readiness Workflow

Run the workflow manually:

```text
Backend Credential Readiness
```

This routine workflow does not reference the `production-backend` Environment
and does not request reviewer approval. It uses the repository-level
`NUTSNEWS_MAINTENANCE_GITHUB_TOKEN` only to read GitHub Environment metadata:
secret names plus non-secret variables. Production secret values are never
exposed to the job.

The workflow validates inventory consistency and required secret-name presence.
Secret value and shape checks remain fail-closed in the protected workflows that
consume those credentials.

To check one group, pass:

```text
cloudflare
grafana
supabase
restic
reporting_email
worker_api
worker_uplift_ai
backend_apply
```

After a credential rotation, run the separate manual workflow:

```text
Backend Protected Value Audit
```

That audit references `production-backend`, requires reviewer approval, and
checks injected values without printing them. Production apply, recovery,
cutover, restart, DNS, and failover workflows continue to use the same protected
Environment.

## Manual Provider Actions

These cannot be completed from the repo without the provider dashboards or token values:

- create the scoped Cloudflare token;
- create Grafana Cloud service account/access-policy tokens;
- create or retrieve Supabase production project credentials;
- create the restic repository and object-storage credentials;
- create SMTP/reporting provider credentials;
- approve GitHub `production-backend` deployments when production-changing workflows request access.

## Rollback

If a credential is wrong, rotate it in the provider and re-run the bootstrap helper with the replacement value.

If a credential is over-scoped, revoke it in the provider dashboard, create a narrower replacement, and update the GitHub Environment secret.

If the GitHub Environment is misconfigured, re-run:

```bash
scripts/bootstrap_production_backend_environment.sh --apply
```

Then run `Backend Credential Readiness` again.
