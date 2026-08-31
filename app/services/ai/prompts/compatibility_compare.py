"""双向匹配度精算（WP-C1b）的中文 prompt 构建器。

角色是"婚恋匹配分析师"：输入双方**已发布的画像投影摘要**（个人画像 +
理想型画像的结构化字段与条目摘要），输出双向适合概率（0-100）与每个
方向恰好 3 条中文可解释理由。

faithfulness 边界（对齐方案 §六 WP-C1 与工作区 LLM 纪律）：
- 只准基于"所给资料"判断；资料不足给低分并在理由中说明资料不足。
- 禁止编造双方未提供的信息，禁止对身份/健康/财产等敏感维度下判断。
- 输出仅限 JSON：{"viewer_to_target": {"score": 72, "reasons": ["…","…","…"]},
  "target_to_viewer": {…}}；score 为 0-100 整数，理由每条 ≤50 字、口语化。
"""

from __future__ import annotations

from app.services.ai.base import CompatibilityCompareRequest

_PROMPT_TEMPLATE = """你是一名严谨的婚恋匹配分析师。请根据下面两位用户的已确认画像资料，分别评估两个方向的互相适合概率（0-100 整数）。

【用户甲（查看者）的个人画像】
{viewer_personal}

【用户甲的理想型画像】
{viewer_ideal}

【用户乙（被查看者）的个人画像】
{target_personal}

【用户乙的理想型画像】
{target_ideal}

评分与理由要求：
1. "viewer_to_target" 表示"甲适合乙的概率"，"target_to_viewer" 表示"乙适合甲的概率"，各自独立评估。
2. 只准基于上面给出的资料判断；某方向资料不足时给低分，并在理由中说明"资料不足"。
3. 禁止编造任何双方未提供的信息；不要对收入数额、房产、健康、家庭背景等敏感细节做超出资料范围的推断。
4. 每个方向恰好输出 3 条中文理由，每条不超过 50 字，口语化、可直接解释给用户看，先说契合点再说主要差异。
5. 评分参考：价值观/生活规划/关系期待越一致分越高；仅有单一维度匹配时给低分。

只输出如下 JSON，不要输出任何其他文字：
{{"viewer_to_target": {{"score": 整数0-100, "reasons": ["理由1", "理由2", "理由3"]}}, "target_to_viewer": {{"score": 整数0-100, "reasons": ["理由1", "理由2", "理由3"]}}}}"""


def _section(title: str, fields: str, digest: str | None) -> str:
    """一个画像小节：结构化字段 + 条目摘要（无则注明"暂无条目"）。"""
    lines = [fields or "（暂无已确认的结构化字段）"]
    if digest and digest.strip():
        lines.append(f"补充条目：{digest.strip()}")
    else:
        lines.append("补充条目：暂无")
    return f"{title}\n" + "\n".join(lines)


def build_compatibility_compare_prompt(request: CompatibilityCompareRequest) -> str:
    """组装双向精算 prompt（faithfulness 约束内嵌，输出仅限 JSON）。"""
    return _PROMPT_TEMPLATE.format(
        viewer_personal=_section(
            "结构化字段：", request.viewer_personal, request.viewer_personal_digest
        ),
        viewer_ideal=_section(
            "结构化字段：", request.viewer_ideal, request.viewer_ideal_digest
        ),
        target_personal=_section(
            "结构化字段：", request.target_personal, request.target_personal_digest
        ),
        target_ideal=_section(
            "结构化字段：", request.target_ideal, request.target_ideal_digest
        ),
    )
