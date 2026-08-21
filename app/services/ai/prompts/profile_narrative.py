"""画像叙事层（narrative）的中文 prompt 构建器。

与 ``profile_extract.py`` 的抽取器角色不同，这里的角色是"画像分析师"：
输入是用户**已确认的结构化字段**（不是原始回答），任务是基于这些字段
综合生成人格画像解读——包括人格标题、标签、AI 洞察、维度卡片、
理想型权重和最近变化趋势。

安全边界：
- 可以基于已有字段做合理综合与解读，但不得编造用户未提供的信息。
- 输出是展示性的画像成品，不参与匹配/搜索/兼容度计算。
- 输入只含 ``field_key`` + ``display_value``，不含原始回答文本或用户 ID。
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.ai_profile import ProfileSubject

# 维度定义（personal 和 ideal_partner 共用同一套维度卡片）
_DIMENSIONS = {
    "relationship": ("♡", "感情观"),
    "personality": ("☀", "性格"),
    "lifestyle": ("⌂", "生活方式"),
    "future": ("↗", "人生规划"),
}

# ideal_partner 期望权重维度
_IDEAL_WEIGHT_KEYS = {
    "values": "价值观",
    "communication": "沟通方式",
    "emotion": "情绪稳定",
    "lifestyle": "生活节奏",
    "appearance": "外在条件",
}

# 字段语义说明——display_value 可能是编码值（如 education_level=4），
# 在 prompt 里告诉模型每个字段的含义和取值映射，避免误解。
_FIELD_SEMANTICS = {
    "age": "年龄（整数，如28表示28岁）",
    "city_code": "城市行政区划代码（如330100=杭州市，110100=北京市）",
    "marriage_status": "婚姻状态（single=未婚，divorced=离异，widowed=丧偶）",
    "education_level": "学历等级（1=初中及以下，2=高中/中专，3=大专，4=本科，5=硕士，6=博士）",
    "height_cm": "身高厘米数（如172表示172cm）",
    "income_band": "收入档位（0=无收入，1=第一档/最低，数字越大收入越高）",
    "occupation_group": "职业行业（technology=互联网/技术，education=教育，healthcare=医疗，"
    "finance=金融，public_service=公共服务，other=其他）",
    "interest_tags": "兴趣标签数组",
    "lifestyle_tags": "生活方式标签数组",
    "relationship_goal": "关系期待（marriage=结婚，dating=恋爱，friendship=交友）",
}

_SYSTEM_HEADER = (
    "你是一个专业的人格画像分析师。你的任务是基于用户已确认的结构化画像字段，"
    "综合生成一份人格画像解读。你可以对已有信息做合理的综合与解读，"
    "但不得编造用户未提供的信息，不得添加主观评判或道德判断。"
    "你的语气应该温暖、真诚、客观，像一个懂用户的知心朋友。"
    "输出必须是 JSON 格式，不要输出任何 JSON 之外的内容。"
)

_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，结构如下：\n"
    "{\n"
    '  "persona_title": "一句话人格概括，不超过20字",\n'
    '  "persona_tags": ["标签1", "标签2", "标签3"],\n'
    '  "insight": "一段50-150字的AI洞察，综合描述这个人",\n'
    '  "dimensions": [\n'
    '    {"key": "relationship", "icon": "♡", "title": "感情观", '
    '"summary": "20-60字的维度解读"},\n'
    '    {"key": "personality", "icon": "☀", "title": "性格", '
    '"summary": "..."},\n'
    '    {"key": "lifestyle", "icon": "⌂", "title": "生活方式", '
    '"summary": "..."},\n'
    '    {"key": "future", "icon": "↗", "title": "人生规划", '
    '"summary": "..."}\n'
    "  ],\n"
    '  "ideal_weights": [],\n'
    '  "recent_change": null,\n'
    '  "history_observations": []\n'
    "}\n\n"
    "字段说明：\n"
    '- persona_title：一句话概括用户的人格特质，如"慢热但真诚的长期主义者"。\n'
    '- persona_tags：3-5个性格标签词。\n'
    '- insight：综合所有字段，用50-150字描述这个人的核心特质。\n'
    '- dimensions：固定4个维度（感情观/性格/生活方式/人生规划），'
    "每个维度给出20-60字的解读。\n"
    "- ideal_weights：仅当抽取目标为「理想型画像」时填写，"
    "按 5 个维度（价值观/沟通方式/情绪稳定/生活节奏/外在条件）"
    "给出 0-100 的权重百分比，表示用户更看重什么。"
    "个人画像时返回空数组 []。\n"
    "- recent_change：如果有上一版字段可对比，分析用户相比上一次的变化趋势；"
    "首次发布（无上一版）时返回 null。\n"
    "- history_observations：历史观察记录，每条包含 revision_id、"
    "keywords（2-4个关键词）和 observation（30-100字观察）。"
    "如果没有历史数据，返回空数组 []。"
)


def _fields_to_block(fields: tuple[dict[str, Any], ...]) -> str:
    """把字段列表渲染成 prompt 里的可读文本块（带语义说明）。"""
    if not fields:
        return "（暂无字段）"
    lines: list[str] = []
    for f in fields:
        key = str(f.get("field_key", ""))
        display = str(f.get("display_value") or f.get("value") or "")
        if not display:
            continue
        semantics = _FIELD_SEMANTICS.get(key)
        if semantics:
            lines.append(f"  - {key}（{semantics}）：{display}")
        else:
            lines.append(f"  - {key}：{display}")
    return "\n".join(lines) if lines else "（暂无字段）"


def _history_to_block(
    history: tuple[dict[str, Any], ...],
) -> str:
    """把历史 revision 摘要渲染成 prompt 里的文本块。"""
    if not history:
        return "（无历史版本，这是首次发布）"
    lines: list[str] = []
    for h in history:
        rev_id = h.get("revision_id", "?")
        fields = h.get("fields")
        if isinstance(fields, (list, tuple)):
            field_str = ", ".join(
                f'{f.get("field_key")}={f.get("display_value") or f.get("value")}'
                for f in fields
                if isinstance(f, dict)
            )
        else:
            field_str = str(fields or "")
        lines.append(f"  - 版本{rev_id}：{field_str}")
    return "\n".join(lines)


def build_profile_narrative_prompt(
    subject: str,
    current_fields: tuple[dict[str, Any], ...],
    previous_fields: tuple[dict[str, Any], ...],
    history_summaries: tuple[dict[str, Any], ...],
) -> str:
    """构造画像叙事层的完整 prompt。

    ``subject`` 为 ``personal``（个人画像）或 ``ideal_partner``（理想型画像）。
    ``current_fields`` 是本次发布的已确认字段。
    ``previous_fields`` 是上一次发布的字段（首次为空元组）。
    ``history_summaries`` 是历史 revision 摘要列表。
    """
    is_personal = subject == ProfileSubject.PERSONAL.value
    subject_label = "个人画像" if is_personal else "理想型画像"

    current_block = _fields_to_block(current_fields)
    previous_block = _fields_to_block(previous_fields)
    history_block = _history_to_block(history_summaries)

    has_previous = "有上一版本可对比" if previous_fields else "这是首次发布，没有上一版本"

    return (
        f"{_SYSTEM_HEADER}\n\n"
        f"当前画像目标：{subject_label}。\n"
        f"历史状态：{has_previous}。\n\n"
        f"当前版本已确认字段：\n{current_block}\n\n"
        f"上一版本字段（用于变化趋势分析）：\n{previous_block}\n\n"
        f"历史版本摘要：\n{history_block}\n\n"
        f"{_JSON_FORMAT_INSTRUCTION}\n\n"
        f"注意：如果这是首次发布（没有上一版本），recent_change 返回 null。"
        f"如果是理想型画像，请填写 ideal_weights。"
        f"如果是个人画像，ideal_weights 返回空数组。"
    )


def serialize_fields_for_prompt(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """把数据库行转成 prompt 安全的 field dict 元组。

    只保留 field_key 和 display_value，不含原始回答文本或用户 ID。
    """
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "field_key": str(row.get("field_key", "")),
                "display_value": str(row.get("display_value") or row.get("value_json") or ""),
            }
        )
    return tuple(result)
