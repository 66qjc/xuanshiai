"""``scripts.moxiang_prompt_loop`` 评分器 + transcript + snapshot 的离线单测。

不依赖真实数据库 / WebSocket,只验证评分算法与 fixtures 解析。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.moxiang_prompt_loop import db_snapshot, transcript
from scripts.moxiang_prompt_loop.scorers import (
    dimension,
    evidence,
    run_all,
    state_dedup,
    style_safety,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "moxiang" / "transcripts"


def _fake_snapshot(tables: dict[str, list[dict]]) -> db_snapshot.Snapshot:
    snap = db_snapshot.Snapshot(user_id=1, session_id="s-1")
    snap.tables = tables
    snap.stats = {name: len(rows) for name, rows in tables.items()}
    return snap


def test_transcript_load_smoke() -> None:
    path = FIXTURE_DIR / "10turn-smoke.jsonl"
    t = transcript.load(path)
    assert t.name == "10turn-smoke"
    user_count = sum(1 for turn in t.turns if turn.get("role") == "user")
    assert user_count == 10
    assert any(turn.get("category") == "ambiguous" for turn in t.turns)
    assert any(turn.get("category") == "off_topic" for turn in t.turns)


def test_dimension_scorer_recall_precision() -> None:
    snapshot = _fake_snapshot(
        {
            "ai_profile_turn": [
                {"turn_id": "1", "role": "user", "turn_no": 1},
                {"turn_id": "2", "role": "user", "turn_no": 3},
                {"turn_id": "3", "role": "user", "turn_no": 5},
            ],
            "ai_profile_candidate": [
                {
                    "candidate_id": "c1",
                    "session_id": "s-1",
                    "profile_dimension": "lifestyle",
                    "source_turn_ids": ["1"],
                    "content_hash": "h1",
                },
                {
                    "candidate_id": "c2",
                    "session_id": "s-1",
                    "profile_dimension": "personality_social",
                    "source_turn_ids": ["2"],
                    "content_hash": "h2",
                },
            ]
        }
    )
    run_meta = {
        "transcript_name": "test",
        "expect_dimensions": [
            {"turn": 1, "dimensions": ["lifestyle"]},
            {"turn": 2, "dimensions": ["personality_social"]},
            {"turn": 3, "dimensions": ["future_expectations"]},
        ],
        "ai_replies": ["a", "b", "c"],
    }
    report = dimension.score(run_meta, snapshot)
    # 命中 2 / expect 3 = 0.6667, 2 / 2 = 1.0
    assert report.details["recall"] == pytest.approx(0.6667, abs=0.01)
    assert report.details["precision"] == pytest.approx(1.0)
    assert report.passed is True


def test_style_safety_banned_phrases_zero() -> None:
    snapshot = _fake_snapshot({})
    run_meta = {
        "ai_replies": [
            "我爱你,我们命中注定的,一定能找到幸福。",
            "你最近看起来有点累,方便说说吗?",
        ]
    }
    report = style_safety.score(run_meta, snapshot)
    assert report.details["per_turn"][0]["score"] == 0.0
    assert report.details["per_turn"][1]["score"] > 50


def test_style_safety_sensitive_leak_zero() -> None:
    snapshot = _fake_snapshot({})
    run_meta = {
        "ai_replies": [
            "你提到的 13800001111 我帮你记一下。",
        ]
    }
    report = style_safety.score(run_meta, snapshot)
    assert report.details["per_turn"][0]["deductions"][0]["rule"] == "sensitive_leak"


def test_evidence_scorer_missing_turn_id() -> None:
    snapshot = _fake_snapshot(
        {
            "ai_profile_turn": [
                {"turn_id": "t1", "answer_text": "我喜欢咖啡馆"},
            ],
            "ai_profile_candidate": [
                {
                    "candidate_id": "c1",
                    "source_turn_ids": ["t1", "t2"],
                    "confidence": 0.9,
                    "source_span": "咖啡馆",
                }
            ],
        }
    )
    report = evidence.score({}, snapshot)
    assert report.details["failures"] == 1
    assert report.passed is False


def test_state_dedup_duplicate_task_and_hash() -> None:
    snapshot = _fake_snapshot(
        {
            "ai_task": [
                {"task_type": "profile_extract", "idempotency_key": "k1"},
                {"task_type": "profile_extract", "idempotency_key": "k1"},
            ],
            "ai_profile_candidate": [
                {"session_id": "s1", "content_hash": "h1"},
                {"session_id": "s1", "content_hash": "h1"},
            ],
            "ai_profile_draft": [
                {"user_id": 1, "subject": "personal", "expected_revision": 1},
                {"user_id": 1, "subject": "personal", "expected_revision": 2},
            ],
            "ai_profile_draft_field": [],
        }
    )
    report = state_dedup.score({}, snapshot)
    assert report.details["failure_count"] == 2
    assert report.score == 50.0


def test_run_all_score_bundle_shape() -> None:
    snapshot = _fake_snapshot(
        {
            "ai_profile_turn": [{"turn_id": "t1", "answer_text": "你好"}],
            "ai_profile_candidate": [
                {
                    "candidate_id": "c1",
                    "session_id": "s1",
                    "profile_dimension": "lifestyle",
                    "source_turn_ids": ["t1"],
                    "content_hash": "h1",
                    "confidence": 0.8,
                    "source_span": "你好",
                }
            ],
            "ai_task": [],
            "ai_profile_draft": [],
            "ai_profile_draft_field": [],
        }
    )
    run_meta = {
        "transcript_name": "t",
        "expect_dimensions": [{"turn": 1, "dimensions": ["lifestyle"]}],
        "ai_replies": ["你说,你慢慢讲,我听着。"],
    }
    bundle = run_all(run_meta, snapshot)
    assert bundle.total > 0
    assert len(bundle.reports) == 4


def test_transcript_load_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        transcript.load(tmp_path / "missing.jsonl")


def test_transcript_load_malformed(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("not a json", encoding="utf-8")
    with pytest.raises(ValueError):
        transcript.load(path)


def test_scorer_run_all_outputs_jsonable() -> None:
    snapshot = _fake_snapshot(
        {
            "ai_profile_turn": [{"turn_id": "t1", "answer_text": "好"}],
            "ai_profile_candidate": [],
            "ai_task": [],
            "ai_profile_draft": [],
            "ai_profile_draft_field": [],
        }
    )
    bundle = run_all(
        {"ai_replies": ["你最近在想什么?"]},
        snapshot,
    )
    payload = bundle.to_dict()
    json.dumps(payload, ensure_ascii=False)  # 不抛即通过
