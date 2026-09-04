"""B4：WS 重连/切换主体时重放未确认完的确认卡（复用 _push_confirm_card_for_draft）。

覆盖三条路径：有 suggested 字段 → 推卡；无未完成草稿 → 静默；
草稿已全部确认 → 静默。不依赖真实 MySQL，用 fake db session factory。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.routes import voice_moxiang as vm


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeDB:
    """按 SQL 片段路由的 fake async session（async context manager）。"""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses

    async def __aenter__(self) -> "_FakeDB":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: object, params: dict | None = None) -> _FakeResult:
        sql = str(statement)
        for key, rows in self.responses.items():
            if key in sql:
                return _FakeResult(rows)
        return _FakeResult([])


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


@pytest.fixture
def journey_db(monkeypatch: pytest.MonkeyPatch):
    def _install(responses: dict[str, list[dict[str, Any]]]) -> None:
        monkeypatch.setattr(vm, "_db_session_factory", lambda: _FakeDB(responses))

    return _install


@pytest.mark.asyncio
async def test_replay_pushes_card_when_suggested_fields_exist(journey_db) -> None:
    journey_db({
        "SELECT draft_id FROM ai_profile_draft": [{"draft_id": "d-1"}],
        "SELECT expected_revision FROM ai_profile_draft": [{"expected_revision": 4}],
        "FROM ai_profile_draft_field": [
            {
                "field_key": "entry_lifestyle_a1",
                "field_kind": "entry",
                "category": "作息",
                "content": "周末习惯早起跑步",
                "display_value": "周末习惯早起跑步",
            }
        ],
    })
    ws = _FakeWS()
    await vm._replay_pending_confirm_card(ws, session_id="s-1", subject="personal")
    cards = [m for m in ws.sent if m.get("type") == "confirm_card"]
    assert len(cards) == 1
    assert cards[0]["draft_id"] == "d-1"
    assert cards[0]["expected_revision"] == 4
    assert cards[0]["items"][0]["field_key"] == "entry_lifestyle_a1"


@pytest.mark.asyncio
async def test_replay_silent_without_active_draft(journey_db) -> None:
    journey_db({"SELECT draft_id FROM ai_profile_draft": []})
    ws = _FakeWS()
    await vm._replay_pending_confirm_card(ws, session_id="s-1", subject="personal")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_replay_silent_when_all_fields_confirmed(journey_db) -> None:
    journey_db({
        "SELECT draft_id FROM ai_profile_draft": [{"draft_id": "d-1"}],
        "SELECT expected_revision FROM ai_profile_draft": [{"expected_revision": 6}],
        "FROM ai_profile_draft_field": [],
    })
    ws = _FakeWS()
    await vm._replay_pending_confirm_card(ws, session_id="s-1", subject="personal")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_replay_requires_session_id(journey_db) -> None:
    journey_db({"SELECT draft_id FROM ai_profile_draft": [{"draft_id": "d-1"}]})
    ws = _FakeWS()
    await vm._replay_pending_confirm_card(ws, session_id="", subject="personal")
    assert ws.sent == []
