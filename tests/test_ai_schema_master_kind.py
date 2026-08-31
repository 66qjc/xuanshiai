"""session_kind 枚举必须包含 master（墨相师对话建构会话）。

覆盖：DDL 常量口径、存量旧枚举列（enum('build','update')）的升级判定、
ensure 迁移的幂等 MODIFY / 补缺列路径。
"""
from app.db.ai_schema import (
    AI_PROFILE_SESSION_KIND_DDL,
    ensure_ai_profile_session_columns,
    session_kind_needs_upgrade,
)


def test_session_kind_enum_contains_master() -> None:
    assert "'master'" in AI_PROFILE_SESSION_KIND_DDL
    assert AI_PROFILE_SESSION_KIND_DDL.index("'build'") < AI_PROFILE_SESSION_KIND_DDL.index("'master'")


def test_session_kind_needs_upgrade_detects_legacy_enum() -> None:
    assert session_kind_needs_upgrade("enum('build','update')") is True


def test_session_kind_needs_upgrade_passes_after_master_added() -> None:
    assert session_kind_needs_upgrade("enum('build','update','master')") is False


class _RecordingCursor:
    """最小 fake cursor（仿 tests/test_database_bootstrap.py 的 RecordingCursor）：
    SHOW COLUMNS 返回给定行，记录全部已执行语句。"""

    def __init__(self, columns: list[dict[str, str]]) -> None:
        self._columns = columns
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchall(self) -> list[dict[str, str]]:
        return self._columns


def test_ensure_modifies_legacy_enum_column_to_include_master() -> None:
    cursor = _RecordingCursor(
        [{"Field": "session_kind", "Type": "enum('build','update')"}]
    )

    ensure_ai_profile_session_columns(cursor)

    alters = [s for s in cursor.statements if s.startswith("ALTER TABLE")]
    assert len(alters) == 1
    assert "MODIFY COLUMN" in alters[0]
    assert AI_PROFILE_SESSION_KIND_DDL in alters[0]
    assert not any("ADD COLUMN" in s for s in cursor.statements)


def test_ensure_is_idempotent_once_master_present() -> None:
    cursor = _RecordingCursor(
        [{"Field": "session_kind", "Type": "enum('build','update','master')"}]
    )

    ensure_ai_profile_session_columns(cursor)

    assert not [s for s in cursor.statements if s.startswith("ALTER TABLE")]


def test_ensure_keeps_add_column_path_for_missing_column() -> None:
    cursor = _RecordingCursor([])

    ensure_ai_profile_session_columns(cursor)

    alters = [s for s in cursor.statements if s.startswith("ALTER TABLE")]
    assert len(alters) == 1
    assert "ADD COLUMN" in alters[0]
    assert AI_PROFILE_SESSION_KIND_DDL in alters[0]
    assert "MODIFY COLUMN" not in alters[0]
