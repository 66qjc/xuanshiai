"""墨相师建构进度折算：硬字段必达 + entry 0.5 分/条上限 2 分。"""
import pytest

from app.services.ai.profile import MASTER_HARD_FIELD_KEYS, master_progress


def test_gate_requires_all_hard_fields() -> None:
    nine = frozenset(MASTER_HARD_FIELD_KEYS - {"age"}) | {"height_cm", "income_band",
        "education_level", "occupation_group", "city_code", "marriage_status",
        "lifestyle_tags", "relationship_goal"}
    # 9 个 structured 全确认但缺 age：percent 再高也 gate_met=False
    result = master_progress(nine, confirmed_entries=4)
    assert result.hard_done == 2 and result.hard_total == 3
    assert result.percent >= 60.0
    assert result.gate_met is False


def test_entry_score_capped_at_two() -> None:
    result = master_progress(frozenset(MASTER_HARD_FIELD_KEYS), confirmed_entries=99)
    assert result.entry_score == 2.0


def test_gate_met_with_hard_and_entries() -> None:
    # 3 硬字段(3) + 4 条目(折算 2) = 5 分 → 50% < 60%，不达标
    below = master_progress(frozenset(MASTER_HARD_FIELD_KEYS), confirmed_entries=4)
    assert below.gate_met is False and below.percent == 50.0
    # 3 硬字段 + 5 structured + 4 条目(2) = 10 分 → 100%
    ok_keys = frozenset(MASTER_HARD_FIELD_KEYS | {"height_cm", "income_band",
        "education_level", "occupation_group", "lifestyle_tags"})
    above = master_progress(ok_keys, confirmed_entries=4)
    assert above.gate_met is True and above.percent == 100.0


def test_only_confirmed_count_and_formula() -> None:
    result = master_progress(frozenset({"city_code", "age"}), confirmed_entries=2)
    assert result.percent == 30.0  # (2 + 2*0.5)/10*100
    assert result.gate_met is False
