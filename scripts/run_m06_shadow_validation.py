"""M06 shadow 真实 DeepSeek 端到端验证脚本。

在真实 Docker DB（compose.ai-test.yml MySQL/Redis）上用真实 DeepSeek
provider 跑通 M04 画像抽取→发布→投影→M06 shadow 计算全链路，收集 D3
外显前提要求的 shadow 验证指标。

用法：
    cd xuanshiai-backend
    python scripts/run_m06_shadow_validation.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime

# 确保 app 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.compatibility import (
    CompatibilityConsentRequired,
    compute_and_write_shadow,
    read_compatibility_snapshot,
)
from app.services.ai.consents import grant_consent, list_consents
from app.services.ai.profile import (
    confirm_profile_draft,
    create_profile_session,
    publish_profile_draft,
    submit_profile_turn,
)
from app.services.ai.tasks import claim_tasks
from app.services.revisions import RevisionVector
from app.workers.ai_worker import _process

# ── 配置 ──────────────────────────────────────────────────────────
TEST_DATABASE_URL = os.getenv(
    "AI_TEST_DATABASE_URL", "mysql+aiomysql://root:@127.0.0.1:3307/xuanshiai_ai_test"
)
TEST_REDIS_URL = os.getenv("AI_TEST_REDIS_URL", "redis://127.0.0.1:6380/5")
POLICY_REVISION = "ai-policy-2026-08-07-v1"
PROFILE_SCOPE = ("profile_text_extract", "profile-text-v1")
COMPAT_SCOPE = ("compatibility_shadow", "compatibility-shadow-v1")

# 测试用户 id 段（与 conftest sweep 范围一致）
RUN_TS = int(time.time()) % 100000
USER_A = 9_876_548_000 + RUN_TS
USER_B = USER_A + 1

# ── 真实自然语言画像文本（非 Mock 固定文本）────────────────────────
# 覆盖全部 8 个 compatibility 维度：age/city_code/marriage_status/
# education_level/height_cm/income_band/interest_tags/relationship_goal
PERSONAL_TEXTS = {
    USER_A: (
        "我今年28岁，住在杭州，本科毕业，在互联网公司做产品经理，月收入大概一万五。"
        "身高172，喜欢旅行和摄影，性格比较外向。"
        "目前未婚单身，希望能认真谈恋爱找对象结婚。"
    ),
    USER_B: (
        "我今年26岁，也住在杭州，本科毕业，在一家设计公司做UI设计师，月收入一万二。"
        "身高165，喜欢旅行和看展，性格偏文静。"
        "目前未婚单身，想找一个合适的人认真交往以结婚为目标。"
    ),
}

IDEAL_TEXTS = {
    USER_A: (
        "我希望对方年龄在24到30岁之间，身高160以上，本科学历，"
        "住在杭州或者周边城市，性格温柔，有稳定工作，月收入八千以上。"
        "对方应该是未婚，也想认真交往以结婚为目的。"
    ),
    USER_B: (
        "我希望对方年龄在27到32岁之间，身高170以上，本科及以上学历，"
        "在互联网或科技行业工作，月收入一万以上，有上进心。"
        "对方应该未婚，婚恋目标是认真谈恋爱结婚。"
    ),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


# ── DB 编排（照搬 test_ai_trilogy_e2e 模式）──────────────────────
async def _cleanup_pair(db: AsyncSession) -> None:
    for stmt in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_search_snapshot WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_search_draft WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_profile_draft WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_turn WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_session WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_summary WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN (SELECT id FROM ai_profile_revision WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_profile_revision WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id IN (:a, :b) OR target_user_id IN (:a, :b)",
        "DELETE FROM ai_feature_projection WHERE subject_user_id IN (:a, :b)",
        "DELETE FROM ai_task WHERE owner_user_id IN (:a, :b)",
        "DELETE FROM ai_consent_operation WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:a, :b)",
        "DELETE FROM derivation_outbox WHERE aggregate_id IN (:a, :b)",
        "DELETE r FROM derivation_consumer_receipt r JOIN derivation_outbox o ON o.event_id = r.event_id WHERE o.aggregate_id IN (:a, :b)",
        "DELETE FROM user_block WHERE user_id IN (:a, :b) OR target_user_id IN (:a, :b)",
        "DELETE FROM user_revision_state WHERE user_id IN (:a, :b)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:a, :b)",
        "DELETE FROM user_privacy WHERE user_id IN (:a, :b)",
        "DELETE FROM user_auth WHERE user_id IN (:a, :b)",
        "DELETE FROM user_profile WHERE user_id IN (:a, :b)",
        "DELETE FROM users WHERE id IN (:a, :b)",
    ):
        await db.execute(text(stmt), {"a": USER_A, "b": USER_B})
    await db.commit()


async def _seed_users(db: AsyncSession) -> None:
    now = _now()
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:id, :nickname, :gender, :birthday, 1, 1)"
        ),
        [
            {"id": USER_A, "nickname": "shadow-a", "gender": 1, "birthday": "1996-01-01"},
            {"id": USER_B, "nickname": "shadow-b", "gender": 2, "birthday": "1998-01-01"},
        ],
    )
    await db.execute(
        text(
            "INSERT INTO user_profile "
            "(user_id, height, income, occupation, education_level, residence_city_code, "
            "interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 172, 15000, 'technology', 4, '330100', :tags, '[]', :active)"
        ),
        [
            {"user_id": USER_A, "tags": json.dumps(["旅行", "摄影"]), "active": now},
            {"user_id": USER_B, "tags": json.dumps(["旅行", "看展"]), "active": now},
        ],
    )
    for uid in (USER_A, USER_B):
        await db.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:uid, 100)"),
            {"uid": uid},
        )
        await db.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:uid, 2)"),
            {"uid": uid},
        )
        await db.execute(
            text(
                "INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) "
                "VALUES (:uid, 1, 1, 1)"
            ),
            {"uid": uid},
        )
        await db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
                "VALUES (:uid, 0, 0, 0, 0, 0)"
            ),
            {"uid": uid},
        )
    await db.commit()


async def _grant_scopes(db: AsyncSession, user_id: int) -> dict[str, dict[str, str]]:
    privacy_rev = 0
    for scope, version in (PROFILE_SCOPE, COMPAT_SCOPE):
        granted = await grant_consent(
            db, user_id, scope,
            AiConsentGrantRequest(consent_version=version, policy_revision=POLICY_REVISION),
            f"grant-{user_id}-{scope}-{uuid.uuid4().hex[:8]}",
            privacy_rev,
        )
        privacy_rev = granted.privacy_revision
        await db.commit()
    listed = await list_consents(db, user_id)
    result = {
        item.scope: {
            "scope": item.scope, "version": item.version,
            "policy_revision": item.policy_revision,
            "granted_at": item.granted_at.isoformat(),
        }
        for item in listed.consents
    }
    await db.commit()
    return result


async def _run_worker(factory: async_sessionmaker[AsyncSession], worker_id: str, limit: int = 20) -> list[str]:
    async with factory() as claim_db:
        claimed = await claim_tasks(claim_db, worker_id, _now(), limit)
        await claim_db.commit()
    for task in claimed:
        await _process(None, task, worker_id, session_provider=factory)
    return [t.task_id for t in claimed]


async def _run_worker_until_done(
    factory: async_sessionmaker[AsyncSession], user_id: int, subject: str, max_rounds: int = 8
) -> None:
    """反复跑 Worker 轮次，直到该用户的 extract+projection task 全部 succeeded。

    DeepSeek API 偶发 AI_TEMPORARILY_UNAVAILABLE 时 task 进入 retry_wait，
    需要重跑 Worker 轮次才会重试。
    """
    for rnd in range(max_rounds):
        # 递增等待：DeepSeek API 偶发限流，需要更长间隔才恢复
        wait_sec = 3 * (rnd + 1) if rnd > 0 else 0
        await asyncio.sleep(wait_sec)
        await _run_worker(factory, f"worker-r{rnd}-{user_id}-{subject}")
        async with factory() as db:
            pending = (await db.execute(
                text(
                    "SELECT status, error_code FROM ai_task "
                    "WHERE owner_user_id = :uid AND status != 'succeeded' AND status != 'failed' "
                    "ORDER BY id DESC LIMIT 5"
                ),
                {"uid": user_id},
            )).mappings().all()
        if not pending:
            return
        for p in pending:
            print(f"    [轮次 {rnd}] 待重试 task: status={p['status']} err={p['error_code']}")
    print(f"  警告: {user_id}/{subject} 经过 {max_rounds} 轮 Worker 仍有未完成 task")


async def _publish_with_real_deepseek(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    subject: ProfileSubject,
    real_text: str,
) -> None:
    """用真实 DeepSeek 抽取自然语言文本 → 确认草稿 → 发布 → 触发投影。"""
    async with factory() as db:
        session = await create_profile_session(
            db, user_id, subject, PROFILE_SCOPE[1],
            f"session-{user_id}-{subject.value}-{uuid.uuid4().hex[:8]}",
        )
        accepted = await submit_profile_turn(
            db, session.session_id, user_id,
            f"turn-{user_id}-{subject.value}-{uuid.uuid4().hex[:8]}",
            real_text,
            f"extract-{user_id}-{subject.value}-{uuid.uuid4().hex[:8]}",
        )
        assert accepted.task_id, f"submit_profile_turn 未产生 task_id (user={user_id})"
        await db.commit()

    # Worker 执行 DeepSeek 抽取（含重试轮次，处理偶发 API 不可用）
    await _run_worker_until_done(factory, user_id, subject.value)

    async with factory() as db:
        draft_id = await db.scalar(
            text(
                "SELECT draft_id FROM ai_profile_draft "
                "WHERE user_id = :uid AND subject = :subj ORDER BY created_at DESC LIMIT 1"
            ),
            {"uid": user_id, "subj": subject.value},
        )
        assert draft_id, f"未找到 draft (user={user_id}, subject={subject.value})"

        expected_rev = int((await db.execute(
            text("SELECT expected_revision FROM ai_profile_draft WHERE draft_id = :did"),
            {"did": draft_id},
        )).scalar_one())

        field_keys = (await db.execute(
            text("SELECT field_key FROM ai_profile_draft_field WHERE draft_id = :did ORDER BY field_key"),
            {"did": draft_id},
        )).scalars().all()
        assert field_keys, f"DeepSeek 未抽取到任何字段 (user={user_id}, subject={subject.value})"
        print(f"  [{user_id}/{subject.value}] DeepSeek 抽取字段: {list(field_keys)}")

        confirmed = await confirm_profile_draft(
            db, str(draft_id), user_id,
            [ProfileDraftFieldPatchRequest(
                field_key=str(fk), action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=expected_rev,
            ) for fk in field_keys],
            expected_revision=expected_rev,
            idempotency_key=f"confirm-{user_id}-{subject.value}-{uuid.uuid4().hex[:8]}",
        )
        published = await publish_profile_draft(
            db, str(draft_id), user_id,
            expected_revision=confirmed.revision,
            idempotency_key=f"publish-{user_id}-{subject.value}-{uuid.uuid4().hex[:8]}",
        )
        assert published.task_id
        await db.commit()

    # Worker 执行投影重建（含重试）
    await _run_worker_until_done(factory, user_id, subject.value)


async def _get_revisions(db: AsyncSession, user_id: int) -> RevisionVector:
    row = (await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision "
            "FROM user_revision_state WHERE user_id = :uid"
        ),
        {"uid": user_id},
    )).mappings().one()
    return RevisionVector(
        profile=int(row["profile_revision"] or 0),
        preference=int(row["preference_revision"] or 0),
        privacy=int(row["privacy_revision"] or 0),
    )


# ── 主流程 ────────────────────────────────────────────────────────
async def main() -> None:
    # 设置环境变量，指向测试 DB
    os.environ.update({
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "ENVIRONMENT": "testing",
        "AUTO_INIT_DB": "false",
        "AI_MASTER_ENABLED": "true",
        "AI_PROFILE_ENABLED": "true",
        "AI_SEARCH_ENABLED": "true",
        "AI_COMPATIBILITY_SHADOW_ENABLED": "true",
    })
    # 同步 settings 单例
    settings.database_url = TEST_DATABASE_URL
    settings.redis_url = TEST_REDIS_URL
    settings.environment = "testing"
    settings.ai_master_enabled = True
    settings.ai_profile_enabled = True
    settings.ai_search_enabled = True
    settings.ai_compatibility_shadow_enabled = True

    print(f"=== M06 Shadow 真实 DeepSeek 验证 ===")
    print(f"provider={settings.ai_provider} model={settings.ai_deepseek_model}")
    print(f"USER_A={USER_A} USER_B={USER_B}")
    print()

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        # 1. 清理 + 种子
        async with factory() as db:
            await _cleanup_pair(db)
            await _seed_users(db)
            consent_a = await _grant_scopes(db, USER_A)
            consent_b = await _grant_scopes(db, USER_B)
            print(f"consent granted: A={list(consent_a.keys())} B={list(consent_b.keys())}")

        # 2. DeepSeek 真实抽取画像（personal + ideal_partner 双方各两轮）
        print("\n--- M04 DeepSeek 真实画像抽取 ---")
        t0 = time.time()
        for uid, ptext, itext in [
            (USER_A, PERSONAL_TEXTS[USER_A], IDEAL_TEXTS[USER_A]),
            (USER_B, PERSONAL_TEXTS[USER_B], IDEAL_TEXTS[USER_B]),
        ]:
            await _publish_with_real_deepseek(factory, uid, ProfileSubject.PERSONAL, ptext)
            await _publish_with_real_deepseek(factory, uid, ProfileSubject.IDEAL_PARTNER, itext)
        print(f"画像抽取+发布+投影完成，耗时 {time.time()-t0:.1f}s")

        # 3. 读 revision pair
        async with factory() as db:
            rev_a = await _get_revisions(db, USER_A)
            rev_b = await _get_revisions(db, USER_B)
            print(f"\nrevision: A={rev_a.as_dict()} B={rev_b.as_dict()}")

        # 4. M06 shadow 计算（双向）
        # consent 须为 {"viewer": <compat_shadow consent>, "target": <compat_shadow consent>}
        # _pair_consents_current 用 _consent_snapshot 匹配 scope/version/policy_revision/granted_at
        consent_pair = {
            "viewer": consent_a.get("compatibility_shadow", {}),
            "target": consent_b.get("compatibility_shadow", {}),
        }
        print(f"\nconsent_pair viewer={bool(consent_pair['viewer'])} target={bool(consent_pair['target'])}")
        print("\n--- M06 Shadow 计算 ---")
        shadow_results = []
        for viewer, target in [(USER_A, USER_B), (USER_B, USER_A)]:
            async with factory() as db:
                viewer_rev = await _get_revisions(db, viewer)
                target_rev = await _get_revisions(db, target)
                # 双向 viewer/target 对调
                pair = {
                    "viewer": (consent_a if viewer == USER_A else consent_b).get("compatibility_shadow", {}),
                    "target": (consent_b if viewer == USER_A else consent_a).get("compatibility_shadow", {}),
                }
                t1 = time.time()
                snapshot_id = await compute_and_write_shadow(
                    db, viewer, target,
                    (viewer_rev, target_rev),
                    pair,
                )
                await db.commit()
                elapsed = time.time() - t1
                shadow_results.append({
                    "viewer": viewer, "target": target,
                    "snapshot_id": snapshot_id, "elapsed": elapsed,
                })
                print(f"  {viewer}→{target}: snapshot={snapshot_id} ({elapsed:.2f}s)")

        # 5. 读回 shadow 快照，收集指标
        print("\n--- Shadow 指标收集 ---")
        metrics = {
            "provider": settings.ai_provider,
            "model": settings.ai_deepseek_model,
            "pairs": [],
        }
        for sr in shadow_results:
            async with factory() as db:
                row = (await db.execute(
                    text(
                        "SELECT snapshot_id, status, compatibility_index, coverage, "
                        "direction_json, reason_codes, display_eligible, "
                        "experiment_bucket, score_semantics, algorithm_version, "
                        "expires_at, calculated_at "
                        "FROM ai_compatibility_snapshot "
                        "WHERE viewer_user_id = :v AND target_user_id = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"v": sr["viewer"], "t": sr["target"]},
                )).mappings().one()

                direction = json.loads(row["direction_json"]) if row["direction_json"] else None
                reasons = json.loads(row["reason_codes"]) if row["reason_codes"] else []
                pair_metric = {
                    "viewer": sr["viewer"], "target": sr["target"],
                    "status": row["status"],
                    "compatibility_index": float(row["compatibility_index"]) if row["compatibility_index"] else None,
                    "coverage": float(row["coverage"]),
                    "directions": direction,
                    "reason_codes": reasons,
                    "display_eligible": int(row["display_eligible"]),
                    "experiment_bucket": row["experiment_bucket"],
                    "score_semantics": row["score_semantics"],
                    "algorithm_version": row["algorithm_version"],
                }
                metrics["pairs"].append(pair_metric)
                print(f"  {sr['viewer']}→{sr['target']}:")
                print(f"    status={row['status']} score={pair_metric['compatibility_index']} coverage={pair_metric['coverage']}")
                print(f"    directions={direction} reasons={reasons}")
                print(f"    display_eligible={row['display_eligible']} bucket={row['experiment_bucket']}")

        # 6. 验证门禁断言（D3 前提检查）
        print("\n--- D3 前提门禁断言 ---")
        all_pass = True

        # display_eligible 必须全为 0（shadow 阶段不外显）
        leak = [p for p in metrics["pairs"] if p["display_eligible"] != 0]
        print(f"display_eligible_leak: {'PASS (全部=0)' if not leak else f'FAIL ({len(leak)} 条泄漏)'}")
        all_pass = all_pass and not leak

        # experiment_bucket 必须全为 shadow
        bad_bucket = [p for p in metrics["pairs"] if p["experiment_bucket"] != "shadow"]
        print(f"experiment_bucket=shadow: {'PASS' if not bad_bucket else f'FAIL ({len(bad_bucket)} 条不符)'}")
        all_pass = all_pass and not bad_bucket

        # algorithm_version 必须为 compatibility-rule-v1
        bad_ver = [p for p in metrics["pairs"] if p["algorithm_version"] != "compatibility-rule-v1"]
        print(f"algorithm_version=compatibility-rule-v1: {'PASS' if not bad_ver else f'FAIL ({len(bad_ver)} 条不符)'}")
        all_pass = all_pass and not bad_ver

        # coverage 门禁：双方方向都应 ≥ 0.50（否则 coverage_insufficient）
        cov_ok = all(p["coverage"] >= 0.50 for p in metrics["pairs"])
        cov_detail = [f"{p['viewer']}→{p['target']}={p['coverage']}" for p in metrics["pairs"]]
        print(f"coverage≥0.50: {'PASS' if cov_ok else 'FAIL'} ({', '.join(cov_detail)})")
        all_pass = all_pass and cov_ok

        # status 不应为 coverage_insufficient
        bad_status = [p for p in metrics["pairs"] if p["status"] == "coverage_insufficient"]
        print(f"status≠coverage_insufficient: {'PASS' if not bad_status else f'FAIL ({len(bad_status)} 条不足)'}")
        all_pass = all_pass and not bad_status

        # legacy match_score 未被破坏：ai_compatibility_snapshot 中不应有
        # legacy-rule-v1 混入（shadow 写入只写 compatibility-rule-v1）
        async with factory() as db:
            legacy_rows = (await db.execute(
                text(
                    "SELECT algorithm_version, COUNT(*) as cnt FROM ai_compatibility_snapshot "
                    "WHERE viewer_user_id IN (:a, :b) OR target_user_id IN (:a, :b) "
                    "GROUP BY algorithm_version"
                ),
                {"a": USER_A, "b": USER_B},
            )).mappings().all()
            legacy_versions = {r["algorithm_version"] for r in legacy_rows}
            legacy_untouched = "legacy-rule-v1" not in legacy_versions
            print(f"legacy_rule_not_contaminated: {'PASS' if legacy_untouched else 'FAIL'} (versions={legacy_versions})")
            all_pass = all_pass and legacy_untouched

        # consent 门禁：无 consent 时应 403
        print("\n--- Consent 门禁（无 consent 时返回 403）---")
        try:
            async with factory() as db:
                # 传空 consent dict
                await compute_and_write_shadow(
                    db, USER_A, USER_B,
                    (rev_a, rev_b),
                    {},
                )
            print("consent_required_403: FAIL (无 consent 却未抛异常)")
            all_pass = False
        except CompatibilityConsentRequired:
            print("consent_required_403: PASS (抛出 CompatibilityConsentRequired)")
        except Exception as e:
            print(f"consent_required_403: FAIL (异常类型不符: {type(e).__name__}: {e})")
            all_pass = False

        # 7. 总结
        print(f"\n{'='*60}")
        print(f"M06 Shadow 真实 DeepSeek 验证结论: {'PASS' if all_pass else 'FAIL'}")
        print(f"{'='*60}")
        print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))

    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
