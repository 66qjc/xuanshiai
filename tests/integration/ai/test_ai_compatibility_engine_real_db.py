"""WP-C1a：ai_compatibility_snapshot 的 engine/brand_label 列真实库契约。

覆盖：全新库自带两列、旧库 ensure 幂等补列、默认 rule 写路径字节级不变、
llm 形参路径（engine/brand_label/自定义 TTL）落库并经读取模型透传。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

import pymysql
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ai_schema import ensure_ai_compatibility_engine_columns
from app.schemas.ai_compatibility import CompatibilitySnapshotStatus
from app.services.ai.compatibility import (
    BRAND_LABEL,
    CompatibilityResult,
    RevisionVector,
    write_shadow_snapshot,
)
from tests.integration.ai.conftest import TEST_DATABASE_NAME, TEST_DATABASE_URL

ENGINE_COLUMNS = {"engine", "brand_label"}

_VIEWER_ID = 9_876_548_101
_TARGET_ID = _VIEWER_ID + 1


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
async def test_real_engine_columns_present(real_db_session: AsyncSession) -> None:
    missing = ENGINE_COLUMNS - await _columns(real_db_session, "ai_compatibility_snapshot")
    assert not missing, f"engine columns missing: {missing}"


@pytest.mark.asyncio
async def test_real_ensure_engine_columns_idempotent_on_legacy_database() -> None:
    parts = urlsplit(TEST_DATABASE_URL)
    legacy_db = f"xuanshiai_ai_engine_legacy_{__import__('uuid').uuid4().hex[:8]}"
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
                CREATE TABLE `ai_compatibility_snapshot` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `snapshot_id` varchar(64) NOT NULL,
                    `viewer_user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `algorithm_version` varchar(32) NOT NULL DEFAULT 'compatibility-rule-v1',
                    `snapshot_hash` char(64) NOT NULL,
                    `status` varchar(24) NOT NULL DEFAULT 'ready',
                    PRIMARY KEY (`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            ensure_ai_compatibility_engine_columns(cursor)
            ensure_ai_compatibility_engine_columns(cursor)  # 第二遍：幂等
            cursor.execute("SHOW COLUMNS FROM `ai_compatibility_snapshot`")
            existing = {row["Field"] for row in cursor.fetchall()}
            assert ENGINE_COLUMNS <= existing
            cursor.execute(
                "SELECT engine FROM ai_compatibility_snapshot LIMIT 1"
            )
            # 空表即可：默认值语义由 DDL DEFAULT 'rule-v1' 保证。
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{legacy_db}`")
        conn.close()


def _ready_result() -> CompatibilityResult:
    return CompatibilityResult.ready(
        pair_score=76.0,
        directions=(82.0, 71.0),
        coverage=0.8,
        reason_codes=("AGE_MUTUAL_WITHIN_RANGE",),
    )


def _revisions() -> tuple[RevisionVector, RevisionVector]:
    vector = {"profile": 1, "preference": 1, "privacy": 1, "relationship": 0, "policy": 1}
    return (
        RevisionVector(**vector),
        RevisionVector(**{**vector, "profile": 2}),
    )


def _consent() -> dict:
    return {
        "viewer": {
            "scope": "compatibility_shadow",
            "version": "compatibility-shadow-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "granted_at": datetime.now(UTC).isoformat(),
        },
        "target": {
            "scope": "compatibility_shadow",
            "version": "compatibility-shadow-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "granted_at": datetime.now(UTC).isoformat(),
        },
    }


async def _insert_pair_users(db: AsyncSession) -> None:
    await db.execute(
        text(
            "INSERT INTO ai_compatibility_snapshot "
            "(snapshot_id, viewer_user_id, target_user_id, snapshot_hash) "
            "VALUES ('it-engine-seed', :viewer, :target, :viewer)"
        ),
        {"viewer": _VIEWER_ID, "target": _TARGET_ID},
    )
    await db.execute(
        text("DELETE FROM ai_compatibility_snapshot WHERE snapshot_id = 'it-engine-seed'")
    )


@pytest.mark.asyncio
async def test_real_write_shadow_default_rule_engine_unchanged(
    real_db_session: AsyncSession,
) -> None:
    """默认形参：engine='rule-v1'、brand_label NULL、TTL 走规则配置——行为与改前一致。"""
    result = _ready_result()
    snapshot_id = await write_shadow_snapshot(
        real_db_session,
        _VIEWER_ID,
        _TARGET_ID,
        result,
        _revisions(),
        _consent(),
    )
    await real_db_session.commit()
    row = (
        await real_db_session.execute(
            text(
                "SELECT engine, brand_label, score_semantics, algorithm_version, "
                "TIMESTAMPDIFF(MINUTE, calculated_at, expires_at) AS ttl_minutes "
                "FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    ).mappings().one()
    assert row["engine"] == "rule-v1"
    assert row["brand_label"] is None
    assert row["score_semantics"] == "rule_based_reference_shadow"
    assert row["algorithm_version"] == "compatibility-rule-v1"
    assert row["ttl_minutes"] is not None and int(row["ttl_minutes"]) >= 9
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_write_shadow_llm_engine_and_brand_label(
    real_db_session: AsyncSession,
) -> None:
    result = _ready_result()
    snapshot_id = await write_shadow_snapshot(
        real_db_session,
        _VIEWER_ID,
        _TARGET_ID,
        result,
        _revisions(),
        _consent(),
        engine="llm-v1",
        brand_label=BRAND_LABEL,
        score_semantics="llm_pairwise_probability",
        ttl_minutes=7 * 24 * 60,
    )
    await real_db_session.commit()
    row = (
        await real_db_session.execute(
            text(
                "SELECT engine, brand_label, score_semantics, compatibility_index, "
                "TIMESTAMPDIFF(MINUTE, calculated_at, expires_at) AS ttl_minutes "
                "FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    ).mappings().one()
    assert row["engine"] == "llm-v1"
    assert row["brand_label"] == "来自良配Ai算法"
    assert row["score_semantics"] == "llm_pairwise_probability"
    assert float(row["compatibility_index"]) == 76.0
    assert 7 * 24 * 60 - 2 <= int(row["ttl_minutes"]) <= 7 * 24 * 60
    await real_db_session.rollback()
