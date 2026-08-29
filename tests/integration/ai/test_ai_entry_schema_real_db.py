"""画像条目模型（WP-P1a）真实库 schema 契约。

覆盖三类保证：
1. 全新库 bootstrap 后两张字段表自带 entry 4 列；
2. 存量行/缺省插入行 ``field_kind`` 默认 'structured'（structured 链路零影响）；
3. 旧库场景：临时库建旧结构 → ``ensure_ai_profile_entry_columns`` 幂等补列
   （连跑两遍不报错），不触碰共享测试库。
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import pymysql
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ai_schema import ensure_ai_profile_entry_columns
from tests.integration.ai.conftest import TEST_DATABASE_NAME, TEST_DATABASE_URL

ENTRY_COLUMNS = {"field_kind", "category", "content", "replaces_field_key"}
ENTRY_TABLES = ("ai_profile_draft_field", "ai_profile_revision_field")


async def _columns(db: AsyncSession, table_name: str) -> set[str]:
    result = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": TEST_DATABASE_NAME, "table": table_name},
    )
    return {str(row[0]) for row in result.all()}


@pytest.mark.asyncio
async def test_real_entry_columns_present_on_both_field_tables(
    real_db_session: AsyncSession,
) -> None:
    missing = {
        table: sorted(ENTRY_COLUMNS - await _columns(real_db_session, table))
        for table in ENTRY_TABLES
    }
    missing = {table: cols for table, cols in missing.items() if cols}
    assert not missing, f"entry columns missing on real schema: {missing}"


@pytest.mark.asyncio
async def test_real_entry_columns_default_to_structured(
    real_db_session: AsyncSession,
) -> None:
    """缺省插入不感知新列：field_kind 落库即 'structured'，entry 专用列为 NULL。"""
    marker = uuid.uuid4().hex[:12]
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_draft_field "
            "(draft_id, field_key, subject, display_value) "
            "VALUES (:draft_id, :field_key, 'personal', :display_value)"
        ),
        {"draft_id": f"it-{marker}", "field_key": "height_cm", "display_value": "175"},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_revision_field "
            "(revision_id, field_key, subject, content_hash) "
            "VALUES (990000001, :field_key, 'personal', :content_hash)"
        ),
        {"field_key": "height_cm", "content_hash": "0" * 64},
    )
    row = (
        await real_db_session.execute(
            text(
                "SELECT field_kind, category, content, replaces_field_key "
                "FROM ai_profile_draft_field WHERE draft_id = :draft_id"
            ),
            {"draft_id": f"it-{marker}"},
        )
    ).one()
    assert row[0] == "structured"
    assert row[1] is None and row[2] is None and row[3] is None
    rev_row = (
        await real_db_session.execute(
            text(
                "SELECT field_kind FROM ai_profile_revision_field "
                "WHERE revision_id = 990000001 AND field_key = 'height_cm'"
            )
        )
    ).one()
    assert rev_row[0] == "structured"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_ensure_entry_columns_idempotent_on_legacy_database() -> None:
    """旧库补列：临时库建旧结构 → ensure 连跑两遍 → 4 列齐备且默认 'structured'。"""
    parts = urlsplit(TEST_DATABASE_URL)
    legacy_db = f"xuanshiai_ai_entry_legacy_{uuid.uuid4().hex[:8]}"
    conn = pymysql.connect(
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 3306,
        user=parts.username or "root",
        password=parts.password or "",
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{legacy_db}`")
            cursor.execute(f"USE `{legacy_db}`")
            cursor.execute(
                """
                CREATE TABLE `ai_profile_draft_field` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `draft_id` varchar(64) NOT NULL,
                    `field_key` varchar(64) NOT NULL,
                    `subject` varchar(24) NOT NULL,
                    `confirmation_status` varchar(24) NOT NULL DEFAULT 'suggested',
                    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 旧库阶段 helper 只在表存在时补列；revision 表随后补建亦可。
            ensure_ai_profile_entry_columns(cursor)
            ensure_ai_profile_entry_columns(cursor)  # 第二遍：幂等，不报错
            cursor.execute("SHOW COLUMNS FROM `ai_profile_draft_field`")
            existing = {row["Field"] for row in cursor.fetchall()}
            assert ENTRY_COLUMNS <= existing
            cursor.execute(
                "INSERT INTO ai_profile_draft_field (draft_id, field_key, subject) "
                "VALUES ('legacy-1', 'height_cm', 'personal')"
            )
            cursor.execute(
                "SELECT field_kind FROM ai_profile_draft_field WHERE draft_id='legacy-1'"
            )
            assert cursor.fetchone()["field_kind"] == "structured"
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{legacy_db}`")
        conn.close()
