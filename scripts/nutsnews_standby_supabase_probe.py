#!/usr/bin/env python3
"""Fixed Supabase direct PostgreSQL readiness probe for forced-command SSH."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import TracebackType
from typing import IO, Mapping
from urllib.parse import parse_qs, unquote, urlsplit


CONFIG_PATH = Path("/etc/nutsnews-standby-probe/probe.conf")
LOCK_PATH = Path("/run/lock/nutsnews-standby-probe.lock")
MAX_INPUT_BYTES = 2048
LOCK_TIMEOUT_SECONDS = 2.0
PSQL_TIMEOUT_SECONDS = 10.0
PG_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SUCCESS_TOKEN = "READY"
FAILURE_TOKEN = "FAILED"
EXPECTED_CONFIG_KEYS = {
    "expected_project_ref",
    "expected_host",
}
PROJECT_REF_PATTERN = re.compile(r"[a-z0-9]{20}")

FIXED_READ_ONLY_SQL = """\
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5000ms';
SELECT current_database() = 'postgres'
  AND current_setting('server_version_num')::int >= 150000
  AND pg_is_in_recovery() IN (true, false);
ROLLBACK;
"""


class ProbeError(Exception):
    """Safe internal failure marker. The message must never be printed."""


@dataclass(frozen=True)
class ExpectedTarget:
    project_ref: str
    host: str


@dataclass(frozen=True)
class ParsedDatabaseUrl:
    username: str
    password: str
    host: str
    port: int
    database: str


class FileLock:
    """Small non-blocking flock wrapper with a bounded acquisition timeout."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle: IO[str] | None = None

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProbeError("probe busy") from exc
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()


def load_expected_target(config_path: Path = CONFIG_PATH) -> ExpectedTarget:
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProbeError("invalid config")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in EXPECTED_CONFIG_KEYS or not value:
            raise ProbeError("invalid config")
        values[key] = value

    if set(values) != EXPECTED_CONFIG_KEYS:
        raise ProbeError("incomplete config")

    project_ref = values["expected_project_ref"]
    expected_host = values["expected_host"].lower()
    if not PROJECT_REF_PATTERN.fullmatch(project_ref):
        raise ProbeError("invalid project ref")
    if expected_host != f"db.{project_ref}.supabase.co":
        raise ProbeError("invalid expected host")

    return ExpectedTarget(project_ref=project_ref, host=expected_host)


def read_database_url(stdin: IO[bytes]) -> str:
    raw = stdin.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ProbeError("invalid input length")
    if b"\n" in raw or b"\r" in raw:
        raise ProbeError("multiline input")
    try:
        database_url = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("invalid input encoding") from exc
    if not database_url:
        raise ProbeError("empty input")
    return database_url


def parse_database_url(database_url: str, expected: ExpectedTarget) -> ParsedDatabaseUrl:
    try:
        parsed = urlsplit(database_url)
    except ValueError as exc:
        raise ProbeError("invalid url") from exc

    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ProbeError("invalid scheme")
    if parsed.hostname is None or parsed.hostname.lower() != expected.host:
        raise ProbeError("invalid host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProbeError("invalid port") from exc
    if port != 5432:
        raise ProbeError("invalid port")

    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    if database != "postgres" or ";" in parsed.path:
        raise ProbeError("invalid database")
    if parsed.fragment:
        raise ProbeError("invalid fragment")

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        raise ProbeError("missing credentials")

    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    if set(query) != {"sslmode"} or query["sslmode"] != ["require"]:
        raise ProbeError("invalid query")

    return ParsedDatabaseUrl(
        username=username,
        password=password,
        host=expected.host,
        port=port,
        database=database,
    )


def run_fixed_psql_probe(target: ParsedDatabaseUrl, lock_path: Path = LOCK_PATH) -> bool:
    psql = shutil.which("psql", path=DEFAULT_PATH)
    if not psql:
        raise ProbeError("psql unavailable")

    psql_env = {
        "PATH": DEFAULT_PATH,
        "PGAPPNAME": "nutsnews-standby-probe",
        "PGCONNECT_TIMEOUT": str(PG_CONNECT_TIMEOUT_SECONDS),
        "PGDATABASE": target.database,
        "PGHOST": target.host,
        "PGPASSWORD": target.password,
        "PGPORT": str(target.port),
        "PGSSLMODE": "require",
        "PGUSER": target.username,
    }

    started = time.monotonic()
    with FileLock(lock_path, LOCK_TIMEOUT_SECONDS):
        remaining_timeout = max(1.0, PSQL_TIMEOUT_SECONDS - (time.monotonic() - started))
        try:
            completed = subprocess.run(
                [
                    psql,
                    "--no-psqlrc",
                    "--set=ON_ERROR_STOP=1",
                    "--quiet",
                    "--tuples-only",
                    "--no-align",
                ],
                input=FIXED_READ_ONLY_SQL.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=psql_env,
                shell=False,
                timeout=remaining_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError("psql failed") from exc

    if completed.returncode != 0:
        return False
    return completed.stdout.decode("utf-8", errors="replace").strip() == "t"


def original_command_is_empty(environ: Mapping[str, str]) -> bool:
    return not environ.get("SSH_ORIGINAL_COMMAND", "")


def run_probe(
    *,
    stdin: IO[bytes] = sys.stdin.buffer,
    environ: Mapping[str, str] = os.environ,
    config_path: Path = CONFIG_PATH,
    lock_path: Path = LOCK_PATH,
) -> bool:
    if not original_command_is_empty(environ):
        raise ProbeError("original command rejected")
    expected = load_expected_target(config_path)
    database_url = read_database_url(stdin)
    parsed = parse_database_url(database_url, expected)
    return run_fixed_psql_probe(parsed, lock_path)


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[bytes] = sys.stdin.buffer,
    stdout: IO[str] = sys.stdout,
    environ: Mapping[str, str] = os.environ,
    config_path: Path = CONFIG_PATH,
    lock_path: Path = LOCK_PATH,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args:
            raise ProbeError("arguments rejected")
        ok = run_probe(stdin=stdin, environ=environ, config_path=config_path, lock_path=lock_path)
    except Exception:
        print(FAILURE_TOKEN, file=stdout)
        return 1

    print(SUCCESS_TOKEN if ok else FAILURE_TOKEN, file=stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
