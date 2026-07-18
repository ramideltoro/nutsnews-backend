#!/usr/bin/env python3
"""Create safe metadata for Supabase logical export artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "supabase-backend-postgres-parity.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: Path, label: str) -> dict:
    if not path.exists():
        return {
            "label": label,
            "path": path.name,
            "status": "missing",
        }
    return {
        "label": label,
        "path": path.name,
        "status": "present",
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-environment", required=True, choices=["staging", "production"])
    parser.add_argument("--source-project-ref", default="")
    parser.add_argument("--started-at-utc", default="")
    parser.add_argument("--roles-dump", default="")
    parser.add_argument("--schema-dump", default="")
    parser.add_argument("--data-dump", default="")
    parser.add_argument("--migration-history-dump", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    started_at = args.started_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    completed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    artifacts = []
    for label, value in (
        ("roles", args.roles_dump),
        ("schema", args.schema_dump),
        ("data", args.data_dump),
        ("migration_history", args.migration_history_dump),
    ):
        if value:
            artifacts.append(file_metadata(Path(value), label))

    missing = [artifact["label"] for artifact in artifacts if artifact["status"] != "present"]
    report = {
        "status": "pass" if not missing else "fail",
        "source_environment": args.source_environment,
        "source_project_ref": args.source_project_ref,
        "manifest": str(manifest_path.relative_to(ROOT) if manifest_path.is_absolute() and manifest_path.is_relative_to(ROOT) else manifest_path),
        "manifest_version": manifest.get("version"),
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "artifacts": artifacts,
        "missing_artifacts": missing,
        "safe_metadata_only": True,
        "notes": [
            "Dump files are not included in this report and must not be uploaded as GitHub artifacts.",
            "Checksums and byte counts are safe metadata for review evidence.",
        ],
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
