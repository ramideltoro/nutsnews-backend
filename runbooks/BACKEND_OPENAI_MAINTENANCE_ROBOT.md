# Backend OpenAI Maintenance Robot

This runbook covers backend issue #47 for the daily OpenAI-assisted
maintenance robot.

## Workflow

The workflow is:

```text
.github/workflows/backend-openai-maintenance.yml
```

The runner is:

```text
scripts/openai_maintenance_robot.py
```

It scans:

- `ramideltoro/nutsnews`
- `ramideltoro/nutsnews-backend`
- `ramideltoro/nutsnews-infra`
- `ramideltoro/nutsnews-docs`

The workflow runs daily at `11:43 UTC` and supports manual
`workflow_dispatch`.

## Modes

| Mode | Behavior |
| --- | --- |
| `dry-run` | Collects evidence, calls OpenAI, renders planned issues, and uploads the artifact without creating GitHub issues. Manual runs default to this mode. |
| `test` | Creates issues in `ramideltoro/nutsnews-backend` with the shared scan label and `maintenance-robot-test`. |
| `create` | Creates one GitHub issue per finding in the routed target repository. Scheduled runs use this mode. |

Each run uses a shared label shaped as:

```text
scan+YYYY-MM-DDTHH-MM-SSZ
```

Every issue created from the same run gets that label. Low-confidence and
duplicate-looking findings are still filed; uncertainty is recorded in the body.

## Evidence

The robot collects deterministic evidence before calling OpenAI:

- Git commit and checkout status for each scanned repo;
- unpinned GitHub Actions references in workflow files;
- TODO/FIXME/HACK comments with enough text to review;
- package/dependency manifest presence;
- fixed read-only backend server signals over SSH: hostname, failed units, disk,
  inode, service states, `/healthz`, and backup status.

All evidence is redacted before it is sent to OpenAI, written to artifacts, or
included in GitHub issues.

## OpenAI Configuration

Current OpenAI guidance was checked during implementation on 2026-07-17. The
resolver returned `gpt-5.6-sol`, and the OpenAI developer docs recommend the
Responses API for current model behavior. The workflow defaults to the
`gpt-5.6` alias with `OPENAI_REASONING_EFFORT=low`, which fits extraction,
routing, and classification work. Both values are repository variables so they
can be updated without a code change.

Required secrets:

| Secret | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI Responses API authentication |
| `NUTSNEWS_MAINTENANCE_GITHUB_TOKEN` | Cross-repository issue creation |
| `NUTSNEWS_BACKEND_SSH_PRIVATE_KEY` | Read-only backend SSH evidence |
| `NUTSNEWS_BACKEND_KNOWN_HOSTS` | Backend SSH host verification |

Optional secrets:

| Secret | Purpose |
| --- | --- |
| `OPENAI_ORG_ID` | Optional OpenAI organization routing |
| `OPENAI_PROJECT` | Optional OpenAI project routing |
| `NUTSNEWS_BACKEND_ANSIBLE_USER` | Optional SSH user override |

## Issue Body Contract

Generated findings include these sections:

- `Scan run label`
- `Finding fingerprint`
- `Detected repo`
- `Suggested target repo`
- `Category`
- `Severity`
- `Confidence`
- `Possible noise or false-positive reason`
- `Evidence`
- `Why this matters`
- `Suggested fix or investigation path`
- `Acceptance criteria`
- `Validation ideas`
- `Related files/log queries/checks`
- `Secret-redaction status`

The stable fingerprint lets later triage detect repeated findings. The robot
searches for possible open duplicates but still creates a new issue as required.

## Failure Reporting

If OpenAI candidate generation fails, the robot emits a failure finding. In
`create` or `test` mode, it attempts to create a backend issue for that failure.

If GitHub issue creation fails for one or more findings, the artifact preserves
the failed payload metadata and the robot attempts to create a follow-up backend
issue describing what did not get filed.

## Safety Boundaries

The robot does not:

- mutate production servers;
- auto-fix code;
- merge pull requests;
- close issues;
- run arbitrary SSH commands;
- print or persist secrets.

Server inspection is read-only. Remediation belongs in separate reviewed PRs and
protected apply workflows.

## Validation

Local validation:

```bash
python3 -m unittest tests.test_openai_maintenance_robot
python3 -m py_compile scripts/openai_maintenance_robot.py
actionlint .github/workflows/backend-openai-maintenance.yml
python3 scripts/validate_backend_credential_inventory.py
```

Dry-run with a fixture:

```bash
python3 scripts/openai_maintenance_robot.py \
  --mode dry-run \
  --repo-root ramideltoro/nutsnews-backend=. \
  --openai-response-fixture tests/fixtures/openai-maintenance-findings.json \
  --output /tmp/backend-openai-maintenance-report.json
```

## Rollback

Disable the workflow schedule or revert the backend PR if issue generation is
too noisy or provider calls fail. Rotate `OPENAI_API_KEY` or
`NUTSNEWS_MAINTENANCE_GITHUB_TOKEN` if either credential is suspected to be
exposed.
