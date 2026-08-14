"""Real MySQL checks for the forward/rollback AI schema migration runner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_migration(*arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "scripts/manage_ai_migration.py", *arguments, "--target", "test"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def _clear_profile_session_fixture(database_url: str) -> None:
    """Keep the rollback assertion on a clean, disposable migration fixture."""
    parsed = urlsplit(database_url.replace("mysql+aiomysql://", "mysql://", 1))
    connection = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
        database=parsed.path.lstrip("/"),
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM `ai_profile_session`")
    finally:
        connection.close()


def test_real_migration_is_idempotent_and_reversible(
    ai_test_environment: dict[str, str],
) -> None:
    try:
        first = _run_migration("up")
        _assert_ok(first)

        second = _run_migration("up")
        _assert_ok(second)

        verified = _run_migration("verify")
        _assert_ok(verified)
        assert "verified" in (verified.stdout + verified.stderr).lower()

        _clear_profile_session_fixture(ai_test_environment["DATABASE_URL"])
        down = _run_migration("down")
        _assert_ok(down)

        previous = _run_migration("verify", "--expect", "previous")
        _assert_ok(previous)
    finally:
        # Leave the dedicated compose database on the final schema for the
        # remaining real integration tests and for an interactive rerun.
        restore = _run_migration("up")
        _assert_ok(restore)
