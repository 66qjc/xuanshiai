"""Apply and verify the reviewed AI schema migration without Alembic."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

# ``python scripts/manage_ai_migration.py ...`` makes ``scripts/`` the first
# import root instead of the repository root.  Keep the documented direct
# invocation working even when PYTHONPATH has not been preconfigured.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database_setup_marriage import get_db_config  # noqa: E402

MIGRATION_ROOT = ROOT / "migrations" / "ai"
MANIFEST_PATH = MIGRATION_ROOT / "manifest.json"
MIGRATION_LOCK = "xuanshiai_ai_schema_migration_v1"
IGNORABLE_MYSQL_ERRORS = {1050, 1060, 1061, 1091}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class MigrationError(RuntimeError):
    """Raised for an unsafe or incomplete migration operation."""


def _checksum(path: Path, expected: str) -> None:
    # Normalise CRLF → LF before hashing so that Windows checkouts (where git
    # may convert LF to CRLF) produce the same SHA-256 as Unix checkouts.
    # Only line endings are normalised; any other byte change is still detected.
    raw = path.read_bytes()
    normalised = raw.replace(b"\r\n", b"\n")
    digest = hashlib.sha256(normalised).hexdigest()
    if expected and expected != digest:
        raise MigrationError(f"migration checksum mismatch: {path.name}")


def _normalize_versions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of per-version manifests from either new or legacy layout.

    New layout (preferred)::

        {"target": "ai", "requires": [...], "versions": [
            {"version": "...", "up": "...", "down": "...", "sha256": {...}}, ...
        ]}

    Legacy single-version layout (kept for backward compatibility)::

        {"version": "...", "up": "...", "down": "...", "sha256": {...},
         "requires": [...]}

    The legacy ``version``/``up``/``down`` keys are wrapped into a one-element
    ``versions`` list so the rest of the runner can treat every manifest the
    same way.
    """
    if "versions" in data:
        versions = data["versions"]
        if not isinstance(versions, list) or not versions:
            raise MigrationError("migration manifest has empty 'versions'")
        normalized: list[dict[str, Any]] = []
        for entry in versions:
            for key in ("version", "up", "down"):
                if key not in entry:
                    raise MigrationError(f"migration manifest version entry missing {key}")
            entry.setdefault("sha256", {})
            normalized.append(entry)
        return normalized
    # Legacy single-version manifest
    for key in ("version", "up", "down"):
        if key not in data:
            raise MigrationError(f"migration manifest missing {key}")
    data.setdefault("sha256", {})
    return [data]


def _manifest() -> dict[str, Any]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if "requires" not in data:
        raise MigrationError("migration manifest missing 'requires'")
    versions = _normalize_versions(data)
    for entry in versions:
        for direction in ("up", "down"):
            path = MIGRATION_ROOT / str(entry[direction])
            if not path.is_file():
                raise MigrationError(f"migration file not found: {entry[direction]}")
            _checksum(path, str(entry.get("sha256", {}).get(direction, "")))
    data["versions"] = versions
    return data


def _statements(path: Path) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                chunks.append(statement)
            current = []
    trailing = "\n".join(current).strip()
    if trailing:
        chunks.append(trailing)
    return chunks


def _database_config_for_target(target: str) -> dict[str, Any]:
    if target == "development":
        return get_db_config()
    test_database_url = os.getenv("AI_TEST_DATABASE_URL", "").strip()
    if not test_database_url:
        raise MigrationError(
            "AI_TEST_DATABASE_URL is required for --target test; "
            "refusing to fall back to the development database"
        )
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_database_url
    try:
        return get_db_config()
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


def _connect(target: str) -> tuple[Connection, str]:
    config = _database_config_for_target(target)
    database = str(config["database"])
    if not SAFE_IDENTIFIER.fullmatch(database):
        raise MigrationError("invalid database identifier")
    return pymysql.connect(**config, cursorclass=DictCursor, autocommit=False), database


def _ensure_history(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `ai_schema_migration` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `version` varchar(64) NOT NULL,
            `status` varchar(24) NOT NULL,
            `started_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `finished_at` datetime DEFAULT NULL,
            `last_step` int unsigned NOT NULL DEFAULT 0,
            `error_message` varchar(1000) DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_schema_migration_version` (`version`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def _lock(cursor: Any) -> None:
    cursor.execute("SELECT GET_LOCK(%s, 30) AS acquired", (MIGRATION_LOCK,))
    if int(cursor.fetchone()["acquired"] or 0) != 1:
        raise MigrationError("could not acquire AI migration advisory lock")


def _unlock(cursor: Any) -> None:
    cursor.execute("SELECT RELEASE_LOCK(%s)", (MIGRATION_LOCK,))


def _history(cursor: Any, version: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT version, status, last_step, error_message FROM ai_schema_migration "
        "WHERE version = %s LIMIT 1",
        (version,),
    )
    return cursor.fetchone()


def _record_start(cursor: Any, version: str, status: str) -> None:
    cursor.execute(
        "INSERT INTO ai_schema_migration (version, status, started_at, finished_at, last_step, error_message) "
        "VALUES (%s, %s, UTC_TIMESTAMP(), NULL, 0, NULL) "
        "ON DUPLICATE KEY UPDATE status = VALUES(status), started_at = UTC_TIMESTAMP(), "
        "finished_at = NULL, last_step = 0, error_message = NULL",
        (version, status),
    )


def _record_step(cursor: Any, version: str, step: int) -> None:
    cursor.execute(
        "UPDATE ai_schema_migration SET last_step = %s WHERE version = %s",
        (step, version),
    )


def _record_finish(cursor: Any, version: str, status: str, error: str | None = None) -> None:
    cursor.execute(
        "UPDATE ai_schema_migration SET status = %s, finished_at = UTC_TIMESTAMP(), error_message = %s "
        "WHERE version = %s",
        (status, error, version),
    )


def _execute_file(cursor: Any, path: Path, version: str) -> None:
    for step, statement in enumerate(_statements(path), start=1):
        try:
            cursor.execute(statement)
        except pymysql.MySQLError as exc:
            code = int(exc.args[0]) if exc.args and isinstance(exc.args[0], int) else 0
            if code not in IGNORABLE_MYSQL_ERRORS:
                raise
        _record_step(cursor, version, step)


def _table_columns(cursor: Any, table: str) -> set[str]:
    cursor.execute(
        "SELECT column_name AS column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return {str(row["column_name"]) for row in cursor.fetchall()}


def _index_names(cursor: Any, table: str) -> set[str]:
    cursor.execute(
        "SELECT DISTINCT index_name AS index_name FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return {str(row["index_name"]) for row in cursor.fetchall()}


def _column_nullable(cursor: Any, table: str, column: str) -> str:
    cursor.execute(
        "SELECT is_nullable AS is_nullable FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cursor.fetchone()
    return str(row["is_nullable"]) if row else ""


def _verify(
    cursor: Any,
    manifest: dict[str, Any],
    expect: str,
    database_name: str,
) -> None:
    if expect not in {"current", "previous"}:
        raise MigrationError("verify expect must be current or previous")
    versions = manifest["versions"]
    required = {str(item) for item in manifest["requires"]}
    cursor.execute(
        "SELECT table_name AS table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name LIKE 'ai\\_%'"
    )
    existing = {str(row["table_name"]) for row in cursor.fetchall()}
    missing_tables = sorted(required - existing)
    if missing_tables:
        raise MigrationError(f"missing AI tables: {missing_tables}")

    turn_columns = _table_columns(cursor, "ai_profile_turn")
    result_columns = _table_columns(cursor, "ai_search_result")
    session_indexes = _index_names(cursor, "ai_profile_session")
    result_indexes = _index_names(cursor, "ai_search_result")
    latest_version = str(versions[-1]["version"]) if versions else ""
    if expect == "current":
        required_columns = {
            "ai_profile_turn": {"turn_id", "updated_at"},
            "ai_search_result": {
                "projection_id",
                "source_hash",
                "consent_snapshot_json",
                "source_revision_json",
                "updated_at",
            },
        }
        missing = {
            table: sorted(columns - (_table_columns(cursor, table)))
            for table, columns in required_columns.items()
        }
        missing = {table: values for table, values in missing.items() if values}
        if missing or "active_slot" not in _table_columns(cursor, "ai_profile_session"):
            raise MigrationError(f"current AI schema incomplete: {missing}")
        if "uk_ai_profile_session_active" not in session_indexes:
            raise MigrationError("active session unique key missing")
        if "uk_ai_search_result_rank" in result_indexes:
            raise MigrationError("legacy global result-rank unique key still exists")
        if "idx_ai_search_result_snapshot_rank" not in result_indexes:
            raise MigrationError("result-rank lookup index missing")
        # When the latest version is 02_outbox_cleanup, also verify Task 10 columns.
        if latest_version.startswith("20260809_02"):
            outbox_columns = _table_columns(cursor, "derivation_outbox")
            receipt_columns = _table_columns(cursor, "derivation_consumer_receipt")
            required_task10 = {
                "derivation_outbox": {
                    "status", "attempt_count", "last_error_code", "dead_letter_at"
                },
                "derivation_consumer_receipt": {"event_type", "outcome", "duration_ms"},
                "ai_task": {"owner_tombstone"},
                "ai_consent_grant": {"user_tombstone"},
            }
            actual_task10 = {
                "derivation_outbox": outbox_columns,
                "derivation_consumer_receipt": receipt_columns,
                "ai_task": _table_columns(cursor, "ai_task"),
                "ai_consent_grant": _table_columns(cursor, "ai_consent_grant"),
            }
            missing_task10 = {
                table: sorted(columns - actual_task10[table])
                for table, columns in required_task10.items()
            }
            missing_task10 = {table: values for table, values in missing_task10.items() if values}
            if missing_task10:
                raise MigrationError(f"Task 10 AI schema incomplete: {missing_task10}")
            if _column_nullable(cursor, "ai_task", "owner_user_id") != "YES":
                raise MigrationError("ai_task.owner_user_id must be nullable in Task 10")
            if _column_nullable(cursor, "ai_consent_grant", "user_id") != "YES":
                raise MigrationError("ai_consent_grant.user_id must be nullable in Task 10")
    else:
        # "previous" = the rolled-back state. The semantics depend on how
        # many versions the manifest manages:
        #
        # * Multi-version manifest (e.g. 01 hardening + 02 outbox_cleanup):
        #   ``down`` reverses every version, so after rollback none of the
        #   managed hardening remains -- verify the pre-hardening state.
        # * Legacy single-version manifest (e.g. only 02, with 01 applied
        #   out-of-band): ``down`` rolls back only that version, so the
        #   earlier hardening stays -- verify it is preserved while the
        #   rolled-back version's additions are gone.
        if len(versions) >= 2:
            # Full rollback: hardened turn columns must be gone and the
            # legacy global rank unique key restored.
            if {"turn_id", "updated_at"} & turn_columns:
                raise MigrationError("previous schema still contains hardened turn columns")
            if "updated_at" in result_columns or "uk_ai_search_result_rank" not in result_indexes:
                raise MigrationError("previous AI schema was not restored")
            # Task 10 (02) additions must also be gone after full rollback.
            if {"status", "attempt_count", "last_error_code", "dead_letter_at"} & _table_columns(cursor, "derivation_outbox"):
                raise MigrationError("Task 10 outbox columns still exist")
            if {"event_type", "outcome", "duration_ms"} & _table_columns(cursor, "derivation_consumer_receipt"):
                raise MigrationError("Task 10 receipt columns still exist")
            if _column_nullable(cursor, "ai_task", "owner_user_id") != "NO":
                raise MigrationError("ai_task.owner_user_id was not restored to NOT NULL")
            if _column_nullable(cursor, "ai_consent_grant", "user_id") != "NO":
                raise MigrationError("ai_consent_grant.user_id was not restored to NOT NULL")
        elif latest_version.startswith("20260809_02"):
            # Single-version legacy manifest: 01 hardening preserved, 02 gone.
            if "turn_id" not in turn_columns or "updated_at" not in result_columns:
                raise MigrationError("base hardened AI schema was not preserved")
            if {"status", "attempt_count", "last_error_code", "dead_letter_at"} & _table_columns(cursor, "derivation_outbox"):
                raise MigrationError("Task 10 outbox columns still exist")
            if {"event_type", "outcome", "duration_ms"} & _table_columns(cursor, "derivation_consumer_receipt"):
                raise MigrationError("Task 10 receipt columns still exist")
            if _column_nullable(cursor, "ai_task", "owner_user_id") != "NO":
                raise MigrationError("ai_task.owner_user_id was not restored to NOT NULL")
            if _column_nullable(cursor, "ai_consent_grant", "user_id") != "NO":
                raise MigrationError("ai_consent_grant.user_id was not restored to NOT NULL")
        else:
            # Rolling back the only/earliest version: hardened columns gone.
            if {"turn_id", "updated_at"} & turn_columns:
                raise MigrationError("previous schema still contains hardened turn columns")
            if "updated_at" in result_columns or "uk_ai_search_result_rank" not in result_indexes:
                raise MigrationError("previous AI schema was not restored")
    print(f"verified={expect} database={database_name}")


def _active_task_count(cursor: Any) -> int:
    cursor.execute(
        "SELECT COUNT(*) AS count FROM ai_task "
        "WHERE status IN ('queued', 'leased', 'running', 'retry_wait')"
    )
    return int(cursor.fetchone()["count"])


def _run(command: str, target: str, expect: str = "current") -> int:
    if target not in {"test", "development"}:
        raise MigrationError("AI migration target must be test or development")
    # 仅对非 test 目标拒绝 down：test 库是可丢弃的，且 down 内部仍有
    # `_active_task_count` 守卫，会在存在活动 AI 任务时拒绝回滚。
    if (
        command == "down"
        and target != "test"
        and os.getenv("AI_MASTER_ENABLED", "false").lower() == "true"
    ):
        raise MigrationError(
            "refusing down while AI_MASTER_ENABLED=true (non-test target)"
        )
    manifest = _manifest()
    versions = manifest["versions"]
    connection, database_name = _connect(target)
    try:
        with connection.cursor() as cursor:
            _ensure_history(cursor)
            _lock(cursor)
            try:
                if command == "status":
                    rows = []
                    for entry in versions:
                        version = str(entry["version"])
                        rows.append(_history(cursor, version) or {"version": version, "status": "not_started"})
                    print(json.dumps(rows, default=str))
                    connection.commit()
                    return 0
                if command == "verify":
                    _verify(cursor, manifest, expect, database_name)
                    connection.commit()
                    return 0
                if command == "up":
                    for entry in versions:
                        version = str(entry["version"])
                        _record_start(cursor, version, "running")
                        connection.commit()
                        try:
                            _execute_file(cursor, MIGRATION_ROOT / str(entry["up"]), version)
                            _record_finish(cursor, version, "succeeded")
                            connection.commit()
                        except Exception as exc:
                            connection.rollback()
                            _record_finish(cursor, version, "failed", type(exc).__name__)
                            connection.commit()
                            raise
                    _verify(cursor, manifest, "current", database_name)
                    connection.commit()
                    print(f"migration={' -> '.join(str(v['version']) for v in versions)} status=succeeded")
                    return 0
                if command == "down":
                    if _active_task_count(cursor):
                        raise MigrationError("refusing down while AI tasks are active")
                    for entry in reversed(versions):
                        version = str(entry["version"])
                        _record_start(cursor, version, "rolling_back")
                        connection.commit()
                        try:
                            _execute_file(cursor, MIGRATION_ROOT / str(entry["down"]), version)
                            _record_finish(cursor, version, "rolled_back")
                            connection.commit()
                        except Exception as exc:
                            connection.rollback()
                            _record_finish(cursor, version, "rollback_failed", type(exc).__name__)
                            connection.commit()
                            raise
                    _verify(cursor, manifest, "previous", database_name)
                    connection.commit()
                    print(f"migration={' -> '.join(str(v['version']) for v in reversed(versions))} status=rolled_back")
                    return 0
                raise MigrationError(f"unknown migration command: {command}")
            finally:
                _unlock(cursor)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage reviewed AI schema migrations")
    parser.add_argument("command", choices=("status", "up", "down", "verify"))
    parser.add_argument("--target", required=True, choices=("test", "development"))
    parser.add_argument("--expect", default="current", choices=("current", "previous"))
    args = parser.parse_args(argv)
    try:
        return _run(args.command, args.target, args.expect)
    except (MigrationError, pymysql.MySQLError) as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
