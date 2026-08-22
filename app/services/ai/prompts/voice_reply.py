"""实时语音对话回复生成的中文 prompt 构建器（voice reply）。

角色是"像朋友一样聊天的资料收集助手"：用户通过语音说了一句信息，
模型生成一句自然口语的回复——确认听到的信息，并自然地追问下一个
想了解的点。回复会经 TTS 播放，因此必须口语化、简短。

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

# 单轮回复长度上限（字符）：TTS 播放节奏约束，超长会被截断感明显。
_REPLY_MAX_CHARS = 50


def _field_label(field_key: str) -> str:
    return _FIELD_LABELS.get(field_key, field_key)


def _known_fields_summary(known_fields: tuple[dict[str, Any], ...]) -> str:
    """把已抽取字段摘要渲染成 prompt 片段；空时给占位文案。"""
    if not known_fields:
        return "暂无（用户还没提供任何信息）"
    parts = []
    for item in known_fields[:10]:
        key = str(item.get("field_key", ""))
        value = str(item.get("value", ""))
        if key:
            parts.append(f"{_field_label(key)}={value}")
    return "、".join(parts) if parts else "暂无"


def build_voice_reply_prompt(
    transcript: str,
    field_key: str,
    known_fields: tuple[dict[str, Any], ...],
) -> str:
    """构建一轮语音对话的回复生成 prompt。"""
    current_focus = (
        f"{_field_label(field_key)}（{field_key}）" if field_key else "自由聊"
    )
    return (
        "你是相亲平台的资料收集助手，任务是像朋友聊天一样自然地了解用户。"
        "用户刚刚通过语音说了一句话。\n\n"
        f"用户说：「{transcript}」\n"
        f"当前想了解的方面：{current_focus}\n"
        f"已经知道的：{_known_fields_summary(known_fields)}\n\n"
        "请生成一句回复，满足：\n"
        "1. 先简短确认/接住用户说的信息（不要逐字重复）；\n"
        "2. 再自然地追问一个还没了解到的方面；\n"
        "3. 口语化、友好、像朋友聊天；"
        f"不超过{_REPLY_MAX_CHARS}个字；不用引号、不用列表、不用表情。\n"
        '只输出 JSON：{"reply_text": "你的回复"}'
    )
