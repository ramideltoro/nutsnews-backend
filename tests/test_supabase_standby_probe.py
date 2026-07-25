from __future__ import annotations

import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlunsplit

from scripts import nutsnews_standby_supabase_probe as probe


PROJECT_REF = "abcdefghijklmnopqrst"
HOST = f"db.{PROJECT_REF}.supabase.co"
USER = "probe_user"
PASSWORD = "not-secret-test-password"


def make_database_url(
    *,
    scheme: str = "postgresql",
    username: str = USER,
    password: str = PASSWORD,
    host: str = HOST,
    port: str = "5432",
    database: str = "postgres",
    query: str = "sslmode=require",
) -> str:
    netloc = f"{username}:{password}@{host}:{port}"
    return urlunsplit((scheme, netloc, f"/{database}", query, ""))


class SupabaseStandbyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "probe.conf"
        self.lock_path = self.root / "probe.lock"
        self.config_path.write_text(
            f"expected_project_ref={PROJECT_REF}\nexpected_host={HOST}\n",
            encoding="utf-8",
        )

    def run_main(self, database_url: str, *, environ: dict[str, str] | None = None, argv: list[str] | None = None):
        stdout = io.StringIO()
        with mock.patch("scripts.nutsnews_standby_supabase_probe.shutil.which", return_value="/usr/bin/psql") as which:
            with mock.patch("scripts.nutsnews_standby_supabase_probe.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=["/usr/bin/psql"],
                    returncode=0,
                    stdout=b"t\n",
                    stderr=b"raw database error that must never be emitted",
                )
                code = probe.main(
                    [] if argv is None else argv,
                    stdin=io.BytesIO(database_url.encode("utf-8")),
                    stdout=stdout,
                    environ={} if environ is None else environ,
                    config_path=self.config_path,
                    lock_path=self.lock_path,
                )
        return code, stdout.getvalue(), which, run

    def test_valid_probe_returns_ready_and_keeps_secret_out_of_argv(self) -> None:
        database_url = make_database_url()
        code, stdout, _, run = self.run_main(database_url)

        self.assertEqual(0, code)
        self.assertEqual("READY\n", stdout)
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertNotIn(database_url, args)
        self.assertNotIn(PASSWORD, " ".join(args))
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(probe.FIXED_READ_ONLY_SQL.encode("utf-8"), run.call_args.kwargs["input"])
        self.assertEqual(PASSWORD, run.call_args.kwargs["env"]["PGPASSWORD"])
        self.assertEqual(HOST, run.call_args.kwargs["env"]["PGHOST"])
        self.assertEqual("postgres", run.call_args.kwargs["env"]["PGDATABASE"])
        self.assertEqual("require", run.call_args.kwargs["env"]["PGSSLMODE"])
        self.assertEqual("5432", run.call_args.kwargs["env"]["PGPORT"])

    def test_rejects_non_empty_original_command(self) -> None:
        code, stdout, _, run = self.run_main(make_database_url(), environ={"SSH_ORIGINAL_COMMAND": "whoami"})

        self.assertEqual(1, code)
        self.assertEqual("FAILED\n", stdout)
        run.assert_not_called()

    def test_rejects_arguments(self) -> None:
        code, stdout, _, run = self.run_main(make_database_url(), argv=["whoami"])

        self.assertEqual(1, code)
        self.assertEqual("FAILED\n", stdout)
        run.assert_not_called()

    def test_rejects_multiline_and_oversized_input(self) -> None:
        for database_url in [
            make_database_url() + "\n",
            "x" * (probe.MAX_INPUT_BYTES + 1),
            "",
        ]:
            with self.subTest(database_url=database_url[:16]):
                code, stdout, _, run = self.run_main(database_url)
                self.assertEqual(1, code)
                self.assertEqual("FAILED\n", stdout)
                run.assert_not_called()

    def test_rejects_unsafe_url_shapes(self) -> None:
        unsafe_urls = [
            make_database_url(scheme="https"),
            make_database_url(host="pooler.example.invalid"),
            make_database_url(host=f"db.{PROJECT_REF}.supabase.co.evil.example"),
            make_database_url(port="6543"),
            make_database_url(database="otherdb"),
            make_database_url(username=""),
            make_database_url(password=""),
            make_database_url(query="sslmode=prefer"),
            make_database_url(query="sslmode=require&connect_timeout=60"),
        ]
        for database_url in unsafe_urls:
            with self.subTest(database_url=database_url):
                code, stdout, _, run = self.run_main(database_url)
                self.assertEqual(1, code)
                self.assertEqual("FAILED\n", stdout)
                run.assert_not_called()

    def test_psql_failure_returns_generic_failure_without_leaks(self) -> None:
        stdout = io.StringIO()
        database_url = make_database_url()
        with mock.patch("scripts.nutsnews_standby_supabase_probe.shutil.which", return_value="/usr/bin/psql"):
            with mock.patch("scripts.nutsnews_standby_supabase_probe.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=["/usr/bin/psql"],
                    returncode=2,
                    stdout=f"raw row data for {HOST}".encode("utf-8"),
                    stderr=f"password {PASSWORD} failed".encode("utf-8"),
                )
                code = probe.main(
                    [],
                    stdin=io.BytesIO(database_url.encode("utf-8")),
                    stdout=stdout,
                    environ={},
                    config_path=self.config_path,
                    lock_path=self.lock_path,
                )

        self.assertEqual(1, code)
        self.assertEqual("FAILED\n", stdout.getvalue())
        self.assertNotIn(HOST, stdout.getvalue())
        self.assertNotIn(PASSWORD, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
