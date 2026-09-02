"""三类推荐（WP-P6）：打分核心 + 候选池 + 快照物化 + worker 任务。

量纲与 compatibility 引擎对齐：score 0..100、coverage 0..1（D4 预计算，
打分不实时调 LLM；llm 双向分的消费见 materialize 的 engine 标记）。
打分纯函数不触 DB；DB 区函数遵循服务层"不 commit"契约。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.candidate_visibility import VisibilityScene

logger = logging.getLogger(__name__)


def _compat():
    """惰性获取 compatibility 模块（含其私有辅助件）。

    ai_worker 初始化尾部会导入本模块（注册 handler），而 compatibility 的
    模块级自注册又会触发 ai_worker 初始化——若本模块在头部导入
    compatibility 会成三方循环导入。compatibility 的行为由其自身测试冻结，
    本模块调用时再取，不复制实现以免口径漂移。
    """
    from app.services.ai import compatibility as _compatibility

    return _compatibility


@dataclass(frozen=True)
class RecommendationScore:
    """单张推荐卡片的打分结果；``score is None`` 表示不可算（排除出列表）。

    ``reason_texts``：llm 双向分附带的中文理由（rule 引擎为空）。
    """

    score: float | None
    coverage: float
    reason_codes: tuple[str, ...]
    score_detail: dict | None = None
    reason_texts: tuple[str, ...] = ()


def _positive_hits(
    preference: dict, profile: dict, rules=None
) -> tuple[list[str], list[str]]:
    """返回（正向命中码, 未知维度码）：正向码复用 compatibility 展示语义。"""
    compat = _compat()
    rules = rules or compat.COMPATIBILITY_RULES
    positive: list[str] = []
    unknown: list[str] = []
    for dimension in rules.dimensions:
        pref = preference.get(dimension.key)
        value = profile.get(dimension.key)
        if pref is None or value is None:
            unknown.append("DIMENSION_UNKNOWN")
            continue
        if dimension.score(pref, value) > 0:
            reason = compat._DIMENSION_TO_REASON.get(dimension.key)
            if reason is not None:
                positive.append(reason)
    return positive, unknown


def positive_dimension_codes(preference: dict, profile: dict) -> tuple[str, ...]:
    """单向打分的可展示理由码：仅"双方已知且满足"的维度，缺失记 UNKNOWN。"""
    positive, unknown = _positive_hits(preference, profile)
    return tuple(sorted(set(positive + unknown)))


def _directional_score_card(
    source_preference: dict, target_profile: dict
) -> RecommendationScore:
    """i_like/likes_me 共用：包装 compatibility 引擎的单向打分函数。

    ``directional_score``(:457) 只消费 source.preference × target.profile，
    coverage 低于 ``COVERAGE_THRESHOLD``(0.50) 时 score 置 None——消费端把
    候选排除出列表（方案验收："无投影用户不出现在任何列表"）。
    """
    compat = _compat()
    score, coverage, _ = compat.directional_score(
        compat.FeatureSet(profile={}, preference=source_preference),
        compat.FeatureSet(profile=target_profile, preference={}),
        compat.COMPATIBILITY_RULES,
    )
    if score is None or coverage < compat.COVERAGE_THRESHOLD:
        return RecommendationScore(
            score=None,
            coverage=round(coverage, 4),
            reason_codes=positive_dimension_codes(source_preference, target_profile),
        )
    return RecommendationScore(
        score=round(float(score), 2),
        coverage=round(coverage, 4),
        reason_codes=positive_dimension_codes(source_preference, target_profile),
    )


def score_i_like(viewer_preference: dict, candidate_profile: dict) -> RecommendationScore:
    """"我会喜欢"：我的 ideal_partner 投影 × 候选 personal 投影。"""
    return _directional_score_card(viewer_preference, candidate_profile)


def score_likes_me(candidate_preference: dict, viewer_profile: dict) -> RecommendationScore:
    """"会喜欢我"：反向——候选的 ideal_partner 投影 × 我的 personal 投影。"""
    return _directional_score_card(candidate_preference, viewer_profile)


# ----------------------------------------------------------------------
# similar：双方 personal 投影相似度（分类加权 Jaccard + 数值近邻）
# ----------------------------------------------------------------------

# 权重和恒为 1.0（冻结）：兴趣 0.30、关系期待 0.15、学历/婚姻/城市 0.10、
# 收入 0.05、年龄/身高 0.10。调整权重属算法版本升级，须改版本号。
_SIMILAR_WEIGHTS: dict[str, float] = {
    "interest_tags": 0.30,
    "relationship_goal": 0.15,
    "education_level": 0.10,
    "marriage_status": 0.10,
    "city_code": 0.10,
    "income_band": 0.05,
    "age": 0.10,
    "height_cm": 0.10,
}

_AGE_EXACT_BAND = 3.0    # |Δ|≤3 岁记满分
_AGE_FULL_FALLOFF = 15.0  # |Δ|≥15 岁记 0 分，线性递减
_HEIGHT_EXACT_BAND = 5.0   # |Δ|≤5cm 记满分
_HEIGHT_FULL_FALLOFF = 30.0  # |Δ|≥30cm 记 0 分，线性递减


def _tag_set(value) -> set[str]:
    items = value if isinstance(value, list) else [value]
    return {str(item).strip() for item in items if str(item).strip()}


def _same_value(a, b) -> bool:
    """分类等值：先按数值比（education 编号等），再按字符串比。"""
    compat = _compat()
    num_a, num_b = compat._as_number(a), compat._as_number(b)
    if num_a is not None and num_b is not None:
        return num_a == num_b
    str_a, str_b = compat._as_str(a), compat._as_str(b)
    return str_a is not None and str_a == str_b


def _proximity(a, b, exact: float, falloff: float) -> float | None:
    """数值近邻：exact 带宽内 100 分，线性降到 falloff 处 0 分；外抛越界无分。"""
    num_a, num_b = _compat()._as_number(a), _compat()._as_number(b)
    if num_a is None or num_b is None:
        return None
    delta = abs(num_a - num_b)
    if delta <= exact:
        return 100.0
    if delta >= falloff:
        return 0.0
    return round((falloff - delta) / (falloff - exact) * 100.0, 2)


def similarity_score(profile_a: dict, profile_b: dict) -> RecommendationScore:
    """双方 personal 投影相似度：可用维度加权平均（0..100），Jaccard/等值/近邻。

    coverage = 双方已知维度的权重占比；低于 ``COVERAGE_THRESHOLD`` 不出分。
    """
    available: list[tuple[float, float]] = []
    detail: dict[str, dict] = {}
    for key, weight in _SIMILAR_WEIGHTS.items():
        value_a, value_b = profile_a.get(key), profile_b.get(key)
        if value_a is None or value_b is None:
            continue
        if key == "interest_tags":
            set_a, set_b = _tag_set(value_a), _tag_set(value_b)
            if not set_a or not set_b:
                continue
            union = set_a | set_b
            value = round(len(set_a & set_b) / len(union) * 100.0, 2)
        elif key == "age":
            value = _proximity(value_a, value_b, _AGE_EXACT_BAND, _AGE_FULL_FALLOFF)
        elif key == "height_cm":
            value = _proximity(
                value_a, value_b, _HEIGHT_EXACT_BAND, _HEIGHT_FULL_FALLOFF
            )
        else:
            value = 100.0 if _same_value(value_a, value_b) else 0.0
        if value is None:
            continue
        available.append((weight, value))
        detail[key] = {"weight": weight, "value": value}
    coverage = (
        sum(weight for weight, _ in available) / sum(_SIMILAR_WEIGHTS.values())
        if available
        else 0.0
    )
    if not available or coverage < _compat().COVERAGE_THRESHOLD:
        return RecommendationScore(score=None, coverage=round(coverage, 4), reason_codes=())
    score = sum(weight * value for weight, value in available) / sum(
        weight for weight, _ in available
    )
    reason_codes = tuple(
        sorted(f"SIM_{key.upper()}" for key in detail if detail[key]["value"] > 0)
    )
    return RecommendationScore(
        score=round(float(score), 2),
        coverage=round(coverage, 4),
        reason_codes=reason_codes,
        score_detail=detail,
    )


# ----------------------------------------------------------------------
# 任务与快照物化（WP-P6c）：不 commit，由调用方/worker finalize 控制事务
# ----------------------------------------------------------------------

RECOMMEND_TASK_TYPE = "recommend_rebuild"
RECOMMEND_ALGORITHM_VERSION = "recommend-rule-v1"
RECOMMEND_ENGINE_RULE = "rule-v1"
RECOMMEND_ENGINE_LLM = "llm-v1"
VIEW_KINDS = ("i_like", "likes_me", "similar")

_PROJECTION_KINDS = ("personal_compatibility", "ideal_partner_preference")

_RECOMMEND_INSERT = (
    "INSERT INTO ai_recommendation_snapshot "
    "(snapshot_id, viewer_user_id, view_kind, target_user_id, score, coverage, "
    "direction_json, score_detail_json, reason_codes, rank_no, generation, engine, "
    "algorithm_version, source_hash, status, calculated_at, expires_at, created_at) "
    "VALUES (:snapshot_id, :viewer, :view_kind, :target, :score, :coverage, "
    ":direction_json, :score_detail_json, :reason_codes, :rank_no, :generation, "
    ":engine, :algorithm_version, :source_hash, 'ready', UTC_TIMESTAMP(), "
    ":expires_at, UTC_TIMESTAMP())"
)


def _maybe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def load_candidate_pool(
    db: AsyncSession, viewer_id: int, limit: int
) -> list[dict[str, Any]]:
    """候选池：活跃投影用户（personal 必备，ideal 可选），排除 viewer。

    授权纪律：投影行的 ``consent_snapshot_json.scope`` 必须等于
    ``profile_text_extract``（与 compatibility 投影读取同语义）；同用户多代
    投影取 id 最新一行。LIMIT 按 (用户×2 kinds) 放宽后内存去重截断。
    """
    result = await db.execute(
        text(
            "SELECT p.id, p.subject_user_id, p.projection_kind, p.fields_json, p.source_hash, "
            "p.source_revision_json, p.consent_snapshot_json "
            "FROM ai_feature_projection p "
            "INNER JOIN ai_profile_projection_status ps "
            "  ON ps.user_id = p.subject_user_id AND ps.kind = p.projection_kind "
            "WHERE p.projection_kind IN ('personal_compatibility', 'ideal_partner_preference') "
            "AND p.status = 'active' AND p.subject_user_id <> :viewer "
            "AND (p.expires_at IS NULL OR p.expires_at > UTC_TIMESTAMP()) "
            "AND ps.status = 'active' "
            "ORDER BY p.id DESC LIMIT :limit"
        ),
        {"viewer": int(viewer_id), "limit": int(limit) * 2},
    )
    per_user: dict[int, dict[str, Any]] = {}
    for row in result.mappings().all():
        consent = _maybe_json(row.get("consent_snapshot_json"))
        if not isinstance(consent, dict) or consent.get("scope") != _compat().PROJECTION_CONSENT_SCOPE:
            continue
        user_id = int(row["subject_user_id"])
        entry = per_user.setdefault(
            user_id,
            {
                "user_id": user_id,
                "profile_fields": None,
                "preference_fields": None,
                "source_hash": None,
                "source_revision": None,
            },
        )
        kind = str(row["projection_kind"])
        if kind == "personal_compatibility" and entry["profile_fields"] is None:
            entry["profile_fields"] = _maybe_json(row.get("fields_json")) or {}
            entry["source_hash"] = str(row.get("source_hash") or "")
            entry["source_revision"] = _maybe_json(row.get("source_revision_json")) or {}
        elif kind == "ideal_partner_preference" and entry["preference_fields"] is None:
            entry["preference_fields"] = _maybe_json(row.get("fields_json")) or {}
    return [
        entry
        for entry in per_user.values()
        if entry["profile_fields"] is not None
    ][: int(limit)]


async def load_recommendation_inputs(
    db: AsyncSession, viewer_id: int
) -> dict[str, Any] | None:
    """viewer 双投影输入：``profile``/``preference``/``source_hash``。

    personal 投影缺失返回 None（无画像用户不产生任何推荐）；ideal 缺失仅
    使 i_like/likes_me 无分，similar 仍可算。
    """
    result = await db.execute(
        text(
            "SELECT p.projection_kind, p.fields_json, p.source_hash "
            "FROM ai_feature_projection p "
            "INNER JOIN ai_profile_projection_status ps "
            "  ON ps.user_id = p.subject_user_id AND ps.kind = p.projection_kind "
            "WHERE p.subject_user_id = :viewer "
            "AND p.projection_kind IN ('personal_compatibility', 'ideal_partner_preference') "
            "AND p.status = 'active' "
            "AND (p.expires_at IS NULL OR p.expires_at > UTC_TIMESTAMP()) "
            "AND ps.status = 'active' "
            "ORDER BY p.id DESC"
        ),
        {"viewer": int(viewer_id)},
    )
    inputs: dict[str, Any] = {"profile": None, "preference": None, "source_hash": None}
    for row in result.mappings().all():
        kind = str(row["projection_kind"])
        if kind == "personal_compatibility" and inputs["profile"] is None:
            inputs["profile"] = _maybe_json(row.get("fields_json")) or {}
            inputs["source_hash"] = str(row.get("source_hash") or "")
        elif kind == "ideal_partner_preference" and inputs["preference"] is None:
            inputs["preference"] = _maybe_json(row.get("fields_json")) or {}
    if not inputs["profile"]:
        return None
    return inputs


async def viewer_projection_is_current(db: AsyncSession, viewer_id: int) -> bool:
    """viewer 最新 personal 投影的**内容分量**是否对齐当前版本向量。

    publish→projection→recommend 的落库顺序守卫：投影任务未完成时推荐任务
    必须可重试，绝不拿旧画像打分。只比较 profile/preference 两个分量——
    投影内容仅由这两个 revision 派生；privacy/relationship 分量由拉黑/
    授权事件推进但不会重建投影（内容不变），比较它们会造成永久假阴性
    （推荐对受影响用户静默死亡）。可见性/授权的实时性由候选逐个
    ``candidate_visibility`` 门禁与 handler 内 active consent 校验保证。
    """
    result = await db.execute(
        text(
            "SELECT p.source_revision_json, p.profile_revision, p.preference_revision "
            "FROM ai_feature_projection p "
            "INNER JOIN ai_profile_projection_status ps "
            "  ON ps.user_id = p.subject_user_id AND ps.kind = p.projection_kind "
            "WHERE p.subject_user_id = :viewer "
            "AND p.projection_kind = 'personal_compatibility' AND p.status = 'active' "
            "AND ps.status = 'active' "
            "ORDER BY p.id DESC LIMIT 1"
        ),
        {"viewer": int(viewer_id)},
    )
    row = result.mappings().first()
    if row is None:
        return False
    stored = _maybe_json(row.get("source_revision_json"))
    if not isinstance(stored, dict):
        stored = {
            "profile": int(row.get("profile_revision") or 0),
            "preference": int(row.get("preference_revision") or 0),
        }
    current = (await _compat()._load_revision_vector(db, viewer_id)).as_dict()
    return int(stored.get("profile") or 0) == int(current["profile"]) and int(
        stored.get("preference") or 0
    ) == int(current["preference"])


async def _load_active_consent(db: AsyncSession, user_id: int, scope: str) -> dict | None:
    result = await db.execute(
        text(
            "SELECT user_id, scope, version, policy_revision, granted_at "
            "FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": int(user_id), "scope": scope},
    )
    return result.mappings().first()


async def _visible_pool(
    db: AsyncSession, viewer_id: int, pool: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for candidate in pool:
        decision = await _compat().candidate_visibility_service.decide(
            db, viewer_id, candidate["user_id"], VisibilityScene.PROFILE
        )
        if decision.allowed:
            visible.append(candidate)
    return visible


def _reason_json(codes: tuple[str, ...]) -> str:
    return json.dumps(list(codes), ensure_ascii=False)


async def _load_fresh_llm_directions(
    db: AsyncSession, viewer_id: int, candidate_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """新鲜 llm 匹配度快照（WP-C1）：按 target 取最新一代的双向分与理由。

    消费优先级即"平滑切换"语义：pair 有未过期 ``engine='llm-v1'`` 快照 →
    i_like/likes_me 直接取其双向分；否则回退规则单向打分。similar 纯规则，
    不受影响。
    """
    if not candidate_ids:
        return {}
    compat = _compat()
    engine_llm = compat.ENGINE_LLM
    # IN 列表用 int() 内联（候选 id 全为整型主键），避免动态绑定占位符数量。
    id_list = ",".join(str(int(i)) for i in candidate_ids)
    result = await db.execute(
        text(
            "SELECT target_user_id, coverage, direction_json, profile_revision_pair_json "
            "FROM ai_compatibility_snapshot "
            "WHERE viewer_user_id = :viewer "
            f"AND target_user_id IN ({id_list}) "
            "AND engine = :engine AND status = 'ready' "
            "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP()) "
            "ORDER BY id DESC"
        ),
        {"viewer": int(viewer_id), "engine": engine_llm},
    )
    latest: dict[int, dict[str, Any]] = {}
    for row in result.mappings().all():
        user_id = int(row["target_user_id"])
        if user_id in latest:
            continue
        direction = _maybe_json(row.get("direction_json"))
        if not isinstance(direction, dict):
            logger.debug(
                "recommend llm_direction_malformed viewer=%s target=%s",
                viewer_id,
                user_id,
            )
            continue
        stored_pair = _maybe_json(row.get("profile_revision_pair_json")) or {}
        latest[user_id] = {
            "coverage": (
                float(row["coverage"]) if row.get("coverage") is not None else None
            ),
            "direction": direction,
            "target_profile_revision": stored_pair.get("target"),
        }
    return latest


def _direction_card(
    direction: dict[str, Any], coverage: float | None
) -> RecommendationScore | None:
    """llm 方向明细 → 推荐打分卡（score 0..100 原值 + 中文理由）。"""
    try:
        score = float(direction.get("score"))
    except (TypeError, ValueError):
        return None
    reasons = direction.get("reasons")
    reason_texts = tuple(
        str(r) for r in reasons if isinstance(r, str) and r.strip()
    ) if isinstance(reasons, list) else ()
    return RecommendationScore(
        score=round(score, 2),
        coverage=coverage if coverage is not None else 0.0,
        reason_codes=(),
        reason_texts=reason_texts,
    )


async def materialize_recommendations(
    db: AsyncSession, viewer_id: int, trigger: str = "task"
) -> str:
    """三视图打分 → top-N 物化为新 generation；返回批次 snapshot_id（空串=无输入）。

    WP-C3 第一步：每视图按 score 降序 ``rank_no`` 1 起连续（契合度高者靠前）；
    主 discovery 名片流不动（D5）。物化失败由外层事务回滚，不产生半代数据。
    """
    inputs = await load_recommendation_inputs(db, viewer_id)
    if inputs is None:
        return ""
    pool = await _visible_pool(
        db,
        viewer_id,
        await load_candidate_pool(db, viewer_id, settings.ai_recommendation_pool_limit),
    )
    # snapshot_id 仅在确实写入行时返回：空池/全不可算返回 ""——调用方据此
    # 完成"诚实空结果"而不是留下一个库里不存在的幽灵快照 id。
    snapshot_id = f"rc_{uuid.uuid4().hex}"
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
        minutes=settings.ai_recommendation_ttl_minutes
    )
    llm_map = await _load_fresh_llm_directions(
        db, viewer_id, [candidate["user_id"] for candidate in pool]
    )

    def _llm_card(candidate: dict[str, Any], key: str) -> RecommendationScore | None:
        """llm 双向分卡；候选内容版本已前进（republish）则弃用旧 llm 行。"""
        llm = llm_map.get(candidate["user_id"])
        if not llm or not isinstance(llm["direction"].get(key), dict):
            return None
        stored_pair = llm.get("target_profile_revision")
        candidate_rev = candidate.get("source_revision") or {}
        if isinstance(stored_pair, dict) and candidate_rev:
            if (
                int(stored_pair.get("profile") or -1)
                != int(candidate_rev.get("profile") or -2)
                or int(stored_pair.get("preference") or -1)
                != int(candidate_rev.get("preference") or -2)
            ):
                logger.debug(
                    "recommend llm_direction_stale_pair viewer=%s target=%s",
                    viewer_id,
                    candidate["user_id"],
                )
                return None
        return _direction_card(llm["direction"][key], llm["coverage"])

    def _i_like_card(candidate: dict[str, Any]) -> tuple[RecommendationScore, str]:
        card = _llm_card(candidate, "viewer_to_target")
        if card is not None:
            return card, RECOMMEND_ENGINE_LLM
        return score_i_like(inputs["preference"] or {}, candidate["profile_fields"]), RECOMMEND_ENGINE_RULE

    def _likes_me_card(candidate: dict[str, Any]) -> tuple[RecommendationScore, str]:
        card = _llm_card(candidate, "target_to_viewer")
        if card is not None:
            return card, RECOMMEND_ENGINE_LLM
        return (
            score_likes_me(candidate["preference_fields"] or {}, inputs["profile"]),
            RECOMMEND_ENGINE_RULE,
        )

    # (candidate, card, engine) 三元组：i_like/likes_me 优先消费 llm 双向分（T10），
    # similar 恒为规则相似度。
    plans: dict[str, list[tuple[dict[str, Any], RecommendationScore, str]]] = {
        "i_like": [
            (candidate, *_i_like_card(candidate))
            for candidate in pool
        ]
        if inputs["preference"]
        else [],
        "likes_me": [
            (candidate, *_likes_me_card(candidate))
            for candidate in pool
        ],
        "similar": [
            (candidate, similarity_score(inputs["profile"], candidate["profile_fields"]), RECOMMEND_ENGINE_RULE)
            for candidate in pool
        ],
    }
    for view_kind, cards in plans.items():
        scored = sorted(
            (item for item in cards if item[1].score is not None),
            key=lambda item: (-item[1].score, item[0]["user_id"]),
        )
        if not scored:
            continue
        generation_row = (
            await db.execute(
                text(
                    "SELECT COALESCE(MAX(generation), 0) + 1 "
                    "FROM ai_recommendation_snapshot "
                    "WHERE viewer_user_id = :viewer AND view_kind = :view_kind"
                ),
                {"viewer": int(viewer_id), "view_kind": view_kind},
            )
        ).scalar_one()
        generation = int(generation_row)
        for rank_no, (candidate, card, engine) in enumerate(
            scored[: settings.ai_recommendation_top_n], start=1
        ):
            direction_json = None
            score_detail_json = None
            if view_kind in ("i_like", "likes_me"):
                direction_json = json.dumps(
                    {
                        "score": card.score,
                        "reason_texts": list(card.reason_texts),
                    },
                    ensure_ascii=False,
                )
            else:
                score_detail_json = (
                    json.dumps(card.score_detail, ensure_ascii=False)
                    if card.score_detail is not None
                    else None
                )
            await db.execute(
                text(_RECOMMEND_INSERT),
                {
                    "snapshot_id": snapshot_id,
                    "viewer": int(viewer_id),
                    "view_kind": view_kind,
                    "target": candidate["user_id"],
                    "score": card.score,
                    "coverage": card.coverage,
                    "direction_json": direction_json,
                    "score_detail_json": score_detail_json,
                    "reason_codes": _reason_json(card.reason_codes),
                    "rank_no": rank_no,
                    "generation": generation,
                    "engine": engine,
                    "algorithm_version": RECOMMEND_ALGORITHM_VERSION,
                    "source_hash": inputs["source_hash"] or "",
                    "expires_at": expires_at,
                },
            )
        await db.execute(
            text(
                "UPDATE ai_recommendation_snapshot SET status = 'superseded', "
                "updated_at = UTC_TIMESTAMP() "
                "WHERE viewer_user_id = :viewer AND view_kind = :view_kind "
                "AND generation < :generation AND status = 'ready'"
            ),
            {
                "viewer": int(viewer_id),
                "view_kind": view_kind,
                "generation": generation,
            },
        )
    await db.flush()
    return snapshot_id


async def _recommend_wrote_rows(db: AsyncSession, snapshot_id: str) -> bool:
    """批次是否真的写入了行（防幽灵 snapshot_id；同 session 内可见未提交行）。"""
    result = await db.execute(
        text(
            "SELECT 1 FROM ai_recommendation_snapshot "
            "WHERE snapshot_id = :snapshot_id LIMIT 1"
        ),
        {"snapshot_id": snapshot_id},
    )
    return result.first() is not None


async def recommend_rebuild_handler(
    db: AsyncSession, task, worker_id: str
) -> tuple[str, Any] | None:
    """``recommend_rebuild`` Worker handler：授权/投影新鲜度门禁 → 物化。

    门禁顺序仿 ``compatibility_execute_handler``：无 profile_text_extract
    授权 → 不可重试；投影版本落后（projection 任务尚未完成）→ 可重试，
    等下一次调度（publish/GET miss/每日批量均有入队兜底）。
    """
    from app.services.ai.tasks import fail_task

    viewer_id = int(task.owner_user_id)
    consent = await _load_active_consent(db, viewer_id, _compat().PROJECTION_CONSENT_SCOPE)
    if consent is None:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_CONSENT_REQUIRED", retryable=False,
        )
        return None
    if not await viewer_projection_is_current(db, viewer_id):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_TEMPORARILY_UNAVAILABLE", retryable=True,
        )
        return None
    snapshot_id = await materialize_recommendations(db, viewer_id, trigger="task")
    if not snapshot_id or not await _recommend_wrote_rows(db, snapshot_id):
        # 门禁已过（有授权、有投影）："" = 候选池为空或全部不可算——这是合法
        # 的空结果而非错误。完成任务并给出诚实 result_ref，读取端随即可返回
        # 真实空列表（regenerating=false），不会整天停留在"生成中"。
        return (
            "recommend-empty",
            await _compat()._load_revision_vector(db, viewer_id),
        )
    return (
        f"recommend-snapshot:{snapshot_id}",
        await _compat()._load_revision_vector(db, viewer_id),
    )


def register_recommend_handlers() -> None:
    """把 ``recommend_rebuild`` 注册进 AI Worker 的 TASK_HANDLERS（幂等）。

    权威注册路径是 ``ai_worker.register_business_handlers``（standalone worker
    唯一可靠入口）；此处为路由/测试导入路径的幂等兜底，容错部分初始化。
    """
    from app.workers import ai_worker as worker_module

    handlers = getattr(worker_module, "TASK_HANDLERS", None)
    if handlers is not None:
        handlers.setdefault(RECOMMEND_TASK_TYPE, recommend_rebuild_handler)
    else:
        logger.warning(
            "recommend handler registration skipped: ai_worker partially initialized"
        )


register_recommend_handlers()


# ----------------------------------------------------------------------
# 读取端（WP-P6e）：读快照 + miss 触发重建（同日幂等）
# ----------------------------------------------------------------------

_RECOMMEND_READ_COLUMNS = (
    "SELECT target_user_id, score, coverage, rank_no, engine, reason_codes, "
    "direction_json FROM ai_recommendation_snapshot "
    "WHERE viewer_user_id = :viewer AND view_kind = :view_kind "
    "AND status = 'ready' "
    "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP()) "
    "ORDER BY rank_no LIMIT :limit"
)


async def read_recommendations(
    db: AsyncSession, viewer_id: int, view_kind: str, limit: int
) -> list[dict[str, Any]]:
    """读取某视图的 ready 快照（过期视为 miss），按 rank_no 升序。"""
    result = await db.execute(
        text(_RECOMMEND_READ_COLUMNS),
        {"viewer": int(viewer_id), "view_kind": str(view_kind), "limit": int(limit)},
    )
    items: list[dict[str, Any]] = []
    for row in result.mappings().all():
        direction = _maybe_json(row.get("direction_json")) or {}
        items.append(
            {
                "target_user_id": int(row["target_user_id"]),
                "score": float(row["score"]) if row.get("score") is not None else None,
                "coverage": (
                    float(row["coverage"]) if row.get("coverage") is not None else None
                ),
                "rank_no": int(row["rank_no"]),
                "engine": str(row.get("engine") or RECOMMEND_ENGINE_RULE),
                "reason_codes": list(_maybe_json(row.get("reason_codes")) or []),
                "reason_texts": list(direction.get("reason_texts") or []),
            }
        )
    return items


async def enqueue_recommendation_rebuild(
    db: AsyncSession, viewer_id: int
) -> Any | None:
    """读取端 miss 触发：入队一次重建（同用户同日至多一个任务，D4）。

    无 ``profile_text_extract`` 授权或无有效投影的用户不入队（否则永远空转）；
    任务本身视图无关（一次物化三视图），幂等键按"用户+UTC 日期"收敛。
    """
    from app.services.ai.tasks import enqueue_task

    compat = _compat()
    consent = await _load_active_consent(
        db, viewer_id, compat.PROJECTION_CONSENT_SCOPE
    )
    if consent is None:
        return None
    if await load_recommendation_inputs(db, viewer_id) is None:
        return None
    today = datetime.now(UTC).strftime("%Y%m%d")
    # revisions 必须携带当前版本向量：complete_task 的复核门禁以"入队时向量 ==
    # 完成时向量"判断任务是否仍有效——缺省（{}）会让每个重建任务在完成时被
    # 判 superseded 并回滚全部物化结果（review P1）。
    viewer_rev = await _compat()._load_revision_vector(db, viewer_id)
    return await enqueue_task(
        db=db,
        owner_user_id=int(viewer_id),
        task_type=RECOMMEND_TASK_TYPE,
        idempotency_key=f"recommend-view-{int(viewer_id)}-{today}",
        request_hash=hashlib.sha256(
            f"recommend-view:{int(viewer_id)}:{today}".encode()
        ).hexdigest(),
        revisions=viewer_rev,
        # 授权快照需 JSON 可序列化（granted_at → isoformat），不能透传原始行。
        consent={"scope": _compat().PROJECTION_CONSENT_SCOPE,
                 **_compat()._consent_snapshot(consent)},
    )
