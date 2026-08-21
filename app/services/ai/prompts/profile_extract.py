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
    "income_band": "整数，收入档位 0 及以上。中文映射：0=无收入，1=一档/第一档，2=二档/第二档，3=三档，以此类推。区间表述如“一档到二档”取较低值 1。金额换算参考：月收入 0 元=0档，3000 元以下=1档，3000-8000=2档，8000-15000=3档，15000-25000=4档，25000-50000=5档，50000 以上=6档。用户说“月收入一万五”时取 3 档；说“月薪两万”时取 4 档",
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
    "income_band": "区间，{\"min\": 最低档位, \"max\": 最高档位}，0 及以上；max 可为 null。中文映射：1=一档/第一档，2=二档/第二档，以此类推。金额换算参考：月收入 3000 元以下=1档，3000-8000=2档，8000-15000=3档，15000-25000=4档，25000-50000=5档，50000 以上=6档",
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


def build_profile_extract_prompt(
    subject: str,
    turn_texts: tuple[str, ...],
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

    return (
        f"{_SYSTEM_HEADER}\n\n"
        f"当前抽取目标：{subject_label}。\n"
        f"可抽取的字段及其值格式：\n{field_lines}\n\n"
        f"{_JSON_FORMAT_INSTRUCTION}\n\n"
        f"以下是用户的会话回答：\n{turn_block}"
    )
