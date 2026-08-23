import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.db.derivation_schema import DERIVATION_TABLES
from app.main import initialize_database_on_startup
from database_setup_marriage import DatabaseManager


def test_production_rejects_database_auto_init() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", auto_init_db=True)


@pytest.mark.asyncio
async def test_database_bootstrap_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auto_init_db", False)
    await initialize_database_on_startup()


def test_derivation_tables_are_registered_with_database_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

        def fetchone(self) -> None:
            # 合著仓的 _ensure_admin_home_columns 用 SHOW COLUMNS 探列；
            # 返回 None 表示列已存在，跳过 backfill，使建表流程继续。
            return None

        def fetchall(self) -> list:
            return []

    manager = DatabaseManager.__new__(DatabaseManager)
    monkeypatch.setattr(manager, "_ensure_required_columns", lambda _cursor: None)
    monkeypatch.setattr(manager, "_backfill_comment_roots", lambda _cursor: None)
    monkeypatch.setattr(manager, "_backfill_topic_participants", lambda _cursor: None)
    monkeypatch.setattr(manager, "_add_all_foreign_keys", lambda _cursor: None)
    cursor = RecordingCursor()

    manager.init_all_tables(cursor)

    assert set(DERIVATION_TABLES) == {
        "user_revision_state",
        "derivation_outbox",
        "derivation_consumer_receipt",
    }
    assert all(
        any(
            "CREATE TABLE IF NOT EXISTS" in sql and table_name in sql
            for sql in cursor.statements
        )
        for table_name in DERIVATION_TABLES
    )
