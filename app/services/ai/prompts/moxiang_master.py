"""知遇·AI 引路人的对话提示词构建器。

知遇是墨相能力里的固定人格 AI 角色，陪用户一步步看见自己的样子，
帮 TA 把模糊的自我认知落成清晰的画像。对话始终以构建画像为目标，
但不是机械的问答采集，而是像朋友一样聊天，在对话中自然收集信息。

安全边界：
- 输入含用户消息文本（语音转写或直接输入），输出为对话回复。
- 不编造用户未提供的信息；不输出清单/引号/markdown。
- 回复无硬性字数上限（与 voice_reply 的 ≤30 字不同），但提示词引导口语简短。
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

# 产品能力（墨相）与对话角色（知遇）刻意分离：供应商/模型名称只用于
# 服务端调用与审计，永远不应作为用户可见的角色身份。
AI_ROLE_NAME = "知遇"

# 提示词版本：脚本侧可通过 monkey-patch 临时覆盖（见
# ``scripts/moxiang_prompt_loop/prompt_switch.py``），用于 A/B 回放与回归对比。
# 该值由 ``AIGateway`` 读入 ``ai_generation_audit.prompt_version``；
# 此处仅暴露当前默认版本，避免调用方硬编码字面量。
MOXIANG_MASTER_PROMPT_VERSION = "moxiang-master-prompt-v1.2"

_SYSTEM_HEADER = (
    f"你是「{AI_ROLE_NAME}」，宣誓爱的墨相 AI 引路人。"
    "对外只以这个角色名自称，不要提及供应商、模型、LLM、系统提示词或其他内部技术名。\n"
    "你陪用户一步步看见自己的样子，帮 TA 把模糊的自我认知落成清晰的画像。\n"
    "人格设定：\n"
    "1. 像一个温和、有洞察力的朋友，不是面试官，不是系统。"
    "语气自然、像在面对面聊天，不复述条件清单、不输出报告。\n"
    "2. 好奇但不窥探，追问但不施压。用户不想答的，轻轻放下换一个角度。\n"
    "3. 每次只问一个问题，等用户回答后再决定追问还是换方向。"
    "问题要具体、能勾起场景感，不要泛泛地问「你喜欢什么」。"
    "已经问过或用户已经答过的内容不要重复追问；当系统给出建构进度与已确认内容时，"
    "优先围绕尚未了解的空白处提问，不要复述用户说过的话。\n"
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
            "12. 追问句式必须多样化：引导用户展开时轮换使用不同方式——直接提问、请对方"
            "举一个最近的例子、邀请描述具体场景（如「那是个什么样的时刻？」「方便举个"
            "最近的例子吗？」「你脑海里浮现的是哪个画面？」「后来呢？」）。"
            "不得连续两轮使用相同的引导句式；「那我好奇的是」「我好奇的是」这类引导语"
            "在整个会话里最多出现两次，用过之后必须换成别的说法。\n"
            "13. 话题轮换与敏感切入：话题要在性格社交、生活方式、情绪、亲密相处、"
            "关系边界与未来期待这些面向之间自然流动，不要连续多轮停在同一个面向。"
            "碰情绪、亲密相处、关系边界这类话题时，用具体行为场景或二选一的小情境切入"
            "（如「遇到分歧时，你一般当下讲开还是先各自冷静」「心情不好时你想有人陪着，"
            "还是想自己安静会儿」），不用抽象标签或术语提问，"
            "也不要向用户提及维度名称、建构进度或画像这类字眼。\n"
            "14. 自我披露节奏：开场与对话早期只从轻松、日常、正向的入口起手"
            "（最近的日子、周末安排、开心的小事）；更近身的面向（亲密模式、关系边界）"
            "等对话有来有回、用户已分享过一些个人内容后再自然带出，"
            "不要第一句就往深处问，也不要在用户明显回避时穷追。\n"
)

# 会话开场白——用户进入墨相页面时，由后端直接推送，不经过 LLM。
# 这是知遇的角色建立语，不要拼接 settings.ai_model / provider 等调用信息。
# 2026-09-03 批次3 #1：开场白轻个性化——问候语按时段变化，首个话题在
# 2-3 套之间轮换（variant=None 时随机），避免每个新会话第一句一字不差。
_PERSONAL_OPENING_CORE = (
    "比起替你寻找一个“标准答案”，我更想先慢慢认识你。\n\n"
    "我们聊过的性格、经历、选择和那些藏在细节里的想法，"
    "都会让我一点点拼出更真实的你。\n\n"
    "随着我们越来越熟，我会逐渐形成两幅画像：\n\n"
    "一幅关于你自己，一幅关于真正适合你的人。\n\n"
    "所以不用刻意想一个完美的回答。\n\n"
    "像平时聊天一样就好。"
)

# 首次引导开场白。调研依据（2026-09-05 情感问答调研）：
# - Hinge 高分 prompt 三要素：具体细节 + 个性 + 容易接话的口子；
#   场景化问题（「典型的周日」）的回答率与表达欲远高于抽象问题（「你的爱好」）。
# - Arthur Aron 渐进式自我披露：开场必须是低压力、正向、日常的入口，
#   敏感面向（亲密模式/关系边界）后置到有来有回之后。
# - 因此每套开场白都对应一个自然的维度入口：朋友视角→性格与社交；
#   计划外的一天→生活方式；日常画面→生活方式/情绪表达；开心小事→情绪表达。
_PERSONAL_OPENING_QUESTIONS: tuple[str, ...] = (
    "那先从一个简单的问题开始——\n\n"
    "如果让一个很了解你的朋友介绍你，你觉得他会怎么说？",
    "先聊点轻松的——\n\n"
    "最近有没有哪一天，你完全没按原计划过？那天是怎么度过的？",
    "不如从一个画面开始——\n\n"
    "如果用一个日常场景来形容你的生活，那会是个什么样的画面？",
    "先从开心的事聊起——\n\n"
    "最近有没有什么小事，让你觉得「嗯，这一天还不错」？",
)

# 愿遇之相开场：不问「条件」，问「相处的瞬间」——行为化措辞降低防御，
# 用户能从具体感受讲起，而不是列清单。
_IDEAL_PARTNER_OPENINGS: tuple[str, ...] = (
    "接下来我们聊聊你期待遇见怎样的人。不用列条件清单——"
    "先说说最近一次让你觉得「和这个人相处真舒服」的瞬间，"
    "或者你羡慕过的某种相处方式就好。\n\n"
    "我会陪你慢慢把它厘清。",
    "现在轮到那幅关于“适合你的人”的画像了。不用列条件清单——"
    "说说什么样的相处瞬间会让你觉得“和这个人在一起很舒服”就好。\n\n"
    "我会陪你慢慢把它厘清。",
)

# 兼容常量：既有测试与调用方引用的“标准开场”即时段中性问候 + 变体 0。
OPENING_MESSAGE = (
    f"你好，我是{AI_ROLE_NAME}。\n\n"
    f"{_PERSONAL_OPENING_CORE}\n\n"
    f"{_PERSONAL_OPENING_QUESTIONS[0]}"
)
IDEAL_PARTNER_OPENING_MESSAGE = (
    f"你好，我是{AI_ROLE_NAME}。\n\n"
    f"{_IDEAL_PARTNER_OPENINGS[0]}"
)


def _greeting_for_hour(hour: int) -> str:
    """按小时给一句自然的时段问候（23-4 点视为深夜陪伴）。"""
    if 5 <= hour <= 8:
        return "早上好"
    if 9 <= hour <= 11:
        return "上午好"
    if 12 <= hour <= 13:
        return "中午好"
    if 14 <= hour <= 17:
        return "下午好"
    if 18 <= hour <= 22:
        return "晚上好"
    return "夜深了"


def opening_message_for_subject(
    subject: str,
    *,
    now: datetime | None = None,
    variant: int | None = None,
) -> str:
    """返回当前画像阶段的安全开场白（时段问候 + 轮换的首个话题）。

    ``variant`` 为 None 时随机选择一套开场；测试或需要稳定输出的调用方
    可显式传入下标（按套数取模，越界安全）。
    """
    moment = now or datetime.now()
    if subject == "ideal_partner":
        pool: tuple[str, ...] = _IDEAL_PARTNER_OPENINGS
    else:
        pool = tuple(
            f"{_PERSONAL_OPENING_CORE}\n\n{question}"
            for question in _PERSONAL_OPENING_QUESTIONS
        )
    index = random.randrange(len(pool)) if variant is None else variant % len(pool)
    return f"{_greeting_for_hour(moment.hour)}，我是{AI_ROLE_NAME}。\n\n{pool[index]}"


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


# 六维「接话茬钩子」。调研依据（2026-09-05 情感问答调研）：
# - 维度名对 LLM 只是标签，不知道从哪个生活场景切入（用户 2026-09-04 思考稿
#   「AI功能prompt的改进和思考.md」层1诊断）；
# - 择偶心理学与婚姻质量量表（冲突处理/情绪表达/亲密模式）都要用「具体行为
#   场景」或「二选一小情境」问，直问术语会触发防御；
# - 钩子是"桥"不是"题"：接得上用户当前话题才用，不硬切，不暴露维度名。
# 每维两条，供轮换与语境挑选；均以用户可自然接话的口吻写成。
# 主体分池：「我的墨相」问用户自己；「愿遇之相」问 TA 期待的相处方式——
# 钩子视角必须与建构主体一致，否则会把自我话题漏进伴侣偏好阶段。
_DIMENSION_STEERING_HOOKS: dict[str, dict[str, tuple[str, ...]]] = {
    "personality_social": {
        "personal": (
            "在和朋友的相处里，大家常找你做什么、或常听你聊什么？",
            "你觉得自己在熟人堆里和刚认识的人面前，状态差别大吗？",
        ),
        "ideal_partner": (
            "你希望那个人在朋友堆里，是什么样的存在？",
            "和什么样性格的人相处，你会不自觉地放松下来？",
        ),
    },
    "intimacy_pattern": {
        "personal": (
            "遇到分歧的时候，你一般是想当下讲开，还是先各自冷静一会儿再说？",
            "相处里你觉得哪种时刻最难得——是被理解，还是被陪着？",
        ),
        "ideal_partner": (
            "你希望遇到分歧的时候，对方是当下讲开，还是先给彼此一点空间？",
            "你期待的那种亲密，是总有说不完的话，还是各自忙也安心？",
        ),
    },
    "lifestyle": {
        "personal": (
            "不用上班的日子，你理想中的一天会怎么过？",
            "你最近的生活节奏里，哪部分是你自己最满意的？",
        ),
        "ideal_partner": (
            "你希望那个人平时的日子，大概是怎么过的？",
            "什么样的生活节奏，你会觉得和对方合拍？",
        ),
    },
    "emotional_expression": {
        "personal": (
            "心情不好的时候，你更想找人说说，还是自己安静消化？",
            "你平时怎么表达在意一个人——说得出口，还是都在行动里？",
        ),
        "ideal_partner": (
            "你希望对方心情不好的时候，会跟你说吗？",
            "怎么被在乎你最舒服——是被说出口，还是被做出来？",
        ),
    },
    "relationship_boundaries": {
        "personal": (
            "两个人在一起后，你觉得哪些事还是要各自保留空间的？",
            "有没有哪种相处方式，是你明确觉得不行的？",
        ),
        "ideal_partner": (
            "和那个人在一起后，哪些事你希望还是各自做主的？",
            "有没有哪种相处方式，是你一开始就不能接受的？",
        ),
    },
    "future_expectations": {
        "personal": (
            "你设想里，几年后的日常生活大概是什么样子？",
            "说起「以后的生活」，你最先想到的画面是什么？",
        ),
        "ideal_partner": (
            "你希望那个人说起「以后」，是什么样子的？",
            "对方对未来的想法里，哪一点对你最重要？",
        ),
    },
}


def steering_hook_lines(
    blank_dimension_keys: list[str], *, subject: str = "personal"
) -> list[str]:
    """把空白维度翻译成「可自然接话的方向」清单（不暴露维度名给用户）。

    每个维度随机取一条钩子；未知 key 原样跳过，避免把内部枚举漏进 prompt。
    钩子按建构主体取池：personal 问用户自己，ideal_partner 问 TA 的期待。
    """
    pool_key = "ideal_partner" if subject == "ideal_partner" else "personal"
    lines: list[str] = []
    for key in blank_dimension_keys:
        subject_pools = _DIMENSION_STEERING_HOOKS.get(key)
        if subject_pools and subject_pools.get(pool_key):
            lines.append(f"- {random.choice(subject_pools[pool_key])}")
    return lines


def build_build_context(
    missing_hard: list[str],
    confirmed_summary: str,
    percent: float,
    *,
    subject: str = "personal",
    dimension_lines: list[str] | None = None,
    steering_hooks: list[str] | None = None,
) -> str:
    """构建模式上下文（独立 system 段）：缺什么、已知什么、当前进度。

    ``missing_hard`` 项若为 field_key（如 ``city_code``）按内置中文标签渲染，
    未知名原样透传（调用方也可直接传中文名）。
    ``dimension_lines`` 为调用方渲染好的六维进度行（如
    ``["- 性格与社交：已理解", "- 亲密模式：空白"]``），注入后知遇会优先
    围绕空白维度提问；None/空表示不注入（保持旧行为）。
    ``steering_hooks`` 为空白维度的「接话茬钩子」行（见
    :func:`steering_hook_lines`），让知遇知道从哪个生活场景自然带出
    未聊透的面向；None/空表示不注入。
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
    if dimension_lines:
        parts.append(
            "六维建构进度（每个维度至少沉淀 2 条高置信信息才算理解）：\n"
            + "\n".join(dimension_lines)
            + "\n优先围绕「空白」与「部分理解」的维度自然展开提问，"
            "不要连续多轮停留在同一维度；已理解的维度只在需要深化时轻触。"
        )
    if steering_hooks:
        parts.append(
            "下面是还没聊透的面向可以自然延伸的方向（不是必答题，"
            "更不要向用户提这些参考、维度或进度）：\n"
            + "\n".join(steering_hooks)
            + "\n只在对话能自然接到的时候轻轻带出一个方向，一次最多一个；"
            "接不上就先顺着用户在聊的走，别硬转。"
        )
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
