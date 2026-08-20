"""M06 AI 匹配度服务（Task 11，统一方案 §9/§10.3/§11.2，执行计划 §3.1/§3.2）。

本模块是 M06「资料合拍参考」shadow 的事实源：

- ``directional_score`` / ``compute_compatibility`` 是纯函数（简报 Step 3 逐字），
  计算两个方向的加权平均并取调和平均为 ``pair_score``，``coverage`` 为两方向
  可用权重占比的较小值；双方方向 coverage 均达 0.50 才生成可比较 shadow 分数，
  低于阈值返回 ``coverage_insufficient``，缺失维度记 ``DIMENSION_UNKNOWN`` 且
  不补负面事实（§9.2）。
- 维度权重冻结为 §9.2 的八类；MBTI、认证、活跃、会员、置顶不进入兼容度。
- ``build_compatibility_evidence`` 给每条原因码绑定 ``EvidenceRef``（字段 key、
  是否可展示、限制说明），不存对方敏感原文；写入快照时附上五维 revision pair
  （§9.3）。
- ``write_shadow_snapshot`` 只写 ``ai_compatibility_snapshot``：algorithm_version
  ``compatibility-rule-v1``、score_semantics ``rule_based_reference_shadow``、
  experiment_bucket ``shadow``、display_eligible 固定 0；绝不触碰旧
  ``match_score``/``match_reason``（语义恒为 ``legacy-rule-v1``，§10.4）。
- ``read_compatibility_snapshot`` 每次读取重过 ``CandidateVisibilityService`` 门禁
  （不可见 → ``CANDIDATE_NOT_VISIBLE`` 404，不泄露归属）；版本/隐私 revision 变化
  或结果过期 → ``stale``；``blocked`` 不展示候选、``coverage_insufficient`` 不伪造
  完整分。
- ``request_compatibility_recompute`` 先过可见性门禁，再做 expected revision 校验
  （不符 → ``RESULT_STALE`` 409），最后入队 ``compatibility`` 任务（§9.4）。

与 Task 6/7/8/10 一致，本模块函数**不**调用 ``commit()``——调用方（路由或 Worker）
控制事务。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.ai_common import ProjectionKind
from app.schemas.ai_compatibility import (
    CompatibilityDirectionScores,
    CompatibilitySnapshotRead,
    CompatibilitySnapshotStatus,
)
from app.services.ai.tasks import (
    AiTaskRecord,
    enqueue_task,
    fail_task,
)
from app.services.candidate_visibility import (
    CandidateVisibilityService,
    VisibilityScene,
)
from app.services.revisions import RevisionVector

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 冻结常量（统一方案 §9.1/§9.3，执行计划 §3.1/§3.2）
# ----------------------------------------------------------------------

COMPATIBILITY_ALGORITHM_VERSION = "compatibility-rule-v1"
LEGACY_ALGORITHM_VERSION = "legacy-rule-v1"
SCORE_SEMANTICS = "rule_based_reference_shadow"
COMPATIBILITY_EXPERIMENT_BUCKET = "shadow"
COMPATIBILITY_CONSENT_SCOPE = "compatibility_shadow"
COMPATIBILITY_POLICY_REVISION = "ai-policy-2026-08-07-v1"
COMPATIBILITY_TASK_TYPE = "compatibility"
PROJECTION_CONSENT_SCOPE = "profile_text_extract"
DISCLAIMER = "仅根据双方当前可见且已确认资料整理，供了解和破冰参考"
# 双方方向 coverage 均达 0.50 才允许生成可比较 shadow score（§9.2）。
COVERAGE_THRESHOLD = 0.50

# 稳定原因码（§9.3 逐字）。
REASON_AGE = "AGE_MUTUAL_WITHIN_RANGE"
REASON_CITY = "CITY_MUTUAL_ACCEPTED"
REASON_MARRIAGE = "MARRIAGE_MUTUAL_ACCEPTED"
REASON_EDUCATION = "EDUCATION_MUTUAL_WITHIN_RANGE"
REASON_HEIGHT = "HEIGHT_MUTUAL_WITHIN_RANGE"
REASON_INCOME = "INCOME_MUTUAL_WITHIN_RANGE"
REASON_INTEREST = "INTEREST_OVERLAP"
REASON_GOAL = "RELATIONSHIP_GOAL_SHARED"
REASON_UNKNOWN = "DIMENSION_UNKNOWN"
REASON_COVERAGE = "COVERAGE_INSUFFICIENT"
REASON_NOT_VISIBLE = "CANDIDATE_NOT_VISIBLE"

# 原因码 → 证据字段 key（evidence_refs 只引用字段 key 与 revision，不含原文）。
_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    REASON_AGE: ("age",),
    REASON_CITY: ("city_code",),
    REASON_MARRIAGE: ("marriage_status",),
    REASON_EDUCATION: ("education_level",),
    REASON_HEIGHT: ("height_cm",),
    REASON_INCOME: ("income_band",),
    REASON_INTEREST: ("interest_tags",),
    REASON_GOAL: ("relationship_goal",),
    REASON_UNKNOWN: (),
    REASON_COVERAGE: (),
    REASON_NOT_VISIBLE: (),
}

# 原因码 → 可展示标记：只有双方可见已确认资料形成的相互满足码才可展示。
_NON_DISPLAYABLE_REASONS = frozenset(
    {REASON_UNKNOWN, REASON_COVERAGE, REASON_NOT_VISIBLE}
)

# 原因码 → 限制说明（模板解释优先，§9.3）。
_EVIDENCE_LIMITATIONS: dict[str, str] = {
    REASON_AGE: "双方年龄均落在对方已确认的年龄偏好区间内",
    REASON_CITY: "双方所在城市均在对方已确认可接受的城市范围内",
    REASON_MARRIAGE: "双方婚姻状态与对方已确认的偏好一致",
    REASON_EDUCATION: "双方学历均达到对方已确认的学历下限",
    REASON_HEIGHT: "双方身高均在对方已确认的身高偏好区间内",
    REASON_INCOME: "双方收入均在对方已确认的收入偏好区间内",
    REASON_INTEREST: "双方兴趣标签存在重叠",
    REASON_GOAL: "双方关系期待一致",
    REASON_UNKNOWN: "该维度缺少任一方的已确认资料，不计入加权平均",
    REASON_COVERAGE: "任一方向可用维度权重低于 0.50，不生成可比较分数",
    REASON_NOT_VISIBLE: "目标当前不可见，不生成资料合拍参考",
}

candidate_visibility_service = CandidateVisibilityService()


# ----------------------------------------------------------------------
# 领域对象
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSet:
    """双方参与兼容度计算的投影字段：``profile`` + ``preference``。

    来自 Task 9 的 ``personal_compatibility``（本人已确认事实）与
    ``ideal_partner_preference``（本人偏好，self_only）。字段仅限 Task 1
    allowlist，不含认证/活跃/会员等信号。
    """

    profile: dict[str, Any]
    preference: dict[str, Any]


@dataclass(frozen=True)
class DimensionRule:
    """一维双向规则：key/weight 冻结（§9.2），``score`` 为纯函数。

    ``score(source_preference, target_value) -> float`` 返回 0..100 的方向满足度。
    """

    key: str
    weight: float
    score: Callable[[Any, Any], float]


@dataclass(frozen=True)
class RuleSet:
    """维度规则集；``total_weight`` 为全部权重之和（覆盖率分母）。"""

    dimensions: tuple[DimensionRule, ...]

    @property
    def total_weight(self) -> float:
        return float(sum(rule.weight for rule in self.dimensions))


@dataclass(frozen=True)
class CompatibilityResult:
    """双向规则结果：pair_score/directions/coverage/reason_codes/status。"""

    pair_score: float | None
    directions: tuple[float | None, float | None]
    coverage: float
    reason_codes: tuple[str, ...]
    status: str

    @classmethod
    def ready(
        cls,
        *,
        pair_score: float,
        directions: tuple[float, float],
        coverage: float,
        reason_codes: tuple[str, ...],
    ) -> CompatibilityResult:
        return cls(
            pair_score=round(float(pair_score), 2),
            directions=(float(directions[0]), float(directions[1])),
            coverage=round(float(coverage), 4),
            reason_codes=tuple(reason_codes),
            status=CompatibilitySnapshotStatus.READY.value,
        )

    @classmethod
    def blocked(
        cls,
        *,
        coverage: float,
        reason_codes: tuple[str, ...],
    ) -> CompatibilityResult:
        """覆盖度不足/无可用维度时的结果：不伪造完整分（§9.2）。"""
        return cls(
            pair_score=None,
            directions=(None, None),
            coverage=round(float(coverage), 4),
            reason_codes=tuple(reason_codes),
            status=CompatibilitySnapshotStatus.COVERAGE_INSUFFICIENT.value,
        )


@dataclass(frozen=True)
class EvidenceRef:
    """一条原因码的证据引用：字段 key、可展示标记与限制说明。

    ``source_revisions`` 在写快照时由调用方填充为五维 revision pair，不存
    对方敏感原文（§9.3）。
    """

    reason_code: str
    field_keys: tuple[str, ...]
    displayable: bool
    limitation: str
    source_revisions: tuple[RevisionVector, RevisionVector] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "field_keys": list(self.field_keys),
            "displayable": self.displayable,
            "limitation": self.limitation,
            "source_revisions": (
                {
                    "viewer": self.source_revisions[0].as_dict(),
                    "target": self.source_revisions[1].as_dict(),
                }
                if self.source_revisions is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class CompatibilityRecomputeAccepted:
    """POST recompute 的 202 响应（prediction + task）。"""

    snapshot_id: str
    task_id: str
    status: str
    poll_after_ms: int = 1000
    expires_at: datetime | None = None


# ----------------------------------------------------------------------
# 稳定业务错误（执行计划 §3.2 错误码注册表）
# ----------------------------------------------------------------------


class CompatibilityError(Exception):
    code = "AI_INPUT_INVALID"
    status_code = 400
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CompatibilityInputInvalid(CompatibilityError):
    """400 AI_INPUT_INVALID：类型/枚举/自引用等入参非法。"""

    code = "AI_INPUT_INVALID"
    status_code = 400


class CandidateNotVisible(CompatibilityError):
    """404 CANDIDATE_NOT_VISIBLE：门禁失败，不返回具体拒绝原因（§11.2）。"""

    code = "CANDIDATE_NOT_VISIBLE"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("目标用户当前不可见")
        self.message = "目标用户当前不可见"


class CompatibilityConsentRequired(CompatibilityError):
    """403 AI_CONSENT_REQUIRED：compatibility_shadow 授权缺失或已撤回。"""

    code = "AI_CONSENT_REQUIRED"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("尚未同意资料合拍参考授权")
        self.message = "尚未同意资料合拍参考授权"


class CompatibilityResultStale(CompatibilityError):
    """409 RESULT_STALE：expected revision 与当前版本不符。"""

    code = "RESULT_STALE"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("资料版本已变化，请刷新后重新重算")
        self.message = "资料版本已变化，请刷新后重新重算"


# ----------------------------------------------------------------------
# §9.2 维度打分纯函数（参考规则，不是科学概率）
# ----------------------------------------------------------------------


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if hasattr(value, "value"):
        raw = value.value
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _score_within_range(pref: Any, value: Any) -> float:
    """min/max 区间满足度：区间偏好缺失时按 100（偏好不设限）。"""
    number = _as_number(value)
    if number is None:
        return 0.0
    if not isinstance(pref, dict):
        return 0.0
    low = _as_number(pref.get("min"))
    high = _as_number(pref.get("max"))
    if low is not None and number < low:
        return 0.0
    if high is not None and number > high:
        return 0.0
    return 100.0


def _score_age(pref: Any, value: Any) -> float:
    return _score_within_range(pref, value)


def _score_city(pref: Any, value: Any) -> float:
    target_city = _as_str(value)
    if not target_city:
        return 0.0
    accepted = pref if isinstance(pref, list) else [pref]
    for item in accepted:
        if _as_str(item) == target_city:
            return 100.0
    return 0.0


def _score_marriage(pref: Any, value: Any) -> float:
    pref_s = _as_str(pref)
    value_s = _as_str(value)
    if not pref_s or not value_s:
        return 0.0
    return 100.0 if pref_s == value_s else 0.0


def _score_education(pref: Any, value: Any) -> float:
    """学历编号越小越高（1=博士…5=高中）；target <= 偏好下限即满足。"""
    target = _as_number(value)
    if target is None:
        return 0.0
    if isinstance(pref, dict):
        minimum = _as_number(pref.get("min"))
    else:
        minimum = _as_number(pref)
    if minimum is None:
        return 100.0
    return 100.0 if target <= minimum else 0.0


def _score_height(pref: Any, value: Any) -> float:
    return _score_within_range(pref, value)


def _score_income(pref: Any, value: Any) -> float:
    return _score_within_range(pref, value)


def _score_interest(pref: Any, value: Any) -> float:
    pref_tags = pref if isinstance(pref, list) else [pref]
    value_tags = value if isinstance(value, list) else [value]
    pref_set = {str(t).strip() for t in pref_tags if str(t).strip()}
    value_set = {str(t).strip() for t in value_tags if str(t).strip()}
    if not pref_set or not value_set:
        return 0.0
    overlap = pref_set & value_set
    return round(len(overlap) / len(pref_set) * 100.0, 2)


def _score_relationship_goal(pref: Any, value: Any) -> float:
    pref_s = _as_str(pref)
    value_s = _as_str(value)
    if not pref_s or not value_s:
        return 0.0
    return 100.0 if pref_s == value_s else 0.0


# §9.2 冻结维度与权重：年龄 20、城市/异地 15、婚姻 10、学历 10、身高 10、
# 收入 10、兴趣标签 15、关系期待 10。MBTI/认证/活跃/会员/置顶不进入兼容度。
_DIMENSION_SCORERS: dict[str, Callable[[Any, Any], float]] = {
    "age": _score_age,
    "city_code": _score_city,
    "marriage_status": _score_marriage,
    "education_level": _score_education,
    "height_cm": _score_height,
    "income_band": _score_income,
    "interest_tags": _score_interest,
    "relationship_goal": _score_relationship_goal,
}

COMPATIBILITY_RULES = RuleSet(
    dimensions=tuple(
        DimensionRule(
            key=key,
            weight=float(weight),
            score=_DIMENSION_SCORERS[key],
        )
        for key, weight in (
            ("age", 20),
            ("city_code", 15),
            ("marriage_status", 10),
            ("education_level", 10),
            ("height_cm", 10),
            ("income_band", 10),
            ("interest_tags", 15),
            ("relationship_goal", 10),
        )
    )
)


# ----------------------------------------------------------------------
# 双向规则纯函数（简报 Step 3 逐字）
# ----------------------------------------------------------------------


def directional_score(
    source: FeatureSet, target: FeatureSet, rules: RuleSet
) -> tuple[float | None, float, tuple[str, ...]]:
    available = []
    reasons = []
    for dimension in rules.dimensions:
        source_preference = source.preference.get(dimension.key)
        target_value = target.profile.get(dimension.key)
        if source_preference is None or target_value is None:
            reasons.append("DIMENSION_UNKNOWN")
            continue
        available.append((dimension.weight, dimension.score(source_preference, target_value)))
    if not available:
        return None, 0.0, tuple(reasons)
    total_weight = sum(weight for weight, _ in available)
    score = sum(weight * value for weight, value in available) / total_weight
    return score, total_weight / rules.total_weight, tuple(reasons)


def compute_compatibility(
    viewer: FeatureSet, target: FeatureSet, rules: RuleSet
) -> CompatibilityResult:
    first, first_coverage, first_reasons = directional_score(viewer, target, rules)
    second, second_coverage, second_reasons = directional_score(target, viewer, rules)
    coverage = min(first_coverage, second_coverage)
    if first is None or second is None or coverage < COVERAGE_THRESHOLD:
        return CompatibilityResult.blocked(
            coverage=coverage,
            reason_codes=tuple(sorted(set(first_reasons + second_reasons + ("COVERAGE_INSUFFICIENT",)))),
        )
    pair_score = 2 * first * second / (first + second) if first + second else 0.0
    return CompatibilityResult.ready(
        pair_score=pair_score,
        directions=(first, second),
        coverage=coverage,
        reason_codes=tuple(sorted(set(first_reasons + second_reasons))),
    )


# ----------------------------------------------------------------------
# 相互满足原因码与证据（§9.3）
# ----------------------------------------------------------------------

_DIMENSION_TO_REASON: dict[str, str] = {
    "age": REASON_AGE,
    "city_code": REASON_CITY,
    "marriage_status": REASON_MARRIAGE,
    "education_level": REASON_EDUCATION,
    "height_cm": REASON_HEIGHT,
    "income_band": REASON_INCOME,
    "interest_tags": REASON_INTEREST,
    "relationship_goal": REASON_GOAL,
}


def mutual_reason_codes(
    viewer: FeatureSet, target: FeatureSet, rules: RuleSet
) -> tuple[str, ...]:
    """返回双方方向都满足的稳定原因码（§9.3 的相互满足码）。

    缺失维度已由 ``DIMENSION_UNKNOWN`` 标记，不在这里重复出现；偏好冲突只影响
    方向满足度，只有两方向均 >0 才产生相互满足码。
    """
    codes: list[str] = []
    for dimension in rules.dimensions:
        reason = _DIMENSION_TO_REASON.get(dimension.key)
        if reason is None:
            continue
        pref_a = viewer.preference.get(dimension.key)
        value_b = target.profile.get(dimension.key)
        pref_b = target.preference.get(dimension.key)
        value_a = viewer.profile.get(dimension.key)
        if pref_a is None or value_b is None or pref_b is None or value_a is None:
            continue
        if dimension.score(pref_a, value_b) > 0 and dimension.score(pref_b, value_a) > 0:
            codes.append(reason)
    return tuple(codes)


def with_evidence_codes(
    result: CompatibilityResult,
    viewer: FeatureSet,
    target: FeatureSet,
    rules: RuleSet,
) -> CompatibilityResult:
    """把相互满足码并入 ready 结果的 reason_codes（不改变分数）。

    ``compute_compatibility`` 只产生 DIMENSION_UNKNOWN/COVERAGE_INSUFFICIENT；
    快照落库前的 reason_codes 需要包含 §9.3 的相互满足码，供证据解释使用。
    """
    if result.status != CompatibilitySnapshotStatus.READY.value:
        return result
    combined = tuple(
        sorted(set(result.reason_codes + mutual_reason_codes(viewer, target, rules)))
    )
    return replace(result, reason_codes=combined)


def build_compatibility_evidence(result: CompatibilityResult) -> tuple[EvidenceRef, ...]:
    """把每条原因码绑定一个证据引用（字段 key、可展示、限制说明）。

    证据只引用字段 key 与可展示标记，不含对方敏感原文；source revision 由
    写快照路径填充。reason code 与 evidence ref 一一对应（100% 对齐）。
    """
    refs: list[EvidenceRef] = []
    for code in result.reason_codes:
        refs.append(
            EvidenceRef(
                reason_code=code,
                field_keys=_EVIDENCE_FIELDS.get(code, ()),
                displayable=code not in _NON_DISPLAYABLE_REASONS,
                limitation=_EVIDENCE_LIMITATIONS.get(code, DISCLAIMER),
            )
        )
    return tuple(refs)


# ----------------------------------------------------------------------
# 内部辅助（不 commit，由调用方控制事务）
# ----------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _maybe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if isinstance(expires_at, datetime):
        return expires_at.replace(tzinfo=None) < _now_utc()
    return False


def _consent_snapshot(consent: dict[str, Any] | None) -> dict[str, Any]:
    return _consent_snapshot_for_scope(consent, COMPATIBILITY_CONSENT_SCOPE)


def _consent_snapshot_for_scope(
    consent: dict[str, Any] | None, default_scope: str
) -> dict[str, Any]:
    if not consent:
        return {}
    granted_at = consent.get("granted_at")
    if hasattr(granted_at, "isoformat"):
        granted_at = granted_at.isoformat()
    return {
        "scope": str(consent.get("scope") or default_scope),
        "version": str(consent.get("version") or ""),
        "policy_revision": str(consent.get("policy_revision") or ""),
        "granted_at": granted_at,
    }


def _normalize_consent_pair(consent: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not consent:
        return {"viewer": {}, "target": {}}
    viewer = consent.get("viewer") if isinstance(consent.get("viewer"), dict) else None
    target = consent.get("target") if isinstance(consent.get("target"), dict) else None
    if viewer is not None or target is not None:
        return {
            "viewer": _consent_snapshot(viewer or {}),
            "target": _consent_snapshot(target or {}),
        }
    # Low-level shadow writers historically accepted one consent snapshot. The
    # worker/recompute path always supplies both sides; a missing target remains
    # unreadable until a dual-consent snapshot is written.
    logger.debug(
        "_normalize_consent_pair single-side downgrade: consent has no "
        "viewer/target pair, using viewer-only snapshot (compatibility path "
        "only; new code must pass a dual-side restructured consent)"
    )
    return {"viewer": _consent_snapshot(consent), "target": {}}


def _consent_snapshot_matches(
    stored: dict[str, Any] | None, current: dict[str, Any] | None
) -> bool:
    if not stored or not current:
        return False
    stored_snapshot = {
        "scope": str(stored.get("scope") or ""),
        "version": str(stored.get("version") or ""),
        "policy_revision": str(stored.get("policy_revision") or ""),
        "granted_at": str(stored.get("granted_at") or ""),
    }
    current_snapshot = {
        "scope": str(current.get("scope") or ""),
        "version": str(current.get("version") or ""),
        "policy_revision": str(current.get("policy_revision") or ""),
        "granted_at": str(current.get("granted_at") or ""),
    }
    return stored_snapshot == current_snapshot


async def _pair_consents_current(
    db: AsyncSession,
    viewer_id: int,
    target_id: int,
    stored_pair: dict[str, Any] | None,
) -> bool:
    pair = _normalize_consent_pair(stored_pair)
    viewer = await _load_active_consent(db, viewer_id, COMPATIBILITY_CONSENT_SCOPE)
    target = await _load_active_consent(db, target_id, COMPATIBILITY_CONSENT_SCOPE)
    return _consent_snapshot_matches(
        pair.get("viewer"), _consent_snapshot(viewer)
    ) and _consent_snapshot_matches(pair.get("target"), _consent_snapshot(target))


async def _first_row(result: Any) -> dict[str, Any] | None:
    return result.mappings().first()


async def _load_active_consent(
    db: AsyncSession, user_id: int, scope: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT user_id, scope, version, policy_revision, granted_at "
            "FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "scope": scope},
    )
    return await _first_row(result)


async def _load_revision_vector(db: AsyncSession, user_id: int) -> RevisionVector:
    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = await _first_row(result)
    if row is None:
        return RevisionVector()
    return RevisionVector(
        profile=int(row["profile_revision"] or 0),
        preference=int(row["preference_revision"] or 0),
        privacy=int(row["privacy_revision"] or 0),
        relationship=int(row["relationship_revision"] or 0),
        policy=int(row["policy_revision"] or 0),
    )


def _snapshot_hash(
    viewer_id: int,
    target_id: int,
    result: CompatibilityResult,
    viewer_rev: RevisionVector,
    target_rev: RevisionVector,
) -> str:
    raw = json.dumps(
        {
            "algorithm_version": COMPATIBILITY_ALGORITHM_VERSION,
            "viewer_user_id": viewer_id,
            "target_user_id": target_id,
            "status": result.status,
            "pair_score": result.pair_score,
            "coverage": result.coverage,
            "directions": (
                list(result.directions) if result.pair_score is not None else None
            ),
            "reason_codes": list(result.reason_codes),
            "source_revision_pair": {
                "viewer": viewer_rev.as_dict(),
                "target": target_rev.as_dict(),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def _load_projection_rows(
    db: AsyncSession, viewer_id: int, target_id: int
) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            "SELECT id, subject_user_id, projection_kind, fields_json, source_hash, "
            "source_revision_json, profile_revision, preference_revision, "
            "privacy_revision, relationship_revision, policy_revision, "
            "consent_snapshot_json, status, expires_at "
            "FROM ai_feature_projection "
            "WHERE subject_user_id IN (:uid_viewer, :uid_target) "
            "AND projection_kind IN ('personal_compatibility', "
            " 'ideal_partner_preference') "
            "AND status = 'active'"
        ),
        {"uid_viewer": viewer_id, "uid_target": target_id},
    )
    return list(result.mappings().all())


async def _load_current_projection_rows(
    db: AsyncSession, viewer_id: int, target_id: int
) -> list[dict[str, Any]]:
    rows = await _load_projection_rows(db, viewer_id, target_id)
    revision_cache: dict[int, RevisionVector] = {}
    consent_cache: dict[tuple[int, str], dict[str, Any] | None] = {}
    current: list[dict[str, Any]] = []
    for row in rows:
        user_id = int(row["subject_user_id"])
        if _is_expired(row.get("expires_at")):
            continue
        if user_id not in revision_cache:
            revision_cache[user_id] = await _load_revision_vector(db, user_id)
        stored_revision = _maybe_json(row.get("source_revision_json"))
        if not isinstance(stored_revision, dict):
            stored_revision = {
                "profile": int(row.get("profile_revision") or 0),
                "preference": int(row.get("preference_revision") or 0),
                "privacy": int(row.get("privacy_revision") or 0),
                "relationship": int(row.get("relationship_revision") or 0),
                "policy": int(row.get("policy_revision") or 0),
            }
        if stored_revision != revision_cache[user_id].as_dict():
            continue
        stored_consent = _maybe_json(row.get("consent_snapshot_json"))
        if not isinstance(stored_consent, dict):
            continue
        scope = str(stored_consent.get("scope") or "")
        if scope != PROJECTION_CONSENT_SCOPE:
            continue
        cache_key = (user_id, scope)
        if cache_key not in consent_cache:
            consent_cache[cache_key] = await _load_active_consent(db, user_id, scope)
        active_snapshot = _consent_snapshot_for_scope(
            consent_cache[cache_key], scope
        )
        if not _consent_snapshot_matches(stored_consent, active_snapshot):
            continue
        current.append(dict(row))
    # A user can have historical projections for the same kind. Only the
    # newest current projection is an eligible compatibility input.
    latest: dict[tuple[int, str], dict[str, Any]] = {}
    for row in current:
        key = (int(row["subject_user_id"]), str(row["projection_kind"]))
        if key not in latest or int(row.get("id") or 0) > int(latest[key].get("id") or 0):
            latest[key] = row
    return list(latest.values())


async def load_compatibility_features(
    db: AsyncSession, viewer_id: int, target_id: int
) -> tuple[FeatureSet, FeatureSet]:
    """读取双方的 personal_compatibility 与 ideal_partner_preference 投影。

    viewer = 本人 personal_compatibility（profile）+ 本人 ideal_partner_preference
    （preference）；target 同理。缺投影/未激活时对应字段为空 dict，由规则引擎
    记 DIMENSION_UNKNOWN/coverage_insufficient。
    """
    rows = await _load_current_projection_rows(db, viewer_id, target_id)
    by_user_kind: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        by_user_kind[(int(row["subject_user_id"]), str(row["projection_kind"]))] = row

    def _fields(user_id: int, kind: str) -> dict[str, Any]:
        row = by_user_kind.get((user_id, kind))
        if row is None:
            return {}
        return _maybe_json(row.get("fields_json")) or {}

    return (
        FeatureSet(
            profile=_fields(viewer_id, ProjectionKind.PERSONAL_COMPATIBILITY.value),
            preference=_fields(
                viewer_id, ProjectionKind.IDEAL_PARTNER_PREFERENCE.value
            ),
        ),
        FeatureSet(
            profile=_fields(target_id, ProjectionKind.PERSONAL_COMPATIBILITY.value),
            preference=_fields(
                target_id, ProjectionKind.IDEAL_PARTNER_PREFERENCE.value
            ),
        ),
    )


async def compute_and_write_shadow(
    db: AsyncSession,
    viewer_id: int,
    target_id: int,
    revisions: tuple[RevisionVector, RevisionVector],
    consent: dict[str, Any] | None,
    snapshot_id: str | None = None,
) -> str:
    """加载投影 → 计算双向规则 → 并入证据码 → 写 shadow 快照。"""
    if not await _pair_consents_current(
        db, viewer_id, target_id, _normalize_consent_pair(consent)
    ):
        raise CompatibilityConsentRequired()
    viewer_fs, target_fs = await load_compatibility_features(db, viewer_id, target_id)
    result = compute_compatibility(viewer_fs, target_fs, COMPATIBILITY_RULES)
    result = with_evidence_codes(
        result, viewer_fs, target_fs, COMPATIBILITY_RULES
    )
    return await write_shadow_snapshot(
        db, viewer_id, target_id, result, revisions, consent, snapshot_id=snapshot_id
    )


# ----------------------------------------------------------------------
# shadow 快照写入（§9.4/§10.4）
# ----------------------------------------------------------------------

_SNAPSHOT_INSERT_COLUMNS = (
    "snapshot_id, viewer_user_id, target_user_id, algorithm_version, snapshot_hash, "
    "status, score_semantics, compatibility_index, coverage, direction_json, "
    "reason_codes, evidence_json, profile_revision_pair_json, "
    "privacy_revision_pair_json, source_revision_pair_json, "
    "consent_snapshot_pair_json, experiment_bucket, display_eligible, disclaimer, "
    "calculated_at, expires_at, created_at"
)


async def write_shadow_snapshot(
    db: AsyncSession,
    viewer_id: int,
    target_id: int,
    result: CompatibilityResult,
    revisions: tuple[RevisionVector, RevisionVector],
    consent: dict[str, Any] | None,
    snapshot_id: str | None = None,
) -> str:
    """把双向规则结果写入 ``ai_compatibility_snapshot``（shadow，永不覆盖旧字段）。

    固定写 algorithm_version=compatibility-rule-v1、score_semantics=
    rule_based_reference_shadow、experiment_bucket=shadow、display_eligible=0；
    保存 profile/privacy revision pair、expires_at 与 evidence_json。本函数绝不
    触碰旧 ``match_score``/``match_reason`` 或推荐排序。``viewer_id`` 必须与
    ``target_id`` 不同（数据库 CHECK 亦强制）。不 commit。
    """
    if int(viewer_id) == int(target_id):
        raise CompatibilityInputInvalid("不能与自己计算资料合拍参考")
    viewer_rev, target_rev = revisions
    consent_pair = _normalize_consent_pair(consent)
    if not consent_pair["viewer"] or not consent_pair["target"]:
        raise CompatibilityConsentRequired()
    if snapshot_id is None:
        snapshot_id = f"cp_{uuid.uuid4().hex}"
    evidence_refs = build_compatibility_evidence(result)
    evidence_refs = tuple(
        replace(ref, source_revisions=(viewer_rev, target_rev))
        for ref in evidence_refs
    )
    snapshot_hash = _snapshot_hash(
        int(viewer_id), int(target_id), result, viewer_rev, target_rev
    )
    expires_at = _now_utc() + timedelta(
        minutes=settings.ai_compatibility_snapshot_ttl_minutes
    )
    ready = result.status == CompatibilitySnapshotStatus.READY.value
    directions = None
    if ready and result.pair_score is not None and result.directions[0] is not None:
        directions = {
            "viewer_to_target": round(float(result.directions[0]), 2),
            "target_to_viewer": round(float(result.directions[1] or 0.0), 2),
        }
    await db.execute(
        text(
            f"INSERT INTO ai_compatibility_snapshot ({_SNAPSHOT_INSERT_COLUMNS}) "
            "VALUES (:snapshot_id, :viewer_user_id, :target_user_id, "
            " :algorithm_version, :snapshot_hash, :status, :score_semantics, "
            " :compatibility_index, :coverage, :direction_json, :reason_codes, "
            " :evidence_json, :profile_revision_pair_json, "
            " :privacy_revision_pair_json, :source_revision_pair_json, "
            " :consent_snapshot_pair_json, :experiment_bucket, :display_eligible, "
            " :disclaimer, UTC_TIMESTAMP(), :expires_at, UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            " snapshot_id = VALUES(snapshot_id), "
            " status = VALUES(status), "
            " compatibility_index = VALUES(compatibility_index), "
            " coverage = VALUES(coverage), direction_json = VALUES(direction_json), "
            " reason_codes = VALUES(reason_codes), "
            " evidence_json = VALUES(evidence_json), "
            " profile_revision_pair_json = VALUES(profile_revision_pair_json), "
            " privacy_revision_pair_json = VALUES(privacy_revision_pair_json), "
            " source_revision_pair_json = VALUES(source_revision_pair_json), "
            " consent_snapshot_pair_json = VALUES(consent_snapshot_pair_json), "
            " calculated_at = VALUES(calculated_at), "
            " expires_at = VALUES(expires_at), "
            " invalidated_at = NULL, purge_after = NULL, "
            " updated_at = UTC_TIMESTAMP()"
        ),
        {
            "snapshot_id": snapshot_id,
            "viewer_user_id": int(viewer_id),
            "target_user_id": int(target_id),
            "algorithm_version": COMPATIBILITY_ALGORITHM_VERSION,
            "snapshot_hash": snapshot_hash,
            "status": result.status,
            "score_semantics": SCORE_SEMANTICS,
            "compatibility_index": (
                round(float(result.pair_score), 2)
                if ready and result.pair_score is not None
                else None
            ),
            "coverage": round(float(result.coverage), 4),
            "direction_json": json.dumps(directions) if directions else None,
            "reason_codes": json.dumps(list(result.reason_codes), ensure_ascii=False),
            "evidence_json": json.dumps(
                [ref.as_dict() for ref in evidence_refs], ensure_ascii=False
            ),
            "profile_revision_pair_json": json.dumps(
                {"viewer": viewer_rev.profile, "target": target_rev.profile},
                ensure_ascii=False,
            ),
            "privacy_revision_pair_json": json.dumps(
                {"viewer": viewer_rev.privacy, "target": target_rev.privacy},
                ensure_ascii=False,
            ),
            "source_revision_pair_json": json.dumps(
                {"viewer": viewer_rev.as_dict(), "target": target_rev.as_dict()},
                ensure_ascii=False,
            ),
            "consent_snapshot_pair_json": json.dumps(
                consent_pair, ensure_ascii=False
            ),
            "experiment_bucket": COMPATIBILITY_EXPERIMENT_BUCKET,
            "display_eligible": 0,
            "disclaimer": DISCLAIMER,
            "expires_at": expires_at,
        },
    )
    await db.flush()
    return str(snapshot_id)


# ----------------------------------------------------------------------
# 快照读取（每次重过门禁 + revision/过期 stale，§9.4）
# ----------------------------------------------------------------------

_SNAPSHOT_READ_COLUMNS = (
    "id, snapshot_id, viewer_user_id, target_user_id, algorithm_version, "
    "snapshot_hash, status, score_semantics, compatibility_index, coverage, "
    "direction_json, reason_codes, evidence_json, profile_revision_pair_json, "
    "privacy_revision_pair_json, source_revision_pair_json, "
    "consent_snapshot_pair_json, experiment_bucket, display_eligible, disclaimer, "
    "calculated_at, expires_at, invalidated_at, created_at"
)


async def _load_latest_snapshot(
    db: AsyncSession, viewer_id: int, target_id: int
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            f"SELECT {_SNAPSHOT_READ_COLUMNS} FROM ai_compatibility_snapshot "
            "WHERE viewer_user_id = :viewer_user_id "
            "AND target_user_id = :target_user_id "
            "AND algorithm_version = :algorithm_version "
            "ORDER BY id DESC LIMIT 1"
        ),
        {
            "viewer_user_id": int(viewer_id),
            "target_user_id": int(target_id),
            "algorithm_version": COMPATIBILITY_ALGORITHM_VERSION,
        },
    )
    return await _first_row(result)


async def _mark_snapshot_stale(
    db: AsyncSession, viewer_id: int, target_id: int
) -> None:
    await db.execute(
        text(
            "UPDATE ai_compatibility_snapshot SET status = 'stale', "
            "invalidated_at = UTC_TIMESTAMP() "
            "WHERE viewer_user_id = :viewer_user_id "
            "AND target_user_id = :target_user_id "
            "AND algorithm_version = :algorithm_version "
            "AND status NOT IN ('stale', 'blocked')"
        ),
        {
            "viewer_user_id": int(viewer_id),
            "target_user_id": int(target_id),
            "algorithm_version": COMPATIBILITY_ALGORITHM_VERSION,
        },
    )


def _snapshot_to_read(row: dict[str, Any], status: str) -> CompatibilitySnapshotRead:
    direction_json = _maybe_json(row.get("direction_json"))
    directions = None
    if status == CompatibilitySnapshotStatus.READY.value and isinstance(
        direction_json, dict
    ):
        directions = CompatibilityDirectionScores(
            viewer_to_target=float(direction_json.get("viewer_to_target") or 0.0),
            target_to_viewer=float(direction_json.get("target_to_viewer") or 0.0),
        )
    compatibility_index = row.get("compatibility_index")
    return CompatibilitySnapshotRead(
        snapshot_id=str(row["snapshot_id"]),
        status=CompatibilitySnapshotStatus(status),
        algorithm_version=str(
            row.get("algorithm_version") or COMPATIBILITY_ALGORITHM_VERSION
        ),
        score_semantics=str(row.get("score_semantics") or SCORE_SEMANTICS),
        compatibility_index=(
            float(compatibility_index)
            if compatibility_index is not None
            and status == CompatibilitySnapshotStatus.READY.value
            else None
        ),
        coverage=(
            float(row["coverage"]) if row.get("coverage") is not None else None
        ),
        directions=directions,
        reason_codes=list(_maybe_json(row.get("reason_codes")) or []),
        profile_revision_pair=_maybe_json(row.get("profile_revision_pair_json"))
        or {},
        privacy_revision_pair=_maybe_json(row.get("privacy_revision_pair_json"))
        or {},
        experiment_bucket=str(row.get("experiment_bucket") or "shadow"),
        display_eligible=bool(row.get("display_eligible")),
        disclaimer=str(row.get("disclaimer") or DISCLAIMER),
        calculated_at=row["calculated_at"],
        expires_at=row.get("expires_at"),
        evidence=_maybe_json(row.get("evidence_json")) or [],
    )


def _empty_coverage_insufficient() -> CompatibilitySnapshotRead:
    now = _now_utc()
    return CompatibilitySnapshotRead(
        snapshot_id="",
        status=CompatibilitySnapshotStatus.COVERAGE_INSUFFICIENT,
        algorithm_version=COMPATIBILITY_ALGORITHM_VERSION,
        score_semantics=SCORE_SEMANTICS,
        compatibility_index=None,
        coverage=None,
        directions=None,
        reason_codes=[REASON_COVERAGE],
        calculated_at=now,
        expires_at=now,
    )


async def read_compatibility_snapshot(
    db: AsyncSession, viewer_id: int, target_user_id: int
) -> CompatibilitySnapshotRead:
    """读取资料合拍参考：先硬门禁，再版本/过期 stale，blocked/不足不伪造分。

    硬门禁先于规则：denied 的 pair 统一 ``404 CANDIDATE_NOT_VISIBLE``，不泄露
    归属；版本/隐私变化或结果过期 → ``stale``（不能当最新解释）。
    """
    if int(viewer_id) == int(target_user_id):
        raise CandidateNotVisible()
    decision = await candidate_visibility_service.decide(
        db, viewer_id, target_user_id, VisibilityScene.PROFILE
    )
    if not decision.allowed:
        raise CandidateNotVisible()

    row = await _load_latest_snapshot(db, viewer_id, target_user_id)
    if row is None:
        return _empty_coverage_insufficient()

    stored_status = str(row.get("status") or CompatibilitySnapshotStatus.READY.value)
    if stored_status == CompatibilitySnapshotStatus.BLOCKED.value:
        # blocked 不展示候选：保留状态，分数置 None。
        return _snapshot_to_read(row, CompatibilitySnapshotStatus.BLOCKED.value)

    viewer_rev = await _load_revision_vector(db, viewer_id)
    target_rev = await _load_revision_vector(db, target_user_id)
    stored_profile_pair = _maybe_json(row.get("profile_revision_pair_json")) or {}
    stored_privacy_pair = _maybe_json(row.get("privacy_revision_pair_json")) or {}
    current_profile_pair = {"viewer": viewer_rev.profile, "target": target_rev.profile}
    current_privacy_pair = {"viewer": viewer_rev.privacy, "target": target_rev.privacy}
    stored_source_pair = _maybe_json(row.get("source_revision_pair_json")) or {}
    current_source_pair = {
        "viewer": viewer_rev.as_dict(),
        "target": target_rev.as_dict(),
    }
    stale_by_version = (
        stored_source_pair != current_source_pair
        or stored_profile_pair != current_profile_pair
        or stored_privacy_pair != current_privacy_pair
    )
    stale_by_expiry = _is_expired(row.get("expires_at"))

    if stale_by_version or stale_by_expiry:
        await _mark_snapshot_stale(db, viewer_id, target_user_id)
        return _snapshot_to_read(row, CompatibilitySnapshotStatus.STALE.value)
    if stored_status == CompatibilitySnapshotStatus.STALE.value:
        return _snapshot_to_read(row, CompatibilitySnapshotStatus.STALE.value)
    if not await _pair_consents_current(
        db,
        viewer_id,
        target_user_id,
        _maybe_json(row.get("consent_snapshot_pair_json")) or {},
    ):
        return _snapshot_to_read(row, CompatibilitySnapshotStatus.BLOCKED.value)
    current_projection_rows = await _load_current_projection_rows(
        db, viewer_id, target_user_id
    )
    expected_projection_keys = {
        (int(viewer_id), ProjectionKind.PERSONAL_COMPATIBILITY.value),
        (int(viewer_id), ProjectionKind.IDEAL_PARTNER_PREFERENCE.value),
        (int(target_user_id), ProjectionKind.PERSONAL_COMPATIBILITY.value),
        (int(target_user_id), ProjectionKind.IDEAL_PARTNER_PREFERENCE.value),
    }
    current_projection_keys = {
        (int(item["subject_user_id"]), str(item["projection_kind"]))
        for item in current_projection_rows
    }
    if not expected_projection_keys.issubset(current_projection_keys):
        return _snapshot_to_read(row, CompatibilitySnapshotStatus.BLOCKED.value)
    return _snapshot_to_read(row, stored_status)


# ----------------------------------------------------------------------
# recompute（§9.4）：可见性硬门禁 → 版本校验 → 入队 compatibility 任务
# ----------------------------------------------------------------------


def _hash_recompute_request(
    viewer_id: int,
    target_user_id: int,
    expected_viewer_profile_revision: int,
    expected_target_profile_revision: int,
) -> str:
    raw = json.dumps(
        {
            "viewer_user_id": int(viewer_id),
            "target_user_id": int(target_user_id),
            "expected_viewer_profile_revision": int(expected_viewer_profile_revision),
            "expected_target_profile_revision": int(expected_target_profile_revision),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def request_compatibility_recompute(
    db: AsyncSession,
    owner_user_id: int,
    target_user_id: int,
    expected_viewer_profile_revision: int,
    expected_target_profile_revision: int,
    idempotency_key: str,
) -> CompatibilityRecomputeAccepted:
    """请求重算 shadow（202 prediction+task）。

    硬门禁先于规则：不可见 → ``CANDIDATE_NOT_VISIBLE`` 404；expected revision
    与当前不符 → ``RESULT_STALE`` 409；``compatibility_shadow`` 授权缺失 →
    ``AI_CONSENT_REQUIRED`` 403。同 Idempotency-Key + 相同请求摘要回放既有任务。
    不 commit。
    """
    if int(owner_user_id) == int(target_user_id):
        raise CandidateNotVisible()
    decision = await candidate_visibility_service.decide(
        db, owner_user_id, target_user_id, VisibilityScene.PROFILE
    )
    if not decision.allowed:
        raise CandidateNotVisible()

    owner_rev = await _load_revision_vector(db, owner_user_id)
    target_rev = await _load_revision_vector(db, target_user_id)
    if (
        owner_rev.profile != int(expected_viewer_profile_revision)
        or target_rev.profile != int(expected_target_profile_revision)
    ):
        raise CompatibilityResultStale()

    viewer_consent = await _load_active_consent(
        db, owner_user_id, COMPATIBILITY_CONSENT_SCOPE
    )
    target_consent = await _load_active_consent(
        db, target_user_id, COMPATIBILITY_CONSENT_SCOPE
    )
    if viewer_consent is None or target_consent is None:
        raise CompatibilityConsentRequired()
    consent_snapshot = {
        "viewer": _consent_snapshot(viewer_consent),
        "target": _consent_snapshot(target_consent),
    }

    snapshot_id = f"cp_{uuid.uuid4().hex}"
    request_hash = _hash_recompute_request(
        owner_user_id,
        target_user_id,
        expected_viewer_profile_revision,
        expected_target_profile_revision,
    )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=COMPATIBILITY_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=owner_rev,
        consent=consent_snapshot,
    )
    existing_payload = task.payload_summary or {}
    if existing_payload.get("snapshot_id"):
        # Replayed concurrent request: the winner already committed the payload.
        # Re-write would take an X lock on the task row while sibling replays
        # hold S locks left by their duplicate-key INSERT — a deadlock cycle.
        snapshot_id = str(existing_payload["snapshot_id"])
    else:
        await db.execute(
            text(
                "UPDATE ai_task SET payload_summary = :payload_summary, "
                "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
            ),
            {
                "payload_summary": json.dumps(
                    {
                        "snapshot_id": snapshot_id,
                        "target_user_id": int(target_user_id),
                        "expected_viewer_profile_revision": int(
                            expected_viewer_profile_revision
                        ),
                        "expected_target_profile_revision": int(
                            expected_target_profile_revision
                        ),
                        "viewer_source_revision": owner_rev.as_dict(),
                        "target_source_revision": target_rev.as_dict(),
                        "consent_snapshot": consent_snapshot,
                    },
                    ensure_ascii=False,
                ),
                "task_id": task.task_id,
            },
        )
        await db.flush()
    expires_at = _now_utc() + timedelta(
        minutes=settings.ai_compatibility_snapshot_ttl_minutes
    )
    return CompatibilityRecomputeAccepted(
        snapshot_id=snapshot_id,
        task_id=task.task_id,
        status=task.status.value,
        poll_after_ms=1000,
        expires_at=expires_at,
    )


async def compatibility_execute_handler(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``compatibility`` Worker handler：重过门禁并写 shadow 快照。

    完成后由 ``complete_task`` 复核版本向量：任务期间 owner/target 版本变化 → 任务
    转 ``superseded``，旧结果不覆盖新状态。
    """
    payload = task.payload_summary or {}
    target_user_id = payload.get("target_user_id")
    snapshot_id = payload.get("snapshot_id")
    if not target_user_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    decision = await candidate_visibility_service.decide(
        db, task.owner_user_id, int(target_user_id), VisibilityScene.PROFILE
    )
    if not decision.allowed:
        # 重算期间目标不可见：任务按版本失效处理，不写 blocked 快照。
        await fail_task(
            db, task.task_id, worker_id,
            error_code="RESULT_STALE", retryable=False,
        )
        return None

    owner_rev = await _load_revision_vector(db, task.owner_user_id)
    target_rev = await _load_revision_vector(db, int(target_user_id))
    payload_viewer_revision = payload.get("viewer_source_revision")
    payload_target_revision = payload.get("target_source_revision")
    if (
        not isinstance(payload_viewer_revision, dict)
        or not isinstance(payload_target_revision, dict)
        or payload_viewer_revision != owner_rev.as_dict()
        or payload_target_revision != target_rev.as_dict()
    ):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="RESULT_STALE", retryable=False,
        )
        return None
    consent = task.consent_snapshot_json or payload.get("consent_snapshot") or {}
    if not await _pair_consents_current(
        db, task.owner_user_id, int(target_user_id), consent
    ):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="RESULT_STALE", retryable=False,
        )
        return None
    result_snapshot_id = await compute_and_write_shadow(
        db,
        task.owner_user_id,
        int(target_user_id),
        (owner_rev, target_rev),
        consent,
        snapshot_id=str(snapshot_id) if snapshot_id else None,
    )
    # 返回实时加载的 owner_rev（而非入队快照 payload_viewer_revision）作为
    # complete_task 的复核基准：让复核比对的是 handler 执行期间加载的版本，
    # 而非任务入队时的快照，缩小 TOCTOU 窗口。
    return f"compatibility-snapshot:{result_snapshot_id}", owner_rev


# ----------------------------------------------------------------------
# Worker handler 注册（Task 10 模式：模块导入时幂等注册）
# ----------------------------------------------------------------------


def register_compatibility_handlers() -> None:
    """把 ``compatibility`` 注册进 AI Worker 的 TASK_HANDLERS。

    模块导入（路由导入本模块）即生效；幂等，可在测试中重复调用。
    """
    from app.workers import ai_worker as worker_module

    worker_module.TASK_HANDLERS.setdefault(
        COMPATIBILITY_TASK_TYPE, compatibility_execute_handler
    )


register_compatibility_handlers()
