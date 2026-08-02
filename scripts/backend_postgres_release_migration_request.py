#!/usr/bin/env python3
"""Validate a protected backend PostgreSQL release-migration request."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

CONFIRMATION = "apply-backend-postgres-release-migrations"
EXPECTED_REPOSITORY = "ramideltoro/nutsnews-backend"
EXPECTED_WORKFLOW_NAME = "Backend PostgreSQL Backup Restore Proof"
EXPECTED_WORKFLOW_PATH = ".github/workflows/backend-postgres-backup-restore-proof.yml"
MAX_BACKUP_AGE = dt.timedelta(hours=2)


class RequestError(ValueError):
    """A safe, user-facing request validation failure."""


def validate_request(source_commit: str, migration_head: str, backup_run_id: str, confirmation: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise RequestError("source_commit must be a full lowercase 40-character SHA.")
    if not re.fullmatch(r"[0-9]{14}", migration_head):
        raise RequestError("migration_head must be a 14-digit migration version.")
    if not re.fullmatch(r"[1-9][0-9]*", backup_run_id):
        raise RequestError("backup_proof_run_id must be a positive GitHub Actions run ID.")
    if confirmation != CONFIRMATION:
        raise RequestError(f"confirmation must be exactly {CONFIRMATION}.")
    return {
        "source_commit": source_commit,
        "migration_head": migration_head,
        "backup_run_id": backup_run_id,
    }


def _parse_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise RequestError("Backup proof run completion time is missing.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestError("Backup proof run completion time is invalid.") from exc
    if parsed.tzinfo is None:
        raise RequestError("Backup proof run completion time must include a timezone.")
    return parsed.astimezone(dt.timezone.utc)


def validate_backup_run(
    run: dict[str, Any],
    *,
    backup_run_id: str,
    repository: str,
    now: dt.datetime | None = None,
) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    workflow_path = str(run.get("path") or "").removeprefix(f"{repository}/")
    head_repository = (run.get("head_repository") or {}).get("full_name")
    if str(run.get("id") or "") != backup_run_id:
        raise RequestError("Backup proof run ID does not match the approved request.")
    if (
        repository != EXPECTED_REPOSITORY
        or run.get("name") != EXPECTED_WORKFLOW_NAME
        or workflow_path != EXPECTED_WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != "main"
        or head_repository != repository
    ):
        raise RequestError("Approved backup proof must be a successful manual main run of the fixed workflow.")

    completed_at = _parse_timestamp(run.get("updated_at") or run.get("completed_at"))
    age = now - completed_at
    if age < dt.timedelta(0) or age > MAX_BACKUP_AGE:
        raise RequestError("Approved backend PostgreSQL backup restore proof is not fresh enough.")
    return completed_at.isoformat().replace("+00:00", "Z")


def fetch_backup_run(repository: str, run_id: str, token: str) -> dict[str, Any]:
    if repository != EXPECTED_REPOSITORY or not token:
        raise RequestError("Trusted GitHub repository context is missing.")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nutsnews-backend-migration-preflight",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as exc:
        raise RequestError("Unable to verify the approved backup proof run through GitHub.") from exc


def write_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--migration-head", required=True)
    parser.add_argument("--backup-run-id", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    values = validate_request(args.source_commit, args.migration_head, args.backup_run_id, args.confirmation)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run = fetch_backup_run(repository, args.backup_run_id, os.environ.get("GITHUB_TOKEN", ""))
    values["backup_completed_at"] = validate_backup_run(
        run,
        backup_run_id=args.backup_run_id,
        repository=repository,
    )
    write_outputs(args.output, values)
    print(
        f"Validated release migration source {args.source_commit}, head {args.migration_head}, "
        f"and fresh backup proof run {args.backup_run_id}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RequestError as exc:
        raise SystemExit(f"Backend PostgreSQL release migration request rejected: {exc}") from None
