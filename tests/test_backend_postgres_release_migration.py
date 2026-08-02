import datetime as dt
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


request = load_module(
    "backend_postgres_release_migration_request",
    ROOT / "scripts" / "backend_postgres_release_migration_request.py",
)
plan = load_module(
    "backend_postgres_release_migration_plan",
    ROOT / "scripts" / "backend_postgres_release_migration_plan.py",
)
report_validator = load_module(
    "validate_backend_postgres_release_migration_report",
    ROOT / "scripts" / "validate_backend_postgres_release_migration_report.py",
)


class BackendPostgresReleaseMigrationTests(unittest.TestCase):
    def test_request_requires_immutable_identity_and_exact_confirmation(self):
        values = request.validate_request(
            "a" * 40,
            "20260802040522",
            "12345",
            request.CONFIRMATION,
        )
        self.assertEqual(values["migration_head"], "20260802040522")
        with self.assertRaisesRegex(request.RequestError, "confirmation must be exactly"):
            request.validate_request("a" * 40, "20260802040522", "12345", "yes")

    def test_backup_proof_must_be_fresh_successful_manual_main_run(self):
        now = dt.datetime(2026, 8, 2, 7, 0, tzinfo=dt.timezone.utc)
        run = {
            "id": 12345,
            "name": request.EXPECTED_WORKFLOW_NAME,
            "path": request.EXPECTED_WORKFLOW_PATH,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_repository": {"full_name": request.EXPECTED_REPOSITORY},
            "updated_at": "2026-08-02T06:30:00Z",
        }
        completed = request.validate_backup_run(
            run,
            backup_run_id="12345",
            repository=request.EXPECTED_REPOSITORY,
            now=now,
        )
        self.assertEqual(completed, "2026-08-02T06:30:00Z")
        stale = {**run, "updated_at": "2026-08-02T04:00:00Z"}
        with self.assertRaisesRegex(request.RequestError, "not fresh enough"):
            request.validate_backup_run(
                stale,
                backup_run_id="12345",
                repository=request.EXPECTED_REPOSITORY,
                now=now,
            )

    def test_plan_bundles_only_the_continuous_hash_allowlist(self):
        source_commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "supabase" / "migrations"
            migrations.mkdir(parents=True)
            (root / "web").mkdir()
            (root / "web" / "migrationContract.mjs").write_text(
                'export const MIGRATION_HEAD = "20260103000000";\n'
                'export const LEGACY_COMPATIBLE_SCHEMA_VERSION = "20260101000000";\n',
                encoding="utf-8",
            )
            baseline = migrations / "20260101000000_baseline.sql"
            first = migrations / "20260102000000_first.sql"
            second = migrations / "20260103000000_second.sql"
            baseline.write_text("select 1;\n", encoding="utf-8")
            first.write_text("select public.nutsnews_record_migration_head('20260102000000');\n", encoding="utf-8")
            second.write_text("select public.nutsnews_record_migration_head('20260103000000');\n", encoding="utf-8")
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source_repository": "ramideltoro/nutsnews",
                        "target_database": "nutsnews_primary_shadow",
                        "baseline_head": "20260101000000",
                        "baseline_contract": {
                            "schema_version": "20260101000000",
                            "expected_schema_fingerprint": "1" * 32,
                            "actual_schema_fingerprint": "2" * 32,
                        },
                        "migrations": [
                            {
                                "version": "20260102000000",
                                "previous_head": "20260101000000",
                                "filename": first.name,
                                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                                "required_roles": [],
                            },
                            {
                                "version": "20260103000000",
                                "previous_head": "20260102000000",
                                "filename": second.name,
                                "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
                                "required_roles": ["authenticator"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(plan, "checked_out_commit", return_value=source_commit):
                result, files = plan.build_plan(
                    app_root=root,
                    source_commit=source_commit,
                    migration_head="20260103000000",
                    policy_path=policy_path,
                )
            self.assertEqual(result["baseline_head"], "20260101000000")
            self.assertEqual([entry["filename"] for _, entry in files], [first.name, second.name])

            second.write_text("drop database postgres;\n", encoding="utf-8")
            with mock.patch.object(plan, "checked_out_commit", return_value=source_commit):
                with self.assertRaisesRegex(plan.PlanError, "reviewed SHA-256"):
                    plan.build_plan(
                        app_root=root,
                        source_commit=source_commit,
                        migration_head="20260103000000",
                        policy_path=policy_path,
                    )

    def test_safe_report_validator_rejects_secret_material(self):
        valid = {
            "version": 1,
            "status": "pass",
            "safe_metadata_only": True,
            "target_database": "nutsnews_primary_shadow",
            "source_commit": "c" * 40,
            "starting_migration_head": "20260717113000",
            "migration_head": "20260802040522",
            "schema_version": "20260712170000",
            "applied_migration_count": 2,
            "pre_schema_sha256": "d" * 64,
            "backup_proof_url": "https://github.com/ramideltoro/nutsnews-backend/actions/runs/12345",
            "transactional": True,
            "advisory_lock": "nutsnews:backend-release-migration",
            "completed_at_utc": "2026-08-02T07:00:00Z",
            "rollback": "restore the exact encrypted backup proof snapshot or apply a reviewed forward repair",
        }
        self.assertEqual(report_validator.validate(valid), [])
        self.assertTrue(report_validator.validate({**valid, "notes": "password=secret"}))

    def test_workflow_and_remote_runner_keep_protected_boundaries(self):
        workflow = (ROOT / ".github" / "workflows" / "backend-postgres-release-migration.yml").read_text()
        remote = (ROOT / "scripts" / "backend_postgres_release_migrate_remote.sh").read_text()
        for required in (
            "environment: production-backend",
            "inputs.mode == 'apply'",
            "git merge-base --is-ancestor",
            "backend-postgres-release-migration-bundle",
            "StrictHostKeyChecking=yes",
            "EXPECTED_BACKUP_PROOF_URL",
            "nutsnews_primary_shadow",
        ):
            self.assertIn(required, workflow)
        for required in (
            "pg_advisory_xact_lock",
            "-1",
            "primary-shadow-backup-restore-proof.json",
            "expected_schema_fingerprint",
            "actual_schema_fingerprint",
            "pg_dump -s --no-owner --no-privileges",
        ):
            self.assertIn(required, remote)


if __name__ == "__main__":
    unittest.main()
