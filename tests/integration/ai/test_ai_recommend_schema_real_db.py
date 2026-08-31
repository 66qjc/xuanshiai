"""三类推荐快照表（WP-P6a）真实库 schema 契约。

覆盖三类保证：
1. 全新库 bootstrap 后 ``ai_recommendation_snapshot`` 自带 D4 预计算所需列；
2. 视图枚举与默认值（engine='rule-v1'、status='ready'、generation=1）成立；
3. 建表语句幂等：对已存在表重复执行 CREATE TABLE IF NOT EXISTS 不报错。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ai_schema import AI_TABLES
from tests.integration.ai.conftest import TEST_DATABASE_NAME

RECOMMEND_COLUMNS = {
    "snapshot_id",
    "viewer_user_id",
    "view_kind",
    "target_user_id",
    "score",
    "coverage",
    "direction_json",
    "score_detail_json",
    "reason_codes",
    "rank_no",
    "generation",
    "engine",
    "algorithm_version",
    "source_hash",
    "status",
    "calculated_at",
    "expires_at",
}


async def _columns(db: AsyncSession, table_name: str) -> dict[str, str]:
    result = await db.execute(
        text(
            "SELECT column_name, column_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": TEST_DATABASE_NAME, "table": table_name},
    )
    return {str(row[0]): str(row[1]) for row in result.all()}


@pytest.mark.asyncio
async def test_real_recommend_snapshot_columns_present(
    real_db_session: AsyncSession,
) -> None:
    columns = await _columns(real_db_session, "ai_recommendation_snapshot")
    missing = RECOMMEND_COLUMNS - set(columns)
    assert not missing, f"recommendation snapshot columns missing: {missing}"


@pytest.mark.asyncio
async def test_real_recommend_snapshot_view_kind_enum_and_defaults(
    real_db_session: AsyncSession,
) -> None:
    columns = await _columns(real_db_session, "ai_recommendation_snapshot")
    assert "enum" in columns["view_kind"], columns["view_kind"]
    assert "i_like" in columns["view_kind"] and "similar" in columns["view_kind"]
    marker = 987654321
    await real_db_session.execute(
        text(
            "INSERT INTO ai_recommendation_snapshot "
            "(snapshot_id, viewer_user_id, view_kind, target_user_id, rank_no, "
            " source_hash) "
            "VALUES ('it-schema-1', :viewer, 'i_like', :target, 1, :source_hash)"
        ),
        {"viewer": marker, "target": marker + 1, "source_hash": "0" * 64},
    )
    row = (
        await real_db_session.execute(
            text(
                "SELECT engine, status, generation, algorithm_version "
                "FROM ai_recommendation_snapshot "
                "WHERE viewer_user_id = :viewer AND view_kind = 'i_like' "
                "AND target_user_id = :target"
            ),
            {"viewer": marker, "target": marker + 1},
        )
    ).one()
    assert row[0] == "rule-v1"
    assert row[1] == "ready"
    assert row[2] == 1
    assert row[3] == "recommend-rule-v1"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_recommend_snapshot_create_table_idempotent(
    real_db_session: AsyncSession,
) -> None:
    """对已存在的表重复执行建表语句不报错（幂等，与 bootstrap 行为一致）。"""
    await real_db_session.execute(text(AI_TABLES["ai_recommendation_snapshot"]))
