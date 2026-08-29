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

from typing import Any

from app.schemas.ai_profile import PROFILE_ENTRY_CATEGORY_LABELS, ProfileSubject

# 维度定义（personal 和 ideal_partner 共用同一套维度卡片）
_DIMENSIONS = {
    "relationship": ("relationship", "感情观"),
    "personality": ("personality", "性格"),
    "lifestyle": ("lifestyle", "生活方式"),
    "future": ("future", "人生规划"),
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
    "你是「宣誓爱」的画像解读师，读者是刚认真填写完资料的用户本人。"
    "你的任务是基于用户已确认的结构化字段，写一份用户愿意读完、并且觉得"
    "「被理解了」的画像解读。\n"
    "写作铁律：\n"
    "1. 全程使用第二人称「你」，像懂TA的知心朋友在轻声总结，不是系统在输出报告。\n"
    "2. 不复述条件清单：严禁出现「年龄在26-33岁之间」「本科及以上」「身高158cm以上」"
    "这类数字与条件的罗列，字段必须被翻译成生活语言。\n"
    "3. 禁用报告腔词汇：该用户、这位、个体、画像展示、设定、偏好、指标、评分、群体特征。\n"
    "4. 要具体：把字段落到场景里，不要写「热爱户外与艺术」，而是写「愿意陪你走完一条"
    "山线、也会停下来认真拍照的人」。\n"
    "5. 温暖克制：不评判、不制造焦虑、不承诺关系结果；不确定的推断轻描淡写，"
    "不要连用「可能/或许」。\n"
    "6. 不得编造用户未提供的信息。输出必须是 JSON 格式，不要输出任何 JSON 之外的内容。\n"
    "7. 成稿必须能被读者指认到本次已确认字段。标题、洞察、标签、结论都要点出"
    "至少两个具体字段内容，禁止写成放之四海而皆准的套话。\n"
    "8. 某个维度如果本次字段覆盖不到，summary 必须明确写「这一稿还没写到……」，"
    "不要用性格/生活态度去填空。"
)

_JSON_FORMAT_INSTRUCTION = (
    "请以 JSON 格式输出，结构如下：\n"
    "{\n"
    '  "persona_title": "一句话人格概括，不超过20字",\n'
    '  "persona_tags": ["标签1", "标签2", "标签3"],\n'
    '  "insight": "一段50-150字的AI洞察，综合描述这个人",\n'
    '  "dimensions": [\n'
    '    {"key": "relationship", "icon": "relationship", "title": "感情观", '
    '"summary": "20-60字的维度解读"},\n'
    '    {"key": "personality", "icon": "personality", "title": "性格", '
    '"summary": "..."},\n'
    '    {"key": "lifestyle", "icon": "lifestyle", "title": "生活方式", '
    '"summary": "..."},\n'
    '    {"key": "future", "icon": "future", "title": "人生规划", '
    '"summary": "..."}\n'
    "  ],\n"
    '  "ideal_weights": [],\n'
    '  "recent_change": null,\n'
    '  "history_observations": [],\n'
    '  "conclusion": "一段写在最后的概括性总结，50-120字"\n'
    "}\n\n"
    "字段说明：\n"
    "- persona_title：一句有画面感的概括。个人画像如\"慢热但真诚的长期主义者\"；"
    "理想型画像直接描述那个人的气质，不要加「理想伴侣：」前缀。\n"
    "- persona_tags：3-5个标签词，像朋友聊天时会用的说法，避免书面腔。\n"
    "- insight：50-150字，写「你」在意的本质，而不是条件本身。"
    "禁止机械罗列条件（如「该用户年龄26-33岁、本科以上学历」），"
    "要写成人性化表达（如「比起一张条件清单，你更在意那个人有没有把生活过明白」）。\n"
    "- dimensions：固定4个维度（感情观/性格/生活方式/人生规划），每个维度"
    "20-60字。结合具体字段写成生活化的句子，不要泛泛而谈。"
    "icon 必须原样使用示例中的四个 token（relationship、personality、lifestyle、future），"
    "禁止改写成 emoji、HTML/XML 标签或其它英文单词。\n"
    "- ideal_weights：仅当抽取目标为「理想型画像」时填写，"
    "按 5 个维度给出 0-100 的整数，表示你更看重什么。"
    "权重要反映真实侧重：最看重的维度应明显领先，不要输出等差梯度；"
    "各项独立取值，总和不必凑成100。"
    "key 从 values/communication/emotion/lifestyle/appearance 中选取，"
    "label 为对应中文维度名。示例："
    '  [{"key": "values", "label": "价值观", "percent": 45}, '
    '{"key": "communication", "label": "沟通方式", "percent": 20}, '
    '{"key": "emotion", "label": "情绪稳定", "percent": 18}, '
    '{"key": "lifestyle", "label": "生活节奏", "percent": 12}, '
    '{"key": "appearance", "label": "外在条件", "percent": 5}]。'
    "个人画像时返回空数组 []。\n"
    "- recent_change：像朋友注意到你的变化那样写，direction 只能是 up 或 down，"
    "summary 与 observation 都用第二人称。无变化或首次发布时返回 null。\n"
    "- history_observations：历史观察记录，每条包含 revision_id（整数）、"
    "keywords（2-4个关键词）和 observation（30-100字），"
    "写成「这段时间你……」的回顾口吻。没有历史数据返回空数组 []。\n"
    "- conclusion：写在最后的概括性总结，50-120字，第二人称。"
    "把标题、洞察、维度收拢成一个温柔的整体印象，不新增前面没提过的信息，"
    "不承诺关系结果，不以建议、口号或行动号召结尾。"
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
    subject_label = "你自己" if is_personal else "你期望的另一半"

    current_block = _fields_to_block(current_fields)
    previous_block = _fields_to_block(previous_fields)
    history_block = _history_to_block(history_summaries)

    has_previous = "有上一版本可对比" if previous_fields else "这是首次发布，没有上一版本"

    return (
        f"{_SYSTEM_HEADER}\n\n"
        f"这次要写的是：{subject_label}。\n"
        f"历史状态：{has_previous}。\n\n"
        f"当前版本已确认字段：\n{current_block}\n\n"
        f"上一版本字段（用于变化趋势分析）：\n{previous_block}\n\n"
        f"历史版本摘要：\n{history_block}\n\n"
        f"{_JSON_FORMAT_INSTRUCTION}\n\n"
        f"注意：首次发布时 recent_change 返回 null。"
        f"写「你期望的另一半」时填 ideal_weights 且所有文案从「你」的视角出发；"
        f"写你自己时 ideal_weights 返回空数组。"
    )


def serialize_fields_for_prompt(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """把数据库行转成 prompt 安全的 field dict 元组。

    只保留 field_key 和 display_value，不含原始回答文本或用户 ID。
    WP-P1：条目（field_kind='entry'）以「条目·分类」伪字段行进入 prompt，
    让叙事自然融合条目；正文来自用户已确认内容，faithfulness 约束不变。
    """
    result: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("field_kind") or "structured") == "entry":
            category = str(row.get("category") or "")
            label = PROFILE_ENTRY_CATEGORY_LABELS.get(category, category)
            content = str(row.get("content") or "").strip()
            if content:
                result.append(
                    {
                        "field_key": f"entry_{category}",
                        "display_value": f"条目·{label}：{content}",
                    }
                )
            continue
        result.append(
            {
                "field_key": str(row.get("field_key", "")),
                "display_value": str(row.get("display_value") or row.get("value_json") or ""),
            }
        )
    return tuple(result)
