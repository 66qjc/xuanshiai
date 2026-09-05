"""墨相师人设提示词：白名单段必须存在；构建上下文注入 system 消息。"""
from datetime import datetime

from app.services.ai.prompts.moxiang_master import (
    AI_ROLE_NAME,
    _SYSTEM_HEADER,
    _greeting_for_hour,
    build_build_context,
    build_master_prompt,
    opening_message_for_subject,
    OPENING_MESSAGE,
    IDEAL_PARTNER_OPENING_MESSAGE,
)
from app.services.ai.prompts.profile_extract import (
    _MASTER_SYSTEM_HEADER,
    build_profile_master_extract_prompt,
)


def test_system_header_contains_topic_whitelist() -> None:
    for topic in ("自我认知", "三观", "生活方式", "物理位置", "基本情况"):
        assert topic in _SYSTEM_HEADER
    assert "拉回" in _SYSTEM_HEADER


def test_build_context_appended_as_system_message() -> None:
    ctx = build_build_context(["city_code", "age"], "用户已确认：从事设计工作", 40.0)
    messages = build_master_prompt("我在杭州做设计", [], narrative_context="", build_context=ctx)
    assert messages[0]["role"] == "system"
    assert "白名单" not in messages[1]["content"]  # build_context 是独立 system 段
    assert "城市" in messages[1]["content"]
    assert "40" in messages[1]["content"]


def test_build_context_identifies_the_current_portrait_subject() -> None:
    ctx = build_build_context(
        ["age"], "", 0.0, subject="ideal_partner"
    )

    assert "愿遇之相" in ctx
    assert "当前建构主体" in ctx


def test_master_extract_prompt_protects_specific_people_and_requires_preference() -> None:
    for phrase in ("具体对象", "明确确认", "愿遇之相", "偏好"):
        assert phrase in _MASTER_SYSTEM_HEADER


def test_master_extract_prompt_requests_allowlisted_structured_fields_and_patches() -> None:
    """墨相师抽取同时沉淀可确认字段与六维自由条目，不能只产 patch。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal", turn_texts=("我住杭州，周末喜欢看展。",)
    )

    assert '"fields"' in prompt
    assert "city_code" in prompt
    assert "interest_tags" in prompt
    assert '"patches"' in prompt


def test_master_extract_prompt_requires_exhaustive_semantic_atomization() -> None:
    """30 轮基线暴露出一条回答中的第二个维度容易被合并或漏掉。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal",
        turn_texts=(
            "我刚认识时话不多，熟了以后愿意倾听；未来也想和伴侣一起安定生活。",
        ),
        existing_digest="性格：慢热",
    )

    for rule in (
        "逐句扫描",
        "最小独立事实",
        "同一句可以输出多条 patch",
        "一条 patch 只表达一个事实",
        "同一主题不等于重复",
        "新增维度",
    ):
        assert rule in prompt


def test_master_extract_prompt_has_dimension_precedence_for_baseline_misses() -> None:
    """情绪表达、长期愿景与个人空间必须先按语义分桶，再考虑通用词。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal",
        turn_texts=("我不太会说甜言蜜语，但会用行动表达。",),
    )

    for example in (
        "不擅长甜言蜜语但会用行动表达",
        "先倾听、后给建议",
        "三五年",
        "十年后",
        "婚姻里成为队友",
        "个人空间",
        "从具体关系经历中总结出的自我模式",
    ):
        assert example in prompt
    assert "长期关系愿景优先归入 future_expectations" in prompt
    assert "情绪表达优先归入 emotional_expression" in prompt


def test_master_extract_prompt_retains_supported_soft_evidence_without_guessing() -> None:
    """软证据可以低置信保留，但无原文支撑的内容仍必须丢弃。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal",
        turn_texts=("现在想想，我更在意相处时的性格互动。",),
    )

    assert "有直接原文支撑但需要轻度归纳" in prompt
    assert "不要仅因不是正式结论就丢弃" in prompt
    assert "把握不足 0.5 就不要输出" in prompt


# ===== Phase 1 Contract v1.1 P1-B：subject boundary + opening message guards =====


def test_system_header_declares_two_subjects_no_cross_write() -> None:
    """Master system header must declare the personal/ideal_partner split."""
    for cue in ("我的墨相", "愿遇之相", "不"):
        assert cue in _SYSTEM_HEADER, (
            f"master system header missing subject-boundary cue: {cue!r}"
        )


def test_opening_messages_are_subject_specific() -> None:
    """``OPENING_MESSAGE`` and ``IDEAL_PARTNER_OPENING_MESSAGE`` must be distinct."""
    assert OPENING_MESSAGE != IDEAL_PARTNER_OPENING_MESSAGE
    # 批次3 #1：variant=0 是稳定基准开场，正文与兼容常量一致（问候语时段化）。
    noon = datetime(2026, 9, 3, 12, 0)
    personal = opening_message_for_subject("personal", now=noon, variant=0)
    partner = opening_message_for_subject("ideal_partner", now=noon, variant=0)
    assert personal != partner
    assert "中午好，我是知遇。" in personal
    assert "中午好，我是知遇。" in partner


def test_opening_personalizes_by_time_and_rotates_variants() -> None:
    """#1：问候语按时段变化；不同 variant 给出不同首个话题。"""
    from app.services.ai.prompts import moxiang_master

    assert _greeting_for_hour(7) == "早上好"
    assert _greeting_for_hour(15) == "下午好"
    assert _greeting_for_hour(20) == "晚上好"
    assert _greeting_for_hour(1) == "夜深了"
    morning = datetime(2026, 9, 3, 7, 30)
    pool_size = len(moxiang_master._PERSONAL_OPENING_QUESTIONS)
    variants = [
        opening_message_for_subject("personal", now=morning, variant=i)
        for i in range(pool_size)
    ]
    assert len(set(variants)) == pool_size
    assert all(v.startswith("早上好，我是知遇。") for v in variants)
    # variant 越界按套数取模，不抛错。
    assert (
        opening_message_for_subject("personal", now=morning, variant=pool_size)
        == variants[0]
    )
    # 默认（variant=None）随机轮换，但结构不变。
    default = opening_message_for_subject("personal")
    assert "我是知遇。" in default


def test_master_prompt_forbids_repetitive_follow_up_phrases() -> None:
    """#2：人设 prompt 必须带追问句式库与反重复约束。"""
    assert "不得连续两轮使用相同的引导句式" in _SYSTEM_HEADER
    assert "那我好奇的是" in _SYSTEM_HEADER  # 反例点名，限制口头禅
    for cue in ("最近的例子", "什么样的时刻", "画面"):
        assert cue in _SYSTEM_HEADER


def test_opening_messages_do_not_leak_into_the_other_subject() -> None:
    """The two opening strings must not reference the other subject's vocabulary."""
    assert "理想" not in OPENING_MESSAGE
    assert "我自己" not in IDEAL_PARTNER_OPENING_MESSAGE


def test_opening_uses_dedicated_ai_identity_not_product_or_model_name() -> None:
    """角色名属于产品 persona，不应退化成产品名、旧昵称或 LLM 名称。"""
    assert AI_ROLE_NAME == "知遇"
    assert f"我是{AI_ROLE_NAME}" in OPENING_MESSAGE
    assert f"我是{AI_ROLE_NAME}" in IDEAL_PARTNER_OPENING_MESSAGE
    assert AI_ROLE_NAME in _SYSTEM_HEADER
    for internal_term in ("供应商", "模型", "LLM"):
        assert internal_term in _SYSTEM_HEADER
    assert "我是点点" not in OPENING_MESSAGE
    assert "墨相师" not in OPENING_MESSAGE
    assert "deepseek" not in OPENING_MESSAGE.lower()
    assert "dots" not in OPENING_MESSAGE.lower()


def test_build_context_personal_and_ideal_partner_are_distinct() -> None:
    """The two subject build contexts must be distinct and labelled."""
    personal = build_build_context(
        missing_hard=["age"], confirmed_summary="", percent=25.0, subject="personal"
    )
    partner = build_build_context(
        missing_hard=["age"], confirmed_summary="", percent=25.0, subject="ideal_partner"
    )
    assert "我的墨相" in personal
    assert "愿遇之相" in partner
    assert personal != partner


def test_build_context_injects_dimension_progress() -> None:
    """六维进度行注入后，知遇必须能看到每维状态并被引导优先问空白维度。"""
    ctx = build_build_context(
        [], "", 25.0,
        dimension_lines=[
            "- 性格与社交：已理解",
            "- 亲密模式：空白",
            "- 生活方式：部分理解（1条）",
        ],
    )
    assert "六维建构进度" in ctx
    assert "- 亲密模式：空白" in ctx
    assert "优先围绕「空白」与「部分理解」的维度" in ctx
    assert "不要连续多轮停留在同一维度" in ctx


def test_build_context_without_dimension_lines_has_no_progress_section() -> None:
    """不传 dimension_lines 时保持旧行为，不出现六维段。"""
    ctx = build_build_context(["age"], "", 0.0)
    assert "六维建构进度" not in ctx


def test_build_context_injects_steering_hooks_for_blank_dimensions() -> None:
    """层1隐含式引导：空白维度钩子注入后，知遇知道从哪个场景切入。"""
    from app.services.ai.prompts.moxiang_master import steering_hook_lines

    hooks = steering_hook_lines(["intimacy_pattern", "future_expectations"])
    assert len(hooks) == 2
    assert all(hook.startswith("- ") for hook in hooks)
    ctx = build_build_context(
        [], "", 25.0,
        dimension_lines=["- 亲密模式：空白"],
        steering_hooks=hooks,
    )
    assert "可以自然延伸的方向" in ctx
    assert hooks[0] in ctx
    assert "一次最多一个" in ctx
    assert "不要向用户提" in ctx


def test_steering_hook_lines_skip_unknown_and_blank() -> None:
    """未知维度 key 原样跳过；空列表不产生钩子段。"""
    from app.services.ai.prompts.moxiang_master import steering_hook_lines

    assert steering_hook_lines([]) == []
    assert steering_hook_lines(["not_a_dimension"]) == []


def test_steering_hooks_match_the_portrait_subject() -> None:
    """愿遇之相的钩子问 TA 的期待（有「希望/对方」），不漏自我话题进偏好阶段。"""
    from app.services.ai.prompts.moxiang_master import steering_hook_lines

    personal = steering_hook_lines(["intimacy_pattern"], subject="personal")
    ideal = steering_hook_lines(["intimacy_pattern"], subject="ideal_partner")
    assert personal and ideal
    assert personal != ideal
    for _ in range(6):
        assert any(cue in hook for hook in steering_hook_lines(["intimacy_pattern"], subject="ideal_partner") for cue in ("希望", "对方", "期待"))


def test_system_header_has_topic_rotation_and_sensitive_entry_rules() -> None:
    """规则13：话题轮换 + 敏感面向用行为场景切入，不暴露维度词。"""
    for cue in ("话题要在", "二选一的小情境", "向用户提及维度名称"):
        assert cue in _SYSTEM_HEADER


def test_system_header_has_self_disclosure_pacing_rule() -> None:
    """规则14：开场轻起手，敏感面向后置。"""
    for cue in ("自我披露节奏", "轻松、日常、正向", "不要第一句就往深处问"):
        assert cue in _SYSTEM_HEADER


def test_personal_opening_pool_covers_light_dimension_entries() -> None:
    """开场白池 4 套轮换，全部是低压力场景化入口（调研：场景题优于抽象题）。"""
    from app.services.ai.prompts import moxiang_master

    pool = moxiang_master._PERSONAL_OPENING_QUESTIONS
    assert len(pool) >= 4
    assert all("？" in q for q in pool)
    # 不出现术语化提问——开场不暴露维度/画像/进度。
    for banned in ("维度", "画像", "进度", "亲密模式", "关系边界"):
        assert banned not in "".join(pool)


def test_ideal_partner_opening_avoids_condition_checklist_framing() -> None:
    """愿遇之相开场不问条件清单，问相处的瞬间（行为化措辞）。"""
    from app.services.ai.prompts import moxiang_master

    for opening in moxiang_master._IDEAL_PARTNER_OPENINGS:
        assert "相处" in opening
        assert "不用列条件清单" in opening


def test_extract_prompt_requires_implicit_clue_collection() -> None:
    """抽取 prompt 必须提醒内隐线索（情绪/边界/期待藏在生活叙述里）。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal",
        turn_texts=("我周末喜欢自己待着。",),
    )
    assert "内隐线索必收" in prompt
    assert "情绪表达、亲密模式、关系边界、未来期待" in prompt


def test_extract_prompt_cue_words_cover_colloquial_speech() -> None:
    """六维归属线索词必须覆盖口语表达（调研：用户用社恐/嘴硬心软/报备等词）。"""
    prompt = build_profile_master_extract_prompt(
        subject="personal",
        turn_texts=("我是那种嘴硬心软的人。",),
    )
    # 每个维度的口语线索词
    for cue in (
        "嘴硬心软",  # 情绪表达
        "奔着结婚",  # 未来期待
        "报备",      # 关系边界
        "冷处理",    # 亲密模式
        "宅家",      # 生活方式
        "社恐",      # 性格与社交
        "口语词校准",
    ):
        assert cue in prompt


def test_category_whitelist_shared_and_colloquial() -> None:
    """category 白名单三处共用同一常量，且带口语线索词。"""
    from app.services.ai.prompts import profile_extract

    shared = profile_extract._CATEGORY_WHITELIST
    assert shared in profile_extract._ENTRY_GUIDE
    assert shared in profile_extract._UPDATE_SYSTEM_HEADER
    assert shared in profile_extract._MASTER_SYSTEM_HEADER
    for cue in ("社恐", "慢热", "夜猫子", "点外卖", "考公考编"):
        assert cue in shared


def test_update_prompt_accepts_colloquial_preference_markers() -> None:
    """愿遇之相偏好确认词组要收口语变体（我喜欢/吃这一套/找对象就得找）。"""
    from app.services.ai.prompts.profile_extract import _UPDATE_SYSTEM_HEADER

    for cue in ("我喜欢", "我吃这一套", "找对象就得找"):
        assert cue in _UPDATE_SYSTEM_HEADER


def test_journey_dimension_status_rendering() -> None:
    """journey._dimension_status 与进度口径（0/50/100）一致。"""
    from app.services.ai.journey import _dimension_status
    from app.services.ai.journey_progress import JourneyDimensionProgress

    assert _dimension_status(JourneyDimensionProgress(percent=0.0, evidence_count=0)) == "空白"
    assert (
        _dimension_status(JourneyDimensionProgress(percent=50.0, evidence_count=1))
        == "部分理解（1条）"
    )
    assert (
        _dimension_status(JourneyDimensionProgress(percent=100.0, evidence_count=2))
        == "已理解"
    )


def test_master_extract_prompt_injects_per_subject_label() -> None:
    """Master extract prompt must label the per-subject so the model buckets correctly."""
    personal = build_profile_master_extract_prompt(
        subject="personal", turn_texts=("我性格偏内敛。",)
    )
    partner = build_profile_master_extract_prompt(
        subject="ideal_partner", turn_texts=("我希望对方开朗一些。",)
    )
    assert "个人画像" in personal
    assert "理想型画像" in partner
