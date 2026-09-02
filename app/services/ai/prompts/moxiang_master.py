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
    "9. 话题白名单：只围绕用户的自我认知、三观与感情观、生活方式与作息饮食、"
    "物理位置（城市/居住地）、基本情况（年龄/婚姻状态/学历/职业/身高/收入）提问"
    "与展开。用户把话题带到白名单之外时，不接话、不说教，温和地拉回："
    "先用一句话承认对方说的内容，再自然地把话题引回白名单。"
    "若用户明确拒绝回答某项，轻轻放下换角度，不再追问同一项。\n"
    "10. 双画像阶段边界：当前阶段由系统明确指定为「我的墨相」或「愿遇之相」。"
    "在「我的墨相」阶段，只讨论并沉淀用户自己的性格、恋爱观、关系边界和生活方式；"
    "在「愿遇之相」阶段，只讨论并沉淀用户明确表达的伴侣偏好和期待的相处方式。"
    "不要在一句话里自行猜测并同时写入两个主体。\n"
    "11. 用户提到现实中的具体对象时，可以陪用户理解自己的感受，但不要把对方的"
    "人格判断当成事实，也不要默认写入「愿遇之相」。先追问这是否代表用户自己的偏好；"
    "只有用户明确确认「这是我希望未来伴侣具备的特质」后，才把话题转为偏好表达。\n"
)

# 会话开场白——用户进入墨相师页面时，由后端直接推送，不经过 LLM。
OPENING_MESSAGE = "你好，我是墨相师。我会陪你说说话，帮你慢慢看见自己的样子。不用紧张，想到什么说什么就好。你想从哪里开始聊？"
IDEAL_PARTNER_OPENING_MESSAGE = (
    "接下来我们聊聊你期待遇见怎样的人。可以从让你心动的性格、相处时的感受，"
    "或者你看重的关系方式说起。"
)


def opening_message_for_subject(subject: str) -> str:
    """返回当前画像阶段的安全开场白。"""
    if subject == "ideal_partner":
        return IDEAL_PARTNER_OPENING_MESSAGE
    return OPENING_MESSAGE


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


# 缺失基础信息的 field_key → 中文提示（与 voice_reply 的 fieldLabel 语义对齐）。
# 未知 key 原样透传：调用方也可在传入前自行格式化成中文名。
_MISSING_FIELD_LABELS = {
    "age": "年龄",
    "city_code": "所在城市",
    "marriage_status": "婚姻状况",
    "education_level": "学历",
    "height_cm": "身高",
    "income_band": "收入范围",
    "occupation_group": "职业",
}


def _missing_label(field_key: str) -> str:
    return _MISSING_FIELD_LABELS.get(field_key, field_key)


def build_build_context(
    missing_hard: list[str],
    confirmed_summary: str,
    percent: float,
    *,
    subject: str = "personal",
) -> str:
    """构建模式上下文（独立 system 段）：缺什么、已知什么、当前进度。

    ``missing_hard`` 项若为 field_key（如 ``city_code``）按内置中文标签渲染，
    未知名原样透传（调用方也可直接传中文名）。
    """
    subject_label = "愿遇之相" if subject == "ideal_partner" else "我的墨相"
    subject_rule = (
        "本阶段只沉淀用户明确表达的伴侣偏好；具体对象的未经确认观察不写入画像。"
        if subject == "ideal_partner"
        else "本阶段只沉淀用户自己的事实、恋爱观和关系边界；伴侣偏好留到愿遇之相阶段。"
    )
    parts = [
        f"当前建构主体：{subject_label}。{subject_rule}",
        "当前处于画像建构模式（对话目标：自然收集齐用户画像），进度约 "
        f"{percent:.0f}%。",
    ]
    if missing_hard:
        parts.append("还缺少的基础信息：" + "、".join(
            _missing_label(key) for key in missing_hard
        ) + "。在对话自然处把它们问出来，一次只问一个，不要像审表。")
    else:
        parts.append("基础信息已齐，继续丰富生活方式与三观类内容。")
    if confirmed_summary:
        parts.append("已确认的画像内容（呼应即可，不要复述）：\n" + confirmed_summary)
    return "\n".join(parts)


def build_master_prompt(
    user_message: str,
    history: list[dict[str, str]],
    narrative_context: str = "",
    build_context: str = "",
) -> list[dict[str, str]]:
    """组装多轮消息列表：system + 画像上下文 + 构建上下文 + 历史 + 当前用户消息。

    ``history`` 为 ``[{role, content}, ...]`` 格式，只保留最近若干轮。
    ``narrative_context`` 来自 :func:`_format_narrative_context`。
    ``build_context`` 来自 :func:`build_build_context`，空串表示纯聊模式（不注入）。
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_HEADER}
    ]
    if narrative_context:
        messages.append({"role": "system", "content": narrative_context})
    if build_context:
        messages.append({"role": "system", "content": build_context})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages
