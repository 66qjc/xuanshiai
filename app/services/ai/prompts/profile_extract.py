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
    # 产品口径（PRODUCT.md 收入档位）：以个人月收入为准，区间左闭右开。
    "income_band": (
        "整数，收入档位 0-6。按个人月收入划分：0=无收入/暂不固定，"
        "1=5千以下，2=5千-1万，3=1万-2万，4=2万-3万5，5=3万5-5万，6=5万以上。"
        "边界值归较高档（左闭右开）：“月入两万”“月薪2w”归 4 档，“一万五”归 3 档；"
        "区间表述如“一万到两万”取较低档 3"
    ),
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

# category 白名单与口语线索词。调研依据（2026-09-05 情感问答调研）：用户描述
# 自己时大量使用口语词（社恐、慢热、i人/e人、嘴硬心软、粘人、报备、冷处理、
# 奔着结婚），只给书面类名会让抽取器漏掉或错归这些表达；括号内同时给书面义
# 与常见口语线索。三个抽取 header（建构/更新/条目）共用此常量，避免漂移。
_CATEGORY_WHITELIST = (
    "category 只能取：basics（基本情况：年龄、身高、婚姻状况、在哪个城市）/ "
    "occupation（工作状态：职业、行业、加班、忙不忙）/ "
    "appearance（外形特征：长相、身材、穿搭）/ "
    "personality（性格特征：内向、外向、社恐、慢热、自来熟、话多话少、直性子）/ "
    "values（价值观：看重什么、介意什么、金钱观、底线）/ "
    "interests（兴趣爱好：周末安排、游戏、追剧、运动、旅行、看展）/ "
    "routine（作息习惯：早睡、夜猫子、宅家、健身、养宠物）/ "
    "diet（饮食习惯：口味、吃辣、自己做饭、点外卖）/ "
    "life_plan（生活规划：打算、想安定、换城市、考公考编、安家）"
)

# WP-P1：条目（entry）抽取通道。faithfulness 是硬约束——条目只能是对用户
# 原话的归纳或原句摘录，禁止编造用户没有表达过的偏好、细节或推论。
_ENTRY_GUIDE = (
    "除 fields 外，你还可以输出 \"entries\" 数组，把无法落入预设字段、"
    "但能体现用户特点的原话归纳为自由条目。规则：\n"
    f"  - {_CATEGORY_WHITELIST}；\n"
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
    f"  - {_CATEGORY_WHITELIST}。\n"
    "  - personal 只沉淀用户自己的事实、恋爱观与关系边界；ideal_partner（愿遇之相）"
    "只沉淀用户明确表达的伴侣偏好，不得跨主体写入；\n"
    "  - 用户仅描述现实中的具体对象时，不把对方人格判断当作事实或偏好，patches 必须"
    "为空；只有用户明确确认该特质代表自己的择偶偏好，例如说出‘我希望’、‘我看重’、"
    "‘我会被……吸引’，或口语变体‘我喜欢’、‘我吃这一套’、‘受不了不……的’、"
    "‘找对象就得找……的’，才允许生成愿遇之相候选。\n"
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
# clarifying_question；对话里没有可固化内容时允许 0 条 field / patch（空数组
# 是合法结果，不硬凑）。``fields`` 让用户明确陈述的白名单事实能进入后续的
# 确认—发布链；``patches`` 继续承载六维自由条目。faithfulness 硬约束与 update
# 相同。
_MASTER_SYSTEM_HEADER = (
    "你是一个画像建构助手，正在陪用户自然对话，并从中沉淀可确认字段和画像条目候选。"
    "你的任务只有一个：把用户已经表达清晰的内容固化为字段或条目。规则：\n"
    "  - 只能基于用户陈述内容归纳，禁止编造、引申用户没有表达过的偏好或细节；\n"
    "  - 对话里没有可固化内容时，fields 和 patches 输出空数组——这是正常结果，"
    "不要硬凑、不要输出用户没有表达过的内容；\n"
    "  - 覆盖优先：先逐句扫描用户本轮原话，把每个有原文支撑的最小独立事实列出来，"
    "再逐项决定是否输出；不要只抓一句话里最显眼的一个事实。\n"
    "  - 拆分规则：一条 patch 只表达一个事实；同一句可以输出多条 patch。若一句话同时"
    "包含社交风格、情绪表达、生活习惯或长期期待，分别沉淀，不要因已经命中一个维度"
    "而遗漏其余事实。\n"
    "  - personal（我的墨相）只描述用户自己的事实、性格、恋爱观、关系边界"
    "和生活方式；ideal_partner（愿遇之相）只描述用户明确表达的择偶偏好、"
    "理想人格和期待的相处方式，两个主体不得互相改写；\n"
    "  - 用户对现实中具体对象的观察（例如‘他很温柔’）不是用户偏好，必须"
    "输出空 patches；不得给第三方建立画像，也不得把第三方特征当成已确认偏好；\n"
    "  - 从具体关系经历中总结出的自我模式可以抽取，但 content 只能写用户明确说出的"
    "自身认识或相处模式，不能写第三方的人格。例如用户由一段经历明确总结‘职业相同"
    "不等于合拍，我更在意性格互动’，可低置信沉淀亲密模式。\n"
    "  - 只有用户明确确认并表达‘我希望’‘我看重’‘我会被……吸引’等偏好时，"
    "才允许为愿遇之相生成 patch；含混表达仍输出空 patches，追问由墨相师负责；\n"
    "  - 禁止输出 clarifying_question（澄清追问由对话中的墨相师承担，"
    "你不负责提问），该字段必须为 null；\n"
    "  - fields 只收集下方字段表中被用户明确陈述的事实；字段表外、手机号、"
    "身份证、精确地址等敏感信息绝不输出；每项必须有 field_key、value、"
    "source_quote 和 confidence；\n"
    "  - 每条 patch 含 action（add=新增/modify=改写既有条目）、category、"
    "content（1 到 200 字）、replaces_field_key（仅 modify 需要，指向被改写"
    "条目的 field_key）；\n"
    f"  - {_CATEGORY_WHITELIST}。\n"
    "  - 六维归属：每条 patch 会被系统归入六个维度之一——性格与社交、亲密模式、"
    "生活方式、情绪表达、关系边界、未来期待。先按下列语义优先级选 category，再写"
    "content，不要被同一句里的通用词带偏：\n"
    "    · 情绪表达优先归入 emotional_expression：讲表达爱意或感受、倾听和安慰时用"
    "personality，并在 content 保留「表达/倾听/安慰/情绪」等语义；口语线索如"
    "「嘴硬心软」「不会哄人」「报喜不报忧」「情绪稳定」「憋在心里」。例如「不擅长"
    "甜言蜜语但会用行动表达」「先倾听、后给建议」；\n"
    "    · 长期关系愿景优先归入 future_expectations：出现三五年、十年后、安定生活、"
    "婚姻里成为队友、理想日常等长期画面时用 life_plan；口语线索如「奔着结婚」"
    "「想安定下来」「安家」「搭伙过日子」「以后的家」「老了以后」；即使同时提到"
    "沟通、城市或生活方式，也不要改归 values 或 routine；\n"
    "    · 个人空间、粘人程度、忠诚底线和不可接受事项优先归入关系边界：用 values，"
    "content 写清「个人空间/边界/底线」；口语线索如「报备」「翻手机」「查岗」"
    "「各自的圈子」「别管太多」；不要自行补入「亲密/陪伴」等相处词；\n"
    "    · 讲沟通方式、冲突处理、陪伴与亲密相处，用 values 并带「沟通/冲突/陪伴/"
    "亲密」等词，归入亲密模式；口语线索如「冷处理」「冷战」「哄」「秒回」"
    "「仪式感」「安全感」「异地恋」；讲底线与原则（如「不能接受欺骗」）用 values "
    "但不带相处词，归入关系边界；\n"
    "    · 作息、饮食、兴趣、消费与日常用 routine/diet/interests/occupation，归入"
    "生活方式；口语线索如「宅家」「夜猫子」「点外卖」「养宠物」「健身」；人生与"
    "关系规划用 life_plan，归入未来期待；\n"
    "    · 性格本身与社交风格（内向/外向/幽默）用 personality/basics/appearance "
    "且不带情绪词，归入性格与社交；口语线索如「社恐」「慢热」「自来熟」"
    "「i人/e人」「话痨」「社牛」。\n"
    "  - 内隐线索必收：情绪表达、亲密模式、关系边界、未来期待这类面向常藏在生活"
    "叙述里——用户描述怎么消化坏心情、和谁闹过分歧、一个人时的习惯、对以后生活"
    "的想象，都是相关证据，务必按上面的语义归属沉淀，不要只抓外显的性格、兴趣"
    "与事实；\n"
    "  - 口语词校准：以上口语线索（社恐、嘴硬心软、粘人、报备、冷处理、奔着结婚等）"
    "都是正经证据，听到就按语义归入对应维度，不要因为词太口语、太短就漏抽或只当"
    "闲聊；\n"
    "  - 置信度 confidence 按陈述清晰度给值：用户直接明确陈述给 0.9 以上；清晰但"
    "需轻度归纳给 0.75-0.9；有直接原文支撑但需要轻度归纳时给 0.5-0.75，不要仅因"
    "不是正式结论就丢弃；没有原文支撑或把握不足 0.5 就不要输出。\n"
    "  - 去重：同一事实只沉淀一次，但同一主题不等于重复。若下方给出「本会话已沉淀"
    "的候选」，只跳过语义相同的事实；新的时间跨度、行为方式、限制条件、纠正信息或"
    "新增维度都属于新证据，应单独输出。\n"
)

_MASTER_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，根对象形如：\n"
    "{\n"
    "  \"clarifying_question\": null,\n"
    "  \"fields\": [],\n"
    "  \"patches\": []\n"
    "}\n"
    "或\n"
    "{\n"
    "  \"clarifying_question\": null,\n"
    "  \"fields\": [\n"
    "    {\n"
    "      \"field_key\": \"city_code\",\n"
    "      \"value\": \"330100\",\n"
    "      \"source_quote\": \"我住在杭州\",\n"
    "      \"confidence\": 0.95\n"
    "    }\n"
    "  ],\n"
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
    existing_digest: str | None = None,
) -> str:
    """构造 master 会话对话抽取 prompt（设计 Task 6）。

    ``turn_texts`` 是本会话按时间顺序的用户陈述；``entry_digest`` 是该维度
    已发布条目摘要，供 modify patch 定位被改写条目，可为 None。
    ``existing_digest`` 是本会话已沉淀的活跃候选摘要（防跨轮重复抽取），
    可为 None。契约：允许返回 0 条 fields / patches，禁止返回澄清问题——
    澄清由墨相师对话承担。
    """
    is_personal = subject == ProfileSubject.PERSONAL.value
    subject_label = "个人画像" if is_personal else "理想型画像"
    field_guide = _PERSONAL_FIELD_GUIDE if is_personal else _IDEAL_PARTNER_FIELD_GUIDE
    field_lines = "\n".join(
        f"  - {key}：{description}" for key, description in field_guide.items()
    )
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
    existing_block = ""
    if existing_digest:
        existing_block = (
            "\n本会话已沉淀的候选（不要重复输出语义相同的内容，除非用户带来新细节）：\n"
            f"{existing_digest}\n"
        )
    return (
        f"{_MASTER_SYSTEM_HEADER}\n\n"
        f"建构目标：{subject_label}。\n"
        f"可固化的白名单字段及其值格式：\n{field_lines}\n\n"
        f"{digest_block}{existing_block}\n"
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
