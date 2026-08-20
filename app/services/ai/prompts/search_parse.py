"""搜索意图解析的中文 prompt 构建器。

字段与 operator 受控集合严格对齐 ``app/schemas/ai_search.py`` 的
``FIELD_OPERATOR_ALLOWLIST`` 和 ``SearchConditionOperator`` 枚举，
确保 DeepSeek 输出的条件能通过后端校验并编译成参数化筛选。
"""

from __future__ import annotations

# 每个字段允许的 operator，与 FIELD_OPERATOR_ALLOWLIST 一致。
_FIELD_OPERATOR_GUIDE = {
    "age": "between（{\"min\": 最小, \"max\": 最大}）/ gte（最小）/ lte（最大）",
    "city_code": "eq（单个城市代码）/ in（城市代码数组）",
    "marriage_status": "eq（单个状态）/ in（状态数组）",
    "education_level": "gte（最低学历等级，整数 1-8。中文映射：1=初中及以下，2=高中/中专，3=大专，4=本科，5=硕士，6=博士。用户说“本科以上/本科及以上”时 value=4）",
    "height_cm": "between / gte / lte，数值为厘米数",
    "income_band": "between / gte / lte，数值为收入档位。中文映射：1=一档/第一档，2=二档/第二档，以此类推。用户说“收入至少第二档”时 value=2",
    "occupation_group": "eq（单个行业值）",
    "interest_tags": "contains（单个标签字符串）",
    "lifestyle_tags": "contains（单个标签字符串）",
    "relationship_goal": "eq（单个目标值）",
}

# 枚举字段的合法取值，与 PROFILE_ENUM_DICTIONARY 一致。
_ENUM_VALUES = {
    "marriage_status": "single / divorced / widowed",
    "occupation_group": "technology / education / healthcare / finance / public_service / other",
    "relationship_goal": "marriage / dating / friendship",
}

_SYSTEM_HEADER = (
    "你是一个严格的搜索意图解析器。你的任务是把用户的中文自然语言搜索请求，"
    "转换成结构化的筛选条件。你只能使用下面列出的字段和对应的操作符，"
    "不得使用任何未列出的字段或操作符，不得生成 SQL 或数据库查询语句。"
    "每条条件需要标注 kind：hard 表示硬性条件（必须满足），"
    "soft 表示软性偏好（加分项）。"
    "每条条件附带 confidence（0 到 1 之间的浮点数）"
    "和 source_span（用户原话中对应该条件的片段）。"
)

_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，根对象包含一个 \"conditions\" 数组。"
    "示例：\n"
    "{\n"
    "  \"conditions\": [\n"
    "    {\n"
    "      \"field_key\": \"age\",\n"
    "      \"operator\": \"between\",\n"
    "      \"value\": {\"min\": 26, \"max\": 32},\n"
    "      \"kind\": \"hard\",\n"
    "      \"confidence\": 0.95,\n"
    "      \"source_span\": \"26到32岁\"\n"
    "    }\n"
    "  ]\n"
    "}")


def build_search_parse_prompt(query_text: str) -> str:
    """构造搜索意图解析的完整 prompt。"""
    field_lines = "\n".join(
        f"  - {key}：{desc}" for key, desc in _FIELD_OPERATOR_GUIDE.items()
    )
    enum_lines = "\n".join(
        f"  - {key} 的合法取值：{values}"
        for key, values in _ENUM_VALUES.items()
    )

    return (
        f"{_SYSTEM_HEADER}\n\n"
        f"可用字段及其操作符：\n{field_lines}\n\n"
        f"枚举字段的合法取值：\n{enum_lines}\n\n"
        f"{_JSON_FORMAT_INSTRUCTION}\n\n"
        f"用户的搜索请求：\n{query_text}"
    )
