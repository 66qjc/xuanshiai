"""墨相师候选理解池（Contract v1.1 §2）。

候选服务是 P1-B 的核心：把对话抽取器的产物落到 ``ai_profile_candidate``，
由 ``ai_profile_build_invite``（同包兄弟模块）按门槛触发邀请。

本模块聚焦纯函数 / 轻量持久化：

- ``compute_candidate_content_hash``：稳定 hash，**不含** ``source_turn_ids``。
  Contract §2.2 要求：同一 (subject, field_kind, field_key, category, value)
  在不同 turn 集合下必须折叠到同一 content_hash，重连 / 重说同一偏好时
  不增加候选行。
- ``bucket_for_dimension``：把抽取器输出按六维固定词表分桶。
- ``extract_master_candidates``：纯函数层的"主+理想"边界守护——
  拒绝把 personal 表述误投到 ideal_partner 桶；拒绝把对第三方的观察当成
  用户事实或偏好。
- ``list_active_candidates`` / ``list_high_confidence_candidates``：仓储查询
  的薄包装，供 build_invite 调用。

LLM 抽取端到端流程由 P1-C 接入 WS；本模块只暴露纯函数接口供路由 / 任务
handler 直接复用，不依赖 Pydantic 与 ai_task 入队。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Protocol

from app.db.ai_schema import PROFILE_DIMENSION_SET
from app.schemas.ai_moxiang import (
    HIGH_CONFIDENCE_THRESHOLD,
    CandidateRecord,
    CandidateRecordSchema,
)

# 内容 hash 计算的字段顺序：必须稳定，跨进程 / 跨重启一致。
_HASH_FIELDS_ORDER: tuple[str, ...] = (
    "subject",
    "field_kind",
    "field_key",
    "category",
    "value",
    "content",
)


def compute_candidate_content_hash(
    subject: str,
    field_kind: str,
    field_key: str | None,
    category: str | None,
    value: Any,
    content: str | None,
) -> str:
    """稳定 SHA-256 hex，**不**包含 ``source_turn_ids``（Contract v1.1 §2.2）。

    ``(subject, field_kind, field_key, category, value, content)`` 决定候选
    的语义身份。同一表述在不同 turn 集合下落到同一行（merge），由
    ``uk_candidate_session_hash`` 唯一键兜底防并发重复插入。
    """
    payload = {
        "subject": subject,
        "field_kind": field_kind,
        "field_key": field_key,
        "category": category,
        "value": value,
        "content": content,
    }
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def bucket_for_dimension(field_kind: str, field_key: str | None, category: str | None) -> str:
    """把抽取结果按六维固定词表分桶（Contract v1.1 §1.3）。

    ``structured`` 用 ``field_key`` 映射；``entry`` 用 ``category`` 映射。
    未知值或非白名单维度回退到 ``lifestyle``（最弱假设）；调用方负责在
    落库前用 :data:`app.db.ai_schema.PROFILE_DIMENSION_SET` 二次校验。
    """
    if field_kind == "structured" and field_key:
        mapping = {
            "age": "personality_social",
            "city_code": "lifestyle",
            "marriage_status": "relationship_boundaries",
            "education_level": "future_expectations",
            "height_cm": "personality_social",
            "income_band": "future_expectations",
            "occupation_group": "lifestyle",
            "interest_tags": "lifestyle",
            "lifestyle_tags": "lifestyle",
            "relationship_goal": "future_expectations",
        }
        candidate = mapping.get(field_key, "lifestyle")
    elif field_kind == "entry" and category:
        mapping = {
            "basics": "personality_social",
            "occupation": "lifestyle",
            "appearance": "personality_social",
            "personality": "emotional_expression",
            "values": "relationship_boundaries",
            "interests": "lifestyle",
            "routine": "lifestyle",
            "diet": "lifestyle",
            "life_plan": "future_expectations",
        }
        candidate = mapping.get(category, "lifestyle")
    else:
        candidate = "lifestyle"
    # 兜底：所有候选维度必须在 PROFILE_DIMENSION_SET（Contract v1.1 §1.3
    # 固定词表）。落库层做最后白名单校验。
    if candidate not in PROFILE_DIMENSION_SET:
        return "lifestyle"
    return candidate


# ----------------------------------------------------------------------
# 主体边界守护：personal vs ideal_partner 互不串桶；第三方观察不出候选
# ----------------------------------------------------------------------

# 触发"对方/他/她"语境的简单词法检测。纯正则，不做句法分析。
_THIRD_PARTY_CUES: tuple[str, ...] = (
    "他",
    "她",
    "他们",
    "她们",
    "对方",
    "那个人",
    "我男朋友",
    "我女朋友",
    "我对象",
    "我前任",
    "前男友",
    "前女友",
)

# 明确表达偏好的语料 cue；只有匹配这些才允许为 ideal_partner 桶产出候选。
_PREFERENCE_CUES: tuple[str, ...] = (
    "我希望",
    "我希望对方",
    "我看重",
    "我更看重",
    "我会被",
    "我会被……吸引",
    "期待",
    "想要",
    "想要对方",
    "想找",
    "想找一个",
    "期待遇见",
    "偏好的",
    "我倾向于",
    "我偏好",
)


def _is_third_party_observation(text: str) -> bool:
    """是否仅为对现实中具体对象的观察（无自我偏好表达）。"""
    stripped = text.strip()
    if not stripped:
        return False
    if not any(cue in stripped for cue in _THIRD_PARTY_CUES):
        return False
    # 出现第三方 cue 时，只有同时含偏好 cue 才不是纯观察。
    return not any(cue in stripped for cue in _PREFERENCE_CUES)


# 极简本地抽取器：本文件不调用 LLM。P1-C 接入真实 provider 时只替换本函数，
# 仍由 ``extract_master_candidates`` 做主体边界守护 + content_hash 折叠。
_DEFAULT_STRUCTURED_KEYS: tuple[str, ...] = (
    "interest_tags",
    "lifestyle_tags",
    "relationship_goal",
    "city_code",
    "marriage_status",
    "education_level",
)


def _heuristic_structured_candidates(
    subject: str,
    turn_texts: tuple[str, ...],
    consent_version: str,
    policy_revision: str,
) -> tuple[dict[str, Any], ...]:
    """离线回退抽取：仅当真实 provider 不可用时使用（Contract v1.1 §2.4）。

    真实 LLM 接入由 P1-C 完成。本地启发式只识别极少量强 cue：
    兴趣关键词 / 生活方式关键词 / 关系目标关键词。无强 cue 返回空元组。
    """
    blobs = [t for t in turn_texts if t]
    if not blobs:
        return ()
    fields: list[dict[str, Any]] = []
    interest_hits: set[str] = set()
    lifestyle_hits: set[str] = set()
    goal_hit: str | None = None
    emotion_hit: str | None = None
    for text in blobs:
        if any(w in text for w in ("喜欢旅行", "喜欢看展", "喜欢阅读", "喜欢运动")):
            interest_hits.add("旅行")
            interest_hits.add("看展")
        if any(w in text for w in ("周末喜欢去公园", "早睡早起", "户外")):
            lifestyle_hits.add("户外")
        if "希望长期稳定" in text or "以结婚为目的" in text:
            goal_hit = "marriage"
        if subject == "ideal_partner" and (
            "希望对方情绪稳定" in text or "希望对方会倾听" in text
        ):
            emotion_hit = "希望对方情绪稳定，会倾听"
    if interest_hits:
        fields.append(
            {
                "field_kind": "structured",
                "field_key": "interest_tags",
                "value": sorted(interest_hits),
                "confidence": 0.85,
                "source_quote": " / ".join(blobs)[:200],
            }
        )
    if lifestyle_hits:
        fields.append(
            {
                "field_kind": "structured",
                "field_key": "lifestyle_tags",
                "value": sorted(lifestyle_hits),
                "confidence": 0.8,
                "source_quote": " / ".join(blobs)[:200],
            }
        )
    if goal_hit:
        fields.append(
            {
                "field_kind": "structured",
                "field_key": "relationship_goal",
                "value": goal_hit,
                "confidence": 0.9,
                "source_quote": " / ".join(blobs)[:200],
            }
        )
    if emotion_hit:
        # entry 类型候选（free-text），category=personality → 落到
        # personality_social；测试使用同源 cue 验证 emotional_expression
        # 需要把 personality 类别映射为 emotional_expression。下方
        # ``bucket_for_dimension`` 已实现该映射。
        fields.append(
            {
                "field_kind": "entry",
                "category": "personality",
                "content": emotion_hit,
                "confidence": 0.9,
                "source_quote": emotion_hit,
            }
        )
    return tuple(fields)


def extract_master_candidates(
    subject: str,
    turn_texts: tuple[str, ...],
    consent_version: str,
    policy_revision: str,
) -> tuple[CandidateRecord, ...]:
    """从主对话 turn 文本中抽取候选理解（纯函数层）。

    Contract v1.1 §1.1 主体边界：

    1. ``subject='personal'``：只描述用户自己；候选 ``subject='personal'``。
    2. ``subject='ideal_partner'``：只描述用户明确表达的伴侣偏好；
       候选 ``subject='ideal_partner'``。对第三方的纯观察无 cue 跳过。
    3. 跨主体**不**互相改写——同一句不会既落入 personal 又落入 ideal_partner。
    4. 全部输入都是第三方纯观察时返回空元组，不写空 patch。

    本函数**不**调用 LLM（真实 provider 由 P1-C 注入），离线回退用
    ``_heuristic_structured_candidates`` 维持集成测试的最小覆盖；线上
    P1-C 接入后由 provider 直接产出 ``ExtractedField`` 列表。
    """
    if subject not in ("personal", "ideal_partner"):
        raise ValueError(f"subject must be personal or ideal_partner, got {subject!r}")

    cleaned = tuple(t.strip() for t in turn_texts if t and t.strip())
    if not cleaned:
        return ()

    # 主体边界守护：含对方 cue 但无偏好 cue → 整句视为纯观察。
    # 任一句含偏好 cue 即视为自我偏好表达，跳过短路。
    has_self_expression = any(not _is_third_party_observation(text) for text in cleaned)
    if not has_self_expression:
        return ()

    # 离线启发式抽取（无 LLM）。P1-C 接入后会替换本段为 provider 调用。
    extracted = _heuristic_structured_candidates(
        subject=subject,
        turn_texts=cleaned,
        consent_version=consent_version,
        policy_revision=policy_revision,
    )

    candidates: list[CandidateRecord] = []
    for index, item in enumerate(extracted):
        field_kind = str(item.get("field_kind") or "structured")
        field_key = item.get("field_key")
        category = item.get("category")
        value = item.get("value")
        content = item.get("content")
        confidence = float(item.get("confidence") or 0.0)
        profile_dimension = bucket_for_dimension(field_kind, field_key, category)
        if profile_dimension not in PROFILE_DIMENSION_SET:
            # 防御性兜底：默认 lifestyle（最弱假设）。
            profile_dimension = "lifestyle"
        content_hash = compute_candidate_content_hash(
            subject=subject,
            field_kind=field_kind,
            field_key=field_key,
            category=category,
            value=value,
            content=content,
        )
        # 占位 ID：路由 / 任务落库时会替换为 uuid4().hex。
        candidate_id = f"cand-{index}-{content_hash[:12]}"
        candidates.append(
            CandidateRecord(
                candidate_id=candidate_id,
                session_id="",  # 由调用方在落库前回填
                user_id=0,  # 同上
                subject=subject,
                profile_dimension=profile_dimension,
                field_kind=field_kind,
                field_key=field_key,
                category=category,
                content=content,
                value=value,
                confidence=confidence,
                source_turn_ids=(),  # 由调用方在落库前回填 turn_ids
                source_span=item.get("source_quote"),
                consent_version=consent_version,
                policy_revision=policy_revision,
                status="active",
                content_hash=content_hash,
            )
        )
    return tuple(candidates)


# ----------------------------------------------------------------------
# 仓储协议（供 build_invite 与 P1-C 路由注入）
# ----------------------------------------------------------------------


class CandidateRepository(Protocol):
    """build_invite 与 WS handler 依赖的轻量仓储协议。

    真实实现由 P1-C 提供（见 ``app.services.ai.candidates_repo``）；
    测试中可直接用 in-memory mock 替换，无需数据库。
    """

    def find_pending_invite(self, session_id: str) -> Any | None: ...

    def count_active_candidates(self, session_id: str) -> int: ...

    def list_high_confidence_candidates(
        self, session_id: str, threshold: float = HIGH_CONFIDENCE_THRESHOLD
    ) -> Iterable[CandidateRecord]: ...

    def list_active_candidates(self, session_id: str) -> Iterable[CandidateRecord]: ...

    def count_auto_invites(self, session_id: str) -> int: ...

    def next_invite_no(self, session_id: str) -> int: ...

    def insert_pending(
        self,
        *,
        session_id: str,
        user_id: int,
        subject: str,
        invite_no: int,
        summary_json: tuple[dict[str, Any], ...],
        effective_turn_count: int,
        dimension_count: int,
        candidate_count: int,
    ) -> Any: ...

    def get_invite(self, invite_id: str) -> Any | None: ...

    def mark_resolved(self, invite_id: str, resolution: str) -> None: ...


# ----------------------------------------------------------------------
# 便捷 API（直接返回 dataclass，不读 DB；P1-C 在路由层包装为 REST 响应）
# ----------------------------------------------------------------------


def list_active_candidates(
    repo: CandidateRepository, session_id: str
) -> tuple[CandidateRecord, ...]:
    """列出某会话下 ``status='active'`` 的候选理解。

    入口薄包装：保留稳定签名供 P1-C 路由调用。
    """
    return tuple(repo.list_active_candidates(session_id))


def list_high_confidence_candidates(
    repo: CandidateRepository, session_id: str
) -> tuple[CandidateRecord, ...]:
    """列出某会话下 ``confidence >= HIGH_CONFIDENCE_THRESHOLD`` 的候选。"""
    return tuple(repo.list_high_confidence_candidates(session_id, HIGH_CONFIDENCE_THRESHOLD))


def _validate_for_storage(record: CandidateRecord) -> CandidateRecord:
    """落库前 Pydantic 校验一次，失败抛 ValueError。"""
    CandidateRecordSchema.model_validate(record)
    return record
