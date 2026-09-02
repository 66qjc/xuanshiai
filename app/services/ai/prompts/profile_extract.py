"""画像字段抽取的中文 prompt 构建器。

字段契约严格对齐 ``app/schemas/ai_profile.py`` 的
``PERSONAL_FACT_FIELD_KINDS`` / ``IDEAL_PARTNER_FIELD_KINDS``、
``PROFILE_ENUM_DICTIONARY`` 与 ``_RANGE_LIMITS``：prompt 里告诉模型的
字段类型和取值约束与后端校验完全一致，确保 DeepSeek 输出能通过
``normalize_profile_extracted_value`` 校验。
"""

from __future__ import annotations

from app.schemas.ai_profile import ProfileSubject

# 个人画像（personal）字段类型契约，与 PERSONAL_FACT_FIELD_KINDS 一致。
_PERSONAL_FIELD_GUIDE = {
    "age": "整数，年龄，范围 18-100",
    "city_code": "字符串，6 位行政区划代码，如 330100（杭州市）",
    "marriage_status": "枚举，取值 single / divorced / widowed",
    "education_level": "整数，学历等级 1-8。中文映射：1=初中及以下，2=高中/中专，3=大专，4=本科，5=硕士，6=博士",
    "height_cm": "整数，身高厘米数，范围 100-250",
    "income_band": "整数，收入档位 0 及以上。中文映射：0=无收入，1=一档/第一档，2=二档/第二档，3=三档，以此类推。区间表述如“一档到二档”取较低值 1",
    "occupation_group": "枚举，取值 technology / education / healthcare / finance / public_service / other",
    "interest_tags": "字符串数组，兴趣标签，如 [\"旅行\", \"看展\"]",
    "lifestyle_tags": "字符串数组，生活方式标签，如 [\"户外\"]",
    "relationship_goal": "枚举，取值 marriage / dating / friendship",
}

# 理想型画像（ideal_partner）字段类型契约，与 IDEAL_PARTNER_FIELD_KINDS 一致。
_IDEAL_PARTNER_FIELD_GUIDE = {
    "age": "区间，{\"min\": 最小年龄, \"max\": 最大年龄}，18-100",
    "city_code": "字符串数组，可接受的城市代码，如 [\"330100\", \"330200\"]",
    "marriage_status": "字符串数组，可接受的婚姻状态，single / divorced / widowed",
    "education_level": "区间，{\"min\": 最低学历, \"max\": 最高学历}，1-8；max 可为 null。中文映射：1=初中及以下，2=高中/中专，3=大专，4=本科，5=硕士，6=博士。用户说“本科以上”时 min=4",
    "height_cm": "区间，{\"min\": 最矮, \"max\": 最高}，100-250",
    "income_band": "区间，{\"min\": 最低档位, \"max\": 最高档位}，0 及以上；max 可为 null。中文映射：1=一档/第一档，2=二档/第二档，以此类推",
    "occupation_group": "字符串数组，可接受的行业，technology / education / healthcare / finance / public_service / other",
    "interest_tags": "字符串数组，期望兴趣标签",
    "lifestyle_tags": "字符串数组，期望生活方式标签",
    "relationship_goal": "字符串数组，期望关系目标，marriage / dating / friendship",
}

_SYSTEM_HEADER = (
    "你是一个严格的画像字段抽取器。你的任务是从用户的中文会话回答中，"
    "抽取预设字段表里出现的信息。你只能抽取下面列出的字段，"
    "不得猜测、不得编造、不得添加未列出的字段。"
    "不要处理手机号、身份证、精确地址、IP 等敏感信息——用户即使提到也要忽略。"
    "如果某个字段在会话中没有提及，就不要在输出里包含它。"
    "每条抽取结果必须附带 source_quote（用户原文中支撑该结论的片段）"
    "和 confidence（0 到 1 之间的浮点数，表示你对这次抽取的把握）。"
)

_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，根对象包含一个 \"fields\" 数组。"
    "示例：\n"
    "{\n"
    "  \"fields\": [\n"
    "    {\n"
    "      \"field_key\": \"interest_tags\",\n"
    "      \"value\": [\"旅行\", \"看展\"],\n"
    "      \"source_quote\": \"周末喜欢旅行和看展\",\n"
    "      \"confidence\": 0.91\n"
    "    }\n"
    "  ]\n"
    "}"
)

# WP-P1：条目（entry）抽取通道。faithfulness 是硬约束——条目只能是对用户
# 原话的归纳或原句摘录，禁止编造用户没有表达过的偏好、细节或推论。
_ENTRY_GUIDE = (
    "除 fields 外，你还可以输出 \"entries\" 数组，把无法落入预设字段、"
    "但能体现用户特点的原话归纳为自由条目。规则：\n"
    "  - category 只能取：basics（基本情况）/ occupation（工作状态）/ "
    "appearance（外形特征）/ personality（性格特征）/ values（价值观）/ "
    "interests（兴趣爱好）/ routine（作息习惯）/ diet（饮食习惯）/ "
    "life_plan（生活规划）；\n"
    "  - content 是对用户原话的紧凑归纳或原句摘录，1 到 200 字；\n"
    "  - 只准基于用户本轮原话归纳，禁止编造、引申或补充用户没有说过的内容；\n"
    "  - 同样必须附带 source_quote（用户原文片段）和 confidence；\n"
    "  - 用户没有表达可归纳内容时 entries 输出空数组。\n"
)

_ENTRY_JSON_EXAMPLE = (
    "  \"entries\": [\n"
    "    {\n"
    "      \"category\": \"values\",\n"
    "      \"content\": \"欣赏阳光开朗、品行端正的人\",\n"
    "      \"source_quote\": \"我喜欢阳光开朗品行端正的\",\n"
    "      \"confidence\": 0.88\n"
    "    }\n"
    "  ]"
)


def build_profile_extract_prompt(
    subject: str,
    turn_texts: tuple[str, ...],
    target_field_key: str | None = None,
) -> str:
    """构造画像抽取的完整 prompt（system + user 拼接为单段文本）。

    ``subject`` 为 ``personal``（个人画像）或 ``ideal_partner``（理想型画像），
    两套字段类型契约不同，prompt 会据此列出不同的字段说明。
    """
    is_personal = subject == ProfileSubject.PERSONAL.value
    field_guide = _PERSONAL_FIELD_GUIDE if is_personal else _IDEAL_PARTNER_FIELD_GUIDE
    subject_label = "个人画像" if is_personal else "理想型画像"

    field_lines = "\n".join(
        f"  - {key}：{desc}" for key, desc in field_guide.items()
    )

    turn_block = "\n\n".join(
        f"【第 {idx + 1} 轮回答】\n{text}"
        for idx, text in enumerate(turn_texts)
        if text
    )
    if not turn_block:
        turn_block = "（用户尚未提供回答）"

    target_block = ""
    if target_field_key and target_field_key in field_guide:
        target_block = (
            f"本轮用户正在回答字段 {target_field_key}"
            f"（{field_guide[target_field_key]}）。"
            "优先抽取该字段；用户用口语、生活细节或短句作答时，"
            "也要把能对应到该字段的信息写进 fields，不要因为不够正式就输出空数组。\n\n"
        )

    return (
        f"{_SYSTEM_HEADER}\n\n"
        f"当前抽取目标：{subject_label}。\n"
        f"可抽取的字段及其值格式：\n{field_lines}\n\n"
        f"{target_block}"
        f"{_JSON_FORMAT_INSTRUCTION}\n"
        "根对象还可以包含一个 \"entries\" 数组（可选），示例：\n"
        "{\n"
        f"{_ENTRY_JSON_EXAMPLE}\n"
        "}\n\n"
        f"{_ENTRY_GUIDE}\n\n"
        f"以下是用户的会话回答：\n{turn_block}"
    )


# WP-P4：对话式更新（update 会话）的澄清式 prompt。与建构抽取不同：模型
# 先判断「信息是否足以固化为条目」，不足则只提一个聚焦的澄清问题；足够则
# 直接产出 entry patch。faithfulness 是硬约束——只准基于用户陈述归纳。
_UPDATE_SYSTEM_HEADER = (
    "你是一个画像更新助手。用户会陈述对画像的新期望或新信息，"
    "你的任务是围绕这段陈述追问澄清，或把它固化为条目。规则：\n"
    "  - 只能基于用户陈述内容归纳，禁止编造、引申用户没有表达过的偏好或细节；\n"
    "  - 陈述信息不足、有歧义或有多种理解时，输出 clarifying_question：\n"
    "    只问一个问题，聚焦最关键的分歧点，不超过 80 字，不要堆叠多个问题；\n"
    "  - 陈述已经足够清晰时，输出 patches：每条含 action（add=新增/modify=改写\n"
    "    既有条目）、category、content（1 到 200 字）、replaces_field_key\n"
    "    （仅 modify 需要，指向被改写条目的 field_key）；\n"
    "  - 不要同时输出 clarifying_question 和 patches；\n"
    "  - category 只能取：basics（基本情况）/ occupation（工作状态）/ "
    "appearance（外形特征）/ personality（性格特征）/ values（价值观）/ "
    "interests（兴趣爱好）/ routine（作息习惯）/ diet（饮食习惯）/ "
    "life_plan（生活规划）。\n"
    "  - personal 只沉淀用户自己的事实、恋爱观与关系边界；ideal_partner（愿遇之相）"
    "只沉淀用户明确表达的伴侣偏好，不得跨主体写入；\n"
    "  - 用户仅描述现实中的具体对象时，不把对方人格判断当作事实或偏好，patches 必须"
    "为空；只有用户明确确认该特质代表自己的择偶偏好，例如说出‘我希望’、‘我看重’、"
    "‘我会被……吸引’，才允许生成愿遇之相候选。\n"
)

_UPDATE_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，根对象形如：\n"
    "{\n"
    "  \"clarifying_question\": \"偏向音乐、绘画还是舞蹈？\",\n"
    "  \"patches\": []\n"
    "}\n"
    "或\n"
    "{\n"
    "  \"clarifying_question\": null,\n"
    "  \"patches\": [\n"
    "    {\n"
    "      \"action\": \"add\",\n"
    "      \"category\": \"interests\",\n"
    "      \"content\": \"希望对方热爱艺术，愿意一起看展、听音乐会\",\n"
    "      \"source_quote\": \"希望对方是搞艺术的，能陪我看展\",\n"
    "      \"confidence\": 0.86\n"
    "    }\n"
    "  ]\n"
    "}"
)


# 设计 Task 6：墨相师对话建构（master 会话）的对话抽取 prompt。与 update 的
# 澄清式契约不同：澄清追问由对话中的墨相师承担，抽取器**禁止**输出
# clarifying_question；对话里没有可固化内容时允许 0 条 patch（空数组是合法
# 结果，不硬凑）。faithfulness 硬约束与 update 相同。
_MASTER_SYSTEM_HEADER = (
    "你是一个画像建构助手，正在陪用户自然对话，并从中沉淀画像条目候选。"
    "你的任务只有一个：把用户已经表达清晰的内容固化为条目。规则：\n"
    "  - 只能基于用户陈述内容归纳，禁止编造、引申用户没有表达过的偏好或细节；\n"
    "  - 对话里没有可固化内容时，patches 输出空数组——这是正常结果，"
    "不要硬凑、不要输出用户没有表达过的内容；\n"
    "  - personal（我的墨相）只描述用户自己的事实、性格、恋爱观、关系边界"
    "和生活方式；ideal_partner（愿遇之相）只描述用户明确表达的择偶偏好、"
    "理想人格和期待的相处方式，两个主体不得互相改写；\n"
    "  - 用户对现实中具体对象的观察（例如‘他很温柔’）不是用户偏好，必须"
    "输出空 patches；不得给第三方建立画像，也不得把第三方特征当成已确认偏好；\n"
    "  - 只有用户明确确认并表达‘我希望’‘我看重’‘我会被……吸引’等偏好时，"
    "才允许为愿遇之相生成 patch；含混表达仍输出空 patches，追问由墨相师负责；\n"
    "  - 禁止输出 clarifying_question（澄清追问由对话中的墨相师承担，"
    "你不负责提问），该字段必须为 null；\n"
    "  - 每条 patch 含 action（add=新增/modify=改写既有条目）、category、"
    "content（1 到 200 字）、replaces_field_key（仅 modify 需要，指向被改写"
    "条目的 field_key）；\n"
    "  - category 只能取：basics（基本情况）/ occupation（工作状态）/ "
    "appearance（外形特征）/ personality（性格特征）/ values（价值观）/ "
    "interests（兴趣爱好）/ routine（作息习惯）/ diet（饮食习惯）/ "
    "life_plan（生活规划）。\n"
)

_MASTER_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，根对象形如：\n"
    "{\n"
    "  \"clarifying_question\": null,\n"
    "  \"patches\": []\n"
    "}\n"
    "或\n"
    "{\n"
    "  \"clarifying_question\": null,\n"
    "  \"patches\": [\n"
    "    {\n"
    "      \"action\": \"add\",\n"
    "      \"category\": \"interests\",\n"
    "      \"content\": \"喜欢看展，周末常去美术馆\",\n"
    "      \"source_quote\": \"我喜欢看展，周末常去美术馆\",\n"
    "      \"confidence\": 0.9\n"
    "    }\n"
    "  ]\n"
    "}"
)


def build_profile_master_extract_prompt(
    subject: str,
    turn_texts: tuple[str, ...],
    entry_digest: str | None = None,
) -> str:
    """构造 master 会话对话抽取 prompt（设计 Task 6）。

    ``turn_texts`` 是本会话按时间顺序的用户陈述；``entry_digest`` 是该维度
    已发布条目摘要，供 modify patch 定位被改写条目，可为 None。契约：允许
    返回 0 条 patch，禁止返回澄清问题——澄清由墨相师对话承担。
    """
    is_personal = subject == ProfileSubject.PERSONAL.value
    subject_label = "个人画像" if is_personal else "理想型画像"
    dialogue_block = "\n\n".join(
        f"【第 {idx + 1} 句】\n{text}" for idx, text in enumerate(turn_texts) if text
    )
    if not dialogue_block:
        dialogue_block = "（用户尚未陈述）"
    digest_block = ""
    if entry_digest:
        digest_block = (
            f"\n该维度当前已发布的条目（modify 时 replaces_field_key 从中选取）：\n"
            f"{entry_digest}\n"
        )
    return (
        f"{_MASTER_SYSTEM_HEADER}\n\n"
        f"建构目标：{subject_label}。\n"
        f"{digest_block}\n"
        f"{_MASTER_JSON_FORMAT_INSTRUCTION}\n\n"
        f"以下是用户在本会话中的陈述：\n{dialogue_block}"
    )


def build_profile_update_clarify_prompt(
    subject: str,
    turn_texts: tuple[str, ...],
    entry_digest: str | None = None,
) -> str:
    """构造 update 会话单轮澄清 prompt（system + 对话 + JSON 契约）。

    ``turn_texts`` 是本会话按时间顺序的用户陈述/答复；``entry_digest`` 是该
    维度已发布条目摘要，供 modify patch 定位被改写条目，可为 None。
    """
    is_personal = subject == ProfileSubject.PERSONAL.value
    subject_label = "个人画像" if is_personal else "理想型画像"
    dialogue_block = "\n\n".join(
        f"【第 {idx + 1} 句】\n{text}" for idx, text in enumerate(turn_texts) if text
    )
    if not dialogue_block:
        dialogue_block = "（用户尚未陈述）"
    digest_block = ""
    if entry_digest:
        digest_block = (
            f"\n该维度当前已发布的条目（modify 时 replaces_field_key 从中选取；\n"
            f"本摘要不含 field_key，仅作语义参考，replaces_field_key 由系统按\n"
            f"语义最接近的既有条目回填）：\n{entry_digest}\n"
        )
    return (
        f"{_UPDATE_SYSTEM_HEADER}\n\n"
        f"更新目标：{subject_label}。\n"
        f"{digest_block}\n"
        f"{_UPDATE_JSON_FORMAT_INSTRUCTION}\n\n"
        f"以下是用户在本会话中的陈述：\n{dialogue_block}"
    )
