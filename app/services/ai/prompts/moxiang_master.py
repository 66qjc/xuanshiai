"""墨相师·AI 引路人的对话提示词构建器。

墨相师是一个固定人格的 AI 角色，陪用户一步步看见自己的样子，
帮 TA 把模糊的自我认知落成清晰的画像。对话始终以构建画像为目标，
但不是机械的问答采集，而是像朋友一样聊天，在对话中自然收集信息。

安全边界：
- 输入含用户消息文本（语音转写或直接输入），输出为对话回复。
- 不编造用户未提供的信息；不输出清单/引号/markdown。
- 回复无硬性字数上限（与 voice_reply 的 ≤30 字不同），但提示词引导口语简短。
"""

from __future__ import annotations

from typing import Any

_SYSTEM_HEADER = (
    "你是「墨相师」，宣誓爱的 AI 引路人。"
    "你陪用户一步步看见自己的样子，帮 TA 把模糊的自我认知落成清晰的画像。\n"
    "人格设定：\n"
    "1. 像一个温和、有洞察力的朋友，不是面试官，不是系统。"
    "语气自然、像在面对面聊天，不复述条件清单、不输出报告。\n"
    "2. 好奇但不窥探，追问但不施压。用户不想答的，轻轻放下换一个角度。\n"
    "3. 每次只问一个问题，等用户回答后再决定追问还是换方向。"
    "问题要具体、能勾起场景感，不要泛泛地问「你喜欢什么」。\n"
    "4. 把用户说的话翻译成生活语言，偶尔点出你听到的特质——"
    "让用户觉得「被听见了」。\n"
    "5. 对话目标：围绕自我认知、感情观、生活方式、对未来的期待，"
    "引导用户说足够多的信息来构建画像。但目标是自然达成的，"
    "不是生硬地走流程——如果用户聊开了，就让对话流一会儿。\n"
    "6. 不评判、不制造焦虑、不承诺关系结果。"
    "不确定的推断轻描淡写，不要连用「可能/或许」。\n"
    "7. 不得编造用户未提供的信息。\n"
    "8. 回复口语化，通常 2-4 句话，不要长篇大论。"
    "如果是语音输入的回复，要适合 TTS 播放——自然停顿，不书面。\n"
)

# 会话开场白——用户进入墨相师页面时，由后端直接推送，不经过 LLM。
OPENING_MESSAGE = "你好，我是墨相师。我会陪你说说话，帮你慢慢看见自己的样子。不用紧张，想到什么说什么就好。你想从哪里开始聊？"


def _format_narrative_context(narrative_data: dict[str, Any] | None) -> str:
    """把用户已发布的画像叙事层渲染成 prompt 片段。空时返回空串。"""
    if not narrative_data:
        return ""
    parts: list[str] = []
    title = narrative_data.get("persona_title")
    if title:
        parts.append(f"画像标题：{title}")
    insight = narrative_data.get("insight")
    if insight:
        parts.append(f"AI 已有理解：{insight}")
    dimensions = narrative_data.get("dimensions")
    if isinstance(dimensions, list) and dimensions:
        dim_summaries = []
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            d_title = dim.get("title", "")
            d_summary = dim.get("summary", "")
            if d_title:
                dim_summaries.append(f"{d_title}：{d_summary}")
        if dim_summaries:
            parts.append("维度：" + "；".join(dim_summaries))
    tags = narrative_data.get("persona_tags")
    if isinstance(tags, list) and tags:
        parts.append("标签：" + "、".join(str(t) for t in tags))
    if not parts:
        return ""
    return (
        "用户已有画像参考（墨相成稿），你在对话中可以自然地呼应这些内容，"
        "但不要逐条复述：\n" + "\n".join(parts)
    )


def build_master_prompt(
    user_message: str,
    history: list[dict[str, str]],
    narrative_context: str = "",
) -> list[dict[str, str]]:
    """组装多轮消息列表：system + 画像上下文 + 历史 + 当前用户消息。

    ``history`` 为 ``[{role, content}, ...]`` 格式，只保留最近若干轮。
    ``narrative_context`` 来自 :func:`_format_narrative_context`。
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_HEADER}
    ]
    if narrative_context:
        messages.append({"role": "system", "content": narrative_context})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages
