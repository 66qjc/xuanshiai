"""三类推荐读取端出参（WP-P6e）。

卡片只携带分数、理由码与对方用户 ID：对方画像原文绝不经推荐接口下发，
基本资料/认证标由前端用既有候选名片接口按 target_user_id 拼装（边界见
PRODUCT.md 阶段3章节）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

RecommendationView = Literal["i_like", "likes_me", "similar"]


class RecommendationCard(BaseModel):
    """一张推荐卡：score/coverage 量纲与 compatibility 引擎一致（0..100 / 0..1）。"""

    target_user_id: int
    score: float | None = None
    coverage: float | None = None
    rank_no: int
    engine: str = "rule-v1"
    reason_codes: list[str] = []
    reason_texts: list[str] = []


class RecommendationPage(BaseModel):
    """推荐页读取结果：``regenerating`` 表示快照缺失/过期且已触发后台重建。"""

    view: RecommendationView
    items: list[RecommendationCard] = []
    regenerating: bool = False
