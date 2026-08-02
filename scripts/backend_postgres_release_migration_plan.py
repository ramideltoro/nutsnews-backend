#!/usr/bin/env python3
"""Build a hash-locked backend PostgreSQL migration bundle from app source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "backend-postgres-release-migrations.json"
VERSION = re.compile(r"^[0-9]{14}$")
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
FILENAME = re.compile(r"^(?P<version>[0-9]{14})_[a-z0-9][a-z0-9_]*[a-z0-9]\.sql$")
FORBIDDEN_SQL = re.compile(
    r"(?im)^\s*(?:\\|begin\b|start\s+transaction\b|commit\b|rollback\b|alter\s+system\b|"
    r"create\s+database\b|drop\s+database\b|vacuum\b)|\bconcurrently\b"
)


class PlanError(ValueError):
    """A migration source or policy validation failure."""


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError("Backend release-migration policy is missing or invalid.") from exc
    if (
        policy.get("version") != 1
        or policy.get("source_repository") != "ramideltoro/nutsnews"
        or policy.get("target_database") != "nutsnews_primary_shadow"
        or not VERSION.fullmatch(str(policy.get("baseline_head") or ""))
        or not isinstance(policy.get("baseline_contract"), dict)
        or not isinstance(policy.get("migrations"), list)
    ):
        raise PlanError("Backend release-migration policy has an unsupported boundary.")
    return policy


def checked_out_commit(app_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=app_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PlanError("Approved app source is not a Git checkout.") from exc


def read_application_contract(app_root: Path) -> tuple[str, str]:
    try:
        source = (app_root / "web" / "migrationContract.mjs").read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError("Approved app source does not contain its compiled migration contract.") from exc
    head = re.search(r'MIGRATION_HEAD\s*=\s*"([0-9]{14})"', source)
    schema = re.search(r'LEGACY_COMPATIBLE_SCHEMA_VERSION\s*=\s*"([0-9]{14})"', source)
    if not head or not schema:
        raise PlanError("Approved app source has an invalid compiled migration contract.")
    return head.group(1), schema.group(1)


def build_plan(
    *,
    app_root: Path,
    source_commit: str,
    migration_head: str,
    policy_path: Path = DEFAULT_POLICY,
) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    if not SOURCE_COMMIT.fullmatch(source_commit) or not VERSION.fullmatch(migration_head):
        raise PlanError("Migration plan received an invalid immutable release identity.")
    if checked_out_commit(app_root) != source_commit:
        raise PlanError("Checked-out app source does not match the approved full SHA.")

    compiled_head, schema_version = read_application_contract(app_root)
    migrations_dir = app_root / "supabase" / "migrations"
    filenames = sorted(path.name for path in migrations_dir.glob("*.sql"))
    if not filenames or any(not FILENAME.fullmatch(filename) for filename in filenames):
        raise PlanError("Approved app source contains an invalid migration filename.")
    repository_head = FILENAME.fullmatch(filenames[-1]).group("version")
    if migration_head != repository_head or compiled_head != repository_head:
        raise PlanError("Requested, repository, and compiled migration heads do not match.")

    policy = load_policy(policy_path)
    baseline_head = policy["baseline_head"]
    baseline_contract = policy["baseline_contract"]
    if (
        not VERSION.fullmatch(str(baseline_contract.get("schema_version") or ""))
        or any(
            not re.fullmatch(r"[a-f0-9]{32}", str(baseline_contract.get(name) or ""))
            for name in ("expected_schema_fingerprint", "actual_schema_fingerprint")
        )
    ):
        raise PlanError("Reviewed backend baseline schema contract is invalid.")
    candidate_names = [
        filename
        for filename in filenames
        if baseline_head < FILENAME.fullmatch(filename).group("version") <= migration_head
    ]
    allowed = policy["migrations"]
    if candidate_names != [item.get("filename") for item in allowed]:
        raise PlanError("Forward migration set is not exactly represented by the reviewed backend allowlist.")

    previous_head = baseline_head
    bundled: list[tuple[Path, dict[str, Any]]] = []
    safe_entries: list[dict[str, Any]] = []
    for item in allowed:
        filename = str(item.get("filename") or "")
        match = FILENAME.fullmatch(filename)
        version = str(item.get("version") or "")
        if not match or version != match.group("version") or item.get("previous_head") != previous_head:
            raise PlanError("Reviewed backend migration allowlist is not a continuous forward chain.")
        path = migrations_dir / filename
        source = path.read_bytes()
        sha256 = hashlib.sha256(source).hexdigest()
        if sha256 != item.get("sha256"):
            raise PlanError(f"Migration {filename} does not match its reviewed SHA-256.")
        sql = source.decode("utf-8")
        if FORBIDDEN_SQL.search(sql):
            raise PlanError(f"Migration {filename} contains SQL excluded from the transactional runner.")
        head_call = re.compile(
            rf"select\s+public\.nutsnews_record_migration_head\(\s*'{re.escape(version)}'\s*\)\s*;",
            re.IGNORECASE,
        )
        if not head_call.search(sql):
            raise PlanError(f"Migration {filename} does not atomically record its own head.")
        roles = item.get("required_roles")
        if not isinstance(roles, list) or any(not re.fullmatch(r"[a-z_][a-z0-9_]*", str(role)) for role in roles):
            raise PlanError(f"Migration {filename} has an invalid required-role precondition.")
        safe_entry = {
            "version": version,
            "previous_head": previous_head,
            "filename": filename,
            "sha256": sha256,
            "required_roles": roles,
        }
        bundled.append((path, safe_entry))
        safe_entries.append(safe_entry)
        previous_head = version

    if previous_head != migration_head:
        raise PlanError("Reviewed backend migration allowlist does not reach the requested head.")

    plan = {
        "version": 1,
        "safe_metadata_only": True,
        "source_repository": policy["source_repository"],
        "source_commit": source_commit,
        "target_database": policy["target_database"],
        "baseline_head": baseline_head,
        "baseline_contract": baseline_contract,
        "migration_head": migration_head,
        "schema_version": schema_version,
        "migrations": safe_entries,
    }
    return plan, bundled


def write_bundle(output_dir: Path, plan: dict[str, Any], migrations: list[tuple[Path, dict[str, Any]]]) -> None:
    if output_dir.exists():
        raise PlanError("Migration bundle output directory must not already exist.")
    output_dir.mkdir(mode=0o700, parents=True)
    for source, entry in migrations:
        destination = output_dir / entry["filename"]
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    manifest = output_dir / "bundle-manifest.json"
    manifest.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--migration-head", required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    plan, migrations = build_plan(
        app_root=args.app_root.resolve(),
        source_commit=args.source_commit,
        migration_head=args.migration_head,
        policy_path=args.policy.resolve(),
    )
    write_bundle(args.output_dir.resolve(), plan, migrations)
    print(
        f"Built reviewed backend PostgreSQL migration bundle for {plan['source_commit']} "
        f"from {plan['baseline_head']} through {plan['migration_head']}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlanError as exc:
        raise SystemExit(f"Backend PostgreSQL release migration plan rejected: {exc}") from None
