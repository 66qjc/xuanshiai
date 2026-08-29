"""实时语音对话回复生成的中文 prompt 构建器（voice reply）。

角色是"像朋友一样聊天的资料收集助手"：用户通过语音说了一句信息，
模型生成一句自然口语的回复——确认听到的信息，并自然地追问下一个
想了解的点。回复会经 TTS 播放，因此必须口语化、极简短。

安全边界：
- 输入含用户原始转写文本（仅本轮），输出为对话回复。
- 不编造用户未提供的信息；不输出清单/引号/markdown。
"""

from __future__ import annotations

from typing import Any

# 对话常用字段的中文语义（与画像抽取 field_key 对齐）。
_FIELD_LABELS = {
    "age": "年龄",
    "city_code": "所在城市",
    "marriage_status": "婚姻状况",
    "education_level": "学历",
    "height_cm": "身高",
    "income_band": "收入范围",
    "occupation_group": "职业",
    "interest_tags": "兴趣爱好",
    "lifestyle_tags": "生活方式",
    "relationship_goal": "择偶期望",
}

# 单轮回复长度上限（字符）：TTS 播放节奏约束，超长会导致用户等待感明显。
_REPLY_MAX_CHARS = 30


def _field_label(field_key: str) -> str:
    return _FIELD_LABELS.get(field_key, field_key)


def _known_fields_summary(known_fields: tuple[dict[str, Any], ...]) -> str:
    """把已抽取字段摘要渲染成 prompt 片段；空时给占位文案。"""
    if not known_fields:
        return "无"
    parts = []
    for item in known_fields[:10]:
        key = str(item.get("field_key", ""))
        value = str(item.get("value", ""))
        if key:
            parts.append(f"{_field_label(key)}={value}")
    return "、".join(parts) if parts else "无"


def build_voice_reply_prompt(
    transcript: str,
    field_key: str,
    known_fields: tuple[dict[str, Any], ...],
) -> str:
    """构建一轮语音对话的回复生成 prompt。

    追求极短、极快：提示词精简，约束回复 ≤30 字，减少模型生成时间。
    输出 JSON {"reply_text": "..."}（provider 要求 json_object 格式）。
    """
    current_focus = (
        f"{_field_label(field_key)}" if field_key else "自由聊"
    )
    return (
        f"用户说：「{transcript}」\n"
        f"已知道：{_known_fields_summary(known_fields)}\n"
        f"想了解：{current_focus}\n"
        f"回一句口语，≤{_REPLY_MAX_CHARS}字。接住信息+追问一个。无引号无列表无表情。\n"
        f'输出JSON: {{"reply_text":"你的回复"}}'
    )
