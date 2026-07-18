#!/usr/bin/env python3
"""Report Supabase migration drift gate status without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-parity.json"
MIGRATION_RE = re.compile(r"^(\d{8,})")


def migration_versions(path: Path) -> list[str]:
    if not path.exists():
        return []
    versions: list[str] = []
    for item in sorted(path.iterdir()):
        if not item.is_file():
            continue
        match = MIGRATION_RE.match(item.name)
        if match:
            versions.append(match.group(1))
    return versions


def source_versions_from_psql(db_url: str) -> tuple[list[str], str | None]:
    query = (
        "select version::text from supabase_migrations.schema_migrations "
        "order by version"
    )
    try:
        result = subprocess.run(
            ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", db_url, "-c", query],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return [], "psql_not_installed"
    except subprocess.TimeoutExpired:
        return [], "source_query_timeout"
    except subprocess.CalledProcessError:
        return [], "source_query_failed"

    versions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return versions, None


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--migrations-dir", default=os.environ.get("NUTSNEWS_MIGRATIONS_DIR", "supabase/migrations"))
    parser.add_argument("--source-db-url-env", default="NUTSNEWS_PRODUCTION_SUPABASE_DB_URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    migrations_dir = Path(args.migrations_dir)
    if not migrations_dir.is_absolute():
        migrations_dir = ROOT / migrations_dir

    manifest = load_manifest(manifest_path)
    local_versions = migration_versions(migrations_dir)
    source_db_url = os.environ.get(args.source_db_url_env, "").strip()
    source_versions: list[str] = []
    source_error: str | None = None
    if source_db_url:
        source_versions, source_error = source_versions_from_psql(source_db_url)
    else:
        source_error = "missing_source_db_url"

    missing_locally = sorted(set(source_versions) - set(local_versions))
    missing_in_source = sorted(set(local_versions) - set(source_versions)) if source_versions else []
    blockers: list[str] = []
    warnings: list[str] = []

    if not manifest:
        blockers.append("manifest_unreadable")
    if not migrations_dir.exists():
        blockers.append("local_migrations_dir_missing")
    if source_error:
        blockers.append(source_error)
    if missing_locally:
        blockers.append("source_versions_missing_locally")
    if missing_in_source:
        warnings.append("local_versions_not_seen_in_source")

    status = "pass"
    if blockers:
        status = "blocked" if any(blocker in blockers for blocker in ("missing_source_db_url", "local_migrations_dir_missing", "source_query_failed", "source_query_timeout", "psql_not_installed")) else "fail"
    elif warnings:
        status = "warning"

    report = {
        "status": status,
        "manifest": str(manifest_path.relative_to(ROOT) if manifest_path.is_relative_to(ROOT) else manifest_path),
        "manifest_version": manifest.get("version"),
        "migrations_dir": str(migrations_dir.relative_to(ROOT) if migrations_dir.is_relative_to(ROOT) else migrations_dir),
        "source_db_url_env": args.source_db_url_env,
        "source_db_url_present": bool(source_db_url),
        "local_migration_count": len(local_versions),
        "source_migration_count": len(source_versions),
        "local_latest": local_versions[-1] if local_versions else None,
        "source_latest": source_versions[-1] if source_versions else None,
        "missing_locally": missing_locally,
        "missing_in_source": missing_in_source,
        "blockers": blockers,
        "warnings": warnings,
    }

    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)

    if args.enforce and status != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
