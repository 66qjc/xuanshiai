"""WS 建构模式（设计 Task 7）：会话创建/进度推送/卡片推送/纯聊兼容。

fake WS 形态仿 ``tests/test_voice_ws.py``（TestClient + 真 FastAPI 路由），
fake 库形态仿 ``tests/test_master_extract_handler.py``（内存假库 + worker
``_process`` 同步消费）。抽取任务的「同步完成」由假库接管路由侧的
``ai_task`` 轮询查询实现：轮询到 ``queued`` 行时当场跑一次
``worker_mod._process``（fake gateway 注入），模拟 worker 在轮询间隙完成。

覆盖：
- session_start(mode=profile_build) → session_ready + progress(percent 0)，
  DB 出现 kind='master' 会话
- text_message → ai_reply 正常；DB 出现 user+assistant turn；ai_task 出现
  profile_extract 行并由 fake gateway 同步完成后收到 confirm_card
- 不带 mode 的 session_start：无会话创建、无 progress 推送（回归兼容）
- subject_switch 的非法主体、双主体会话/进度/确认卡隔离
- 断线重连恢复当前主体的进度/确认卡，并按主体推送 publish_ready
- 软删除建议硬字段行（占 (draft_id, field_key) 唯一键）后下一轮抽取重定位
  成功：UPDATE 复活而非 INSERT（无 IntegrityError）
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.db.session as db_session_module
import app.services.ai.profile as profile_mod
from app.core.config import settings
from app.core.security import create_token
from app.main import app
from app.schemas.ai_profile import ProfileSubject
from app.services.ai.base import (
    ExtractedField,
    ExtractedPatch,
    StructuredExtractResult,
)
from app.services.ai.gateway import InvokeOutcome
from app.services.ai.profile import (
    MASTER_HARD_FIELD_KEYS,
    PROFILE_SCHEMA_VERSION,
)
from app.workers import ai_worker as worker_mod
from tests.test_ai_profile_sessions import (
    _MappingResult,
    _WriteResult,
    _now,
)
from tests.test_master_extract_handler import (
    MasterFakeSession,
    MasterProfileStore,
    _master_gateway,
    _seed_turn_task,
)

# JWT 与门禁形态抄 tests/test_voice_ws.py；user_id=10 对齐假库预置授权。


def _make_access_token(user_id: int = 10) -> str:
    return create_token(
        user_id=user_id,
        session_id=1,
        token_type="access",
        expires_delta=timedelta(hours=1),
    )


def _recv_json(ws: Any, *, timeout: float = 10.0) -> dict[str, Any]:
    """带超时收一条消息（TestClient 的 receive_text 无超时，阻塞会挂死测试）。

    超时用 daemon 线程兜底：测试立即以 AssertionError 失败，不无限等待
    （GREEN 路径消息总是按时到达，不会产生滞留线程）。
    """
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            result["msg"] = json.loads(ws.receive_text())
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    reader = threading.Thread(target=_run, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            f"receive_text timed out after {timeout}s (no message arrived)"
        )
    if "error" in result:
        raise AssertionError(f"receive_text failed: {result['error']!r}")
    return result["msg"]


def _drain_until(
    ws: Any, stop_type: str, *, limit: int = 12, timeout: float = 10.0
) -> list[dict[str, Any]]:
    """收取消息直到出现指定 type；收不到则断言失败，避免无限等待。"""
    msgs: list[dict[str, Any]] = []
    for _ in range(limit):
        msg = _recv_json(ws, timeout=timeout)
        msgs.append(msg)
        if msg.get("type") == stop_type:
            return msgs
    raise AssertionError(
        f"did not receive {stop_type} within {limit} messages: "
        f"{[m.get('type') for m in msgs]}"
    )


def _install_gateway(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[Any], InvokeOutcome],
) -> list[Any]:
    """把 profile 模块的 AIGateway 换成按请求分发的 fake（抽取任务用）。"""
    requests: list[Any] = []
    monkeypatch.setattr(
        profile_mod, "AIGateway", _master_gateway(requests, responder)
    )
    return requests


class _WSFakeSession(MasterFakeSession):
    """WS 建构模式假库会话：在 master 假库路由之上补三处。

    1. ``_load_narrative_context`` 的 ``ai_profile_summary`` 查询 → 空上下文。
    2. 路由侧 ``SELECT status FROM ai_task`` 轮询：任务 ``queued`` 且测试注入
       了 ``extract_runner`` 时当场同步跑完 worker（fake gateway），模拟
       「抽取任务在轮询间隙完成」。
    3. 草稿字段复活 UPDATE（confirmation_status='suggested'）按真实 DB 语义
       刷新 value_json/display_value/source_turn_ids/content_hash。
    """

    def __init__(self, store: "_WSProfileStore") -> None:
        super().__init__(store)
        self.extract_runner: Callable[[Any], Any] | None = None

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult | Any:
        sql = str(statement)
        values = dict(params or {})
        store = self._store
        if "FROM ai_profile_turn" in sql and "ORDER BY turn_no DESC" in sql:
            rows = sorted(
                (
                    r
                    for r in store.turns
                    if r["session_id"] == values["session_id"]
                ),
                key=lambda r: r["turn_no"],
                reverse=True,
            )
            return _MappingResult(
                [
                    {
                        "role": r["role"],
                        "answer_text": r["answer_text"],
                    }
                    for r in rows
                ]
            )
        if "FROM ai_profile_summary" in sql:
            return _MappingResult([])
        if (
            "SELECT status FROM ai_task" in sql
            and "WHERE task_id = :task_id" in sql
        ):
            row = store.task_store.tasks.get(str(values.get("task_id")))
            if (
                row is not None
                and row["status"] == "queued"
                and self.extract_runner is not None
            ):
                from app.services.ai.tasks import AiTaskRecord

                await self.extract_runner(AiTaskRecord.from_row(row))
                row = store.task_store.tasks.get(str(values.get("task_id")))
            return _MappingResult([row] if row else [])
        if sql.startswith("UPDATE ai_profile_draft_field") and (
            "SET confirmation_status = 'suggested'" in sql
        ):
            for field in store.draft_fields:
                if field["draft_id"] == str(values["draft_id"]) and field[
                    "field_key"
                ] == str(values["field_key"]):
                    field["confirmation_status"] = "suggested"
                    value_json = values.get("value_json")
                    field["value_json"] = value_json
                    field["value"] = (
                        json.loads(value_json) if value_json else None
                    )
                    field["display_value"] = values.get("display_value")
                    field["source_turn_ids"] = values.get("source_turn_ids")
                    field["content_hash"] = values.get("content_hash")
                    field["updated_at"] = _now()
                    return _WriteResult(rowcount=1)
            return _WriteResult(rowcount=0)
        return await super().execute(statement, values)


class _WSProfileStore(MasterProfileStore):
    """publish/master 假库 + WS 建构模式读取端需要的真实 DB 语义补齐。

    - ``insert_draft`` 同步注册 ``drafts_by_id``（真实 DB 任意草稿都可按 id
      读回，``_load_draft_row`` 依赖它取 expected_revision）。
    - ``insert_draft_field`` 强制 (draft_id, field_key) 唯一键（真实 DB 的
      uk 约束）——让「重复 INSERT」在假库上同样撞 IntegrityError。
    - ``fields_for_draft`` 补 value_json 列（真实 DB 有该列，publish 假库
      只存解析后的 value）。
    """

    def __init__(self) -> None:
        super().__init__()
        # publish 假库预置的 draft-1（其自身测试的 confirmed 种子行）对 WS
        # 用例是噪声：清掉，保持「从零建构」的断言面。
        self.drafts = [d for d in self.drafts if d["draft_id"] != "draft-1"]
        self.draft_fields = [
            f for f in self.draft_fields if f["draft_id"] != "draft-1"
        ]
        self.drafts_by_id.pop("draft-1", None)
        self.session = _WSFakeSession(self)
        self.db = self.session

    def insert_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        row = super().insert_draft(params)
        self.drafts_by_id[row["draft_id"]] = row
        return row

    def insert_draft_field(self, params: dict[str, Any]) -> dict[str, Any]:
        key = (str(params.get("draft_id")), str(params.get("field_key")))
        if any(
            (f["draft_id"], f["field_key"]) == key for f in self.draft_fields
        ):
            raise IntegrityError(
                "INSERT INTO ai_profile_draft_field",
                params,
                Exception("Duplicate entry 'uk_ai_profile_draft_field'"),
            )
        return super().insert_draft_field(params)

    def fields_for_draft(self, draft_id: str) -> list[dict[str, Any]]:
        rows = super().fields_for_draft(draft_id)
        for row in rows:
            if "value_json" not in row:
                value = row.get("value")
                row["value_json"] = (
                    json.dumps(value, ensure_ascii=False)
                    if value is not None
                    else None
                )
        return rows


class _FakeSessionFactory:
    """把 WS 路由的 ``async with _db_session_factory() as db`` 接到内存假库。"""

    def __init__(self, store: _WSProfileStore) -> None:
        self._store = store

    def __call__(self) -> "_FakeSessionFactory":
        return self

    async def __aenter__(self) -> Any:
        return self._store.db

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture()
def build_env(
    monkeypatch: pytest.MonkeyPatch,
) -> _WSProfileStore:
    """WS 建构模式测试环境：内存假库 + 同步抽取 worker + feature 门禁。

    content_filter 敏感词全局缓存隔离（60s TTL），口径同
    tests/test_master_extract_handler.profile_store。
    """
    from app.services.content_filter import clear_sensitive_word_cache

    clear_sensitive_word_cache()
    store = _WSProfileStore()

    async def _run_extract(record: Any) -> None:
        # 真 worker 消费形态：先 claim（queued → leased）再 _process
        # （leased → running → 终态），与 ``_run_round`` 语义一致。
        from datetime import UTC, datetime

        from app.services.ai.tasks import claim_tasks

        now = datetime.now(UTC)
        claimed = await claim_tasks(store.db, "ws-worker", now, limit=5)
        for claimed_task in claimed:
            await worker_mod._process(store.db, claimed_task, "ws-worker")

    store.session.extract_runner = _run_extract
    factory = _FakeSessionFactory(store)
    monkeypatch.setattr(
        "app.api.routes.voice_moxiang._db_session_factory",
        factory,
        raising=False,
    )
    monkeypatch.setattr(db_session_module, "session_factory", factory)
    monkeypatch.setattr(settings, "ai_profile_enabled", True)
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    yield store  # type: ignore[misc]
    clear_sensitive_word_cache()


# ----------------------------------------------------------------------
# 建构模式 WS 协议
# ----------------------------------------------------------------------


def test_session_start_with_build_mode_creates_master_session(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_start(mode=profile_build) → session_ready 后收到 progress
    (percent 0, gate_met false)；DB 出现 kind='master' 会话。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build",
                 "subject": "personal"}
            )
        )
        msgs = _drain_until(ws, "ai_reply")

    # 协议顺序：session_ready → progress → 开场白 ai_reply。
    assert [m["type"] for m in msgs] == [
        "session_ready",
        "progress",
        "ai_reply",
    ]
    progress = msgs[1]
    assert progress["percent"] == 0.0
    assert progress["hard_done"] == 0
    assert progress["hard_total"] == len(MASTER_HARD_FIELD_KEYS)
    assert progress["entry_score"] == 0.0
    assert progress["gate_met"] is False

    # DB 出现 kind='master' 会话。
    assert len(store.sessions) == 1
    assert next(iter(store.sessions.values()))["session_kind"] == "master"


def test_subject_switch_creates_second_master_session_and_tags_progress(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个 WS 连接可以从 personal 阶段切到 ideal_partner 阶段。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build",
                 "subject": "personal"}
            )
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps(
                {"type": "subject_switch", "subject": "ideal_partner"}
            )
        )
        msgs = _drain_until(ws, "ai_reply")

    assert [m["type"] for m in msgs] == [
        "subject_changed",
        "progress",
        "ai_reply",
    ]
    assert msgs[0]["subject"] == "ideal_partner"
    assert msgs[1]["subject"] == "ideal_partner"
    assert len(store.sessions) == 2
    assert {row["subject"] for row in store.sessions.values()} == {
        "personal", "ideal_partner"
    }


def test_switching_back_reuses_both_master_sessions(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来回切换和断线重连都复用两个主体原有的 master 会话。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        first = _drain_until(ws, "ai_reply")
        assert first[0]["new_session"] is True
        assert first[0]["resumed"] is False
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "ideal_partner"}))
        _drain_until(ws, "ai_reply")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "personal"}))
        back = _drain_until(ws, "progress")

    assert back[0]["type"] == "subject_changed"
    assert back[0]["subject"] == "personal"
    original_ids = {
        row["subject"]: row["session_id"] for row in store.sessions.values()
    }
    assert len(original_ids) == 2

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "session_start",
                    "mode": "profile_build",
                    "subject": "ideal_partner",
                }
            )
        )
        _drain_until(ws, "progress")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "personal"}))
        _drain_until(ws, "progress")

    assert len(store.sessions) == 2
    assert {
        row["subject"]: row["session_id"] for row in store.sessions.values()
    } == original_ids


def test_reconnect_replays_subject_progress_and_confirm_card(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断线重连后，当前主体应恢复进度和待确认卡，而不是只恢复会话 ID。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "session_start",
                    "mode": "profile_build",
                    "subject": "personal",
                }
            )
        )
        _drain_until(ws, "ai_reply")

    session = next(iter(store.sessions.values()))
    draft = store._make_draft_row(
        draft_id="draft-reconnect-personal",
        user_id=10,
        subject="personal",
        revision=2,
        session_id=session["session_id"],
    )
    draft["status"] = "awaiting_confirmation"
    store.drafts.append(draft)
    store.drafts_by_id[draft["draft_id"]] = draft
    field = store._make_draft_field_row(
        draft_id=draft["draft_id"],
        field_key="interest_tags",
        subject="personal",
        value=["看展"],
        status="suggested",
    )
    field.update(
        {
            "field_kind": "structured",
            "category": "interests",
            "content": None,
        }
    )
    store.draft_fields.append(field)

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "session_start",
                    "mode": "profile_build",
                    "subject": "personal",
                }
            )
        )
        replay = _drain_until(ws, "confirm_card", timeout=1.0)

    assert [message["type"] for message in replay] == [
        "session_ready",
        "progress",
        "confirm_card",
    ]
    assert replay[1]["subject"] == "personal"
    assert replay[1]["percent"] == 0.0
    assert replay[2]["subject"] == "personal"
    assert replay[2]["draft_id"] == draft["draft_id"]
    assert replay[2]["expected_revision"] == 2
    assert replay[2]["items"][0]["field_key"] == "interest_tags"


def test_reconnect_reuses_history_without_repeating_opening(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有 master 会话重连只恢复历史，不再推送一次性开场白。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        first = _drain_until(ws, "ai_reply")
        assert first[0]["new_session"] is True
        assert first[0]["resumed"] is False

    session = next(iter(store.sessions.values()))
    awaitable = store.seed_turn(session["session_id"], "old-user", "我最近在杭州工作")
    import asyncio

    asyncio.run(awaitable)
    store.turns.append(
        {
            "turn_id": "old-assistant",
            "session_id": session["session_id"],
            "client_turn_id": "old-assistant",
            "user_id": 10,
            "turn_no": 2,
            "role": "assistant",
            "answer_text": "记下了，你最近在杭州工作。",
            "status": "saved",
            "source_type": "assistant_reply",
            "created_at": _now(),
        }
    )

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        first = _drain_until(ws, "progress")
        assert [m["type"] for m in first] == ["session_ready", "progress"]
        assert first[0]["new_session"] is False
        assert first[0]["resumed"] is True
        with pytest.raises(AssertionError):
            _recv_json(ws, timeout=0.2)


def test_reconnect_hydrates_persisted_history_into_next_provider_prompt(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重连后下一轮 provider prompt 必须包含已有 user/assistant turns。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    captured: list[list[dict[str, str]]] = []

    class _CapturingProvider:
        async def stream_chat(self, messages, *, json_mode=False):
            captured.append(messages)
            yield ("content", "继续说说")
            yield ("finish", "stop")

    monkeypatch.setattr(
        "app.services.voice.master_orchestrator.get_provider",
        lambda name: _CapturingProvider(),
    )
    store = build_env
    token = _make_access_token(user_id=10)

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        _drain_until(ws, "ai_reply")

    session = next(iter(store.sessions.values()))
    import asyncio

    asyncio.run(store.seed_turn(session["session_id"], "history-user", "我最近在杭州工作"))
    store.turns.append(
        {
            "turn_id": "history-assistant",
            "session_id": session["session_id"],
            "client_turn_id": "history-assistant",
            "user_id": 10,
            "turn_no": 2,
            "role": "assistant",
            "answer_text": "记下了，你最近在杭州工作。",
            "status": "saved",
            "source_type": "assistant_reply",
            "created_at": _now(),
        }
    )

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        _drain_until(ws, "progress")
        ws.send_text(
            json.dumps(
                {
                    "type": "text_message",
                    "text": "我还想聊聊最近的生活",
                    "clientTurnId": "history-next",
                }
            )
        )
        _drain_until(ws, "ai_reply")

    prompt = captured[-1]
    assert {m["role"] for m in prompt} >= {"user", "assistant"}
    assert any("我最近在杭州工作" in m["content"] for m in prompt)
    assert any("记下了，你最近在杭州工作。" in m["content"] for m in prompt)


def test_both_subjects_replay_scoped_progress_card_and_publish_ready(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个主体各自重连/切换时，progress、confirm_card、publish_ready 不串线。"""
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    store = build_env
    token = _make_access_token(user_id=10)

    # 建立同一用户的两个 master 会话，模拟用户先后访问两个主体。
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build", "subject": "personal"}
            )
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "ideal_partner"}))
        _drain_until(ws, "ai_reply")

    for subject in ("personal", "ideal_partner"):
        session = next(row for row in store.sessions.values() if row["subject"] == subject)
        draft = store._make_draft_row(
            draft_id=f"draft-ready-{subject}",
            user_id=10,
            subject=subject,
            revision=2,
            session_id=session["session_id"],
        )
        draft["status"] = "awaiting_confirmation"
        store.drafts.append(draft)
        store.drafts_by_id[draft["draft_id"]] = draft
        for field_key in (*sorted(MASTER_HARD_FIELD_KEYS), "interest_tags", "occupation_group", "education_level"):
            field = store._make_draft_field_row(
                draft_id=draft["draft_id"],
                field_key=field_key,
                subject=subject,
                value="confirmed-value",
                status="confirmed",
            )
            field["field_kind"] = "structured"
            store.draft_fields.append(field)
        entry = store._make_draft_field_row(
            draft_id=draft["draft_id"],
            field_key=f"entry_{subject}",
            subject=subject,
            value=None,
            status="suggested",
        )
        entry.update(
            {
                "field_kind": "entry",
                "category": "values",
                "content": f"{subject} candidate",
                "value_json": None,
            }
        )
        store.draft_fields.append(entry)

    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build", "subject": "personal"}
            )
        )
        personal = _drain_until(ws, "publish_ready")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "ideal_partner"}))
        ideal = _drain_until(ws, "publish_ready")

    assert [message["type"] for message in personal] == [
        "session_ready",
        "progress",
        "confirm_card",
        "publish_ready",
    ]
    assert personal[1]["subject"] == "personal"
    assert personal[1]["gate_met"] is True
    assert personal[2]["subject"] == "personal"
    assert personal[2]["items"][0]["content"] == "personal candidate"
    assert personal[3]["subject"] == "personal"
    assert personal[3]["summary"] == "你的个人画像已经可以成稿了"

    assert [message["type"] for message in ideal] == [
        "subject_changed",
        "progress",
        "confirm_card",
        "publish_ready",
    ]
    assert ideal[0]["subject"] == "ideal_partner"
    assert ideal[1]["subject"] == "ideal_partner"
    assert ideal[1]["gate_met"] is True
    assert ideal[2]["subject"] == "ideal_partner"
    assert ideal[2]["items"][0]["content"] == "ideal_partner candidate"
    assert ideal[3]["subject"] == "ideal_partner"
    assert ideal[3]["summary"] == "你的愿遇之相已经可以成稿了"


def test_invalid_subject_switch_keeps_current_subject(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        _drain_until(ws, "ai_reply")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "someone_else"}))
        error = _recv_json(ws)
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "personal"}))
        back = _drain_until(ws, "progress")

    assert error["type"] == "error"
    assert error["code"] == "AI_INPUT_INVALID"
    assert back[0]["subject"] == "personal"


def test_text_after_subject_switch_isolated_to_ideal_partner_draft(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切换后用户表达只写入当前 ideal_partner 会话和草稿。"""
    def respond(request: Any) -> InvokeOutcome:
        if request.subject == "ideal_partner":
            return InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    patches=(
                        ExtractedPatch(
                            action="add",
                            category="personality",
                            content="希望对方温柔而稳定",
                            subject=ProfileSubject.IDEAL_PARTNER,
                            source_quote="我希望未来伴侣温柔而稳定",
                            confidence=0.9,
                        ),
                    ),
                )
            )
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build",
                 "subject": "personal"}
            )
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps(
                {"type": "subject_switch", "subject": "ideal_partner"}
            )
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps(
                {
                    "type": "text_message",
                    "text": "我希望未来伴侣温柔而稳定",
                    "clientTurnId": "ct-ideal-001",
                }
            )
        )
        _drain_until(ws, "ai_reply")
        push_msgs = _drain_until(ws, "confirm_card", limit=10)

    session_rows = [
        row for row in store.sessions.values() if row["subject"] == "ideal_partner"
    ]
    assert len(session_rows) == 1
    ideal_session_id = session_rows[0]["session_id"]
    assert all(
        turn["session_id"] == ideal_session_id
        for turn in store.turns
        if turn["role"] == "user"
    )
    assert len(store.draft_fields) == 1
    assert store.draft_fields[0]["subject"] == "ideal_partner"
    assert push_msgs[0]["subject"] == "ideal_partner"
    assert push_msgs[-1]["subject"] == "ideal_partner"


def test_background_extract_keeps_original_subject_after_switch(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧主体抽取在切换后完成时，进度和卡片仍标记旧主体。"""
    def respond(request: Any) -> InvokeOutcome:
        if request.session_kind == "master" and request.subject == "personal":
            return InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    patches=(
                        ExtractedPatch(
                            action="add",
                            category="values",
                            content="重视关系中的坦诚沟通",
                            subject=ProfileSubject.PERSONAL,
                            source_quote="我很重视坦诚沟通",
                            confidence=0.9,
                        ),
                    ),
                )
            )
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start", "mode": "profile_build"}))
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps(
                {
                    "type": "text_message",
                    "text": "我很重视坦诚沟通",
                    "clientTurnId": "ct-personal-background",
                }
            )
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(json.dumps({"type": "subject_switch", "subject": "ideal_partner"}))
        _drain_until(ws, "ai_reply")
        background = _drain_until(ws, "confirm_card")

    subject_messages = [
        msg for msg in background if msg["type"] in {"progress", "confirm_card"}
    ]
    assert subject_messages
    assert all(msg["subject"] == "personal" for msg in subject_messages)


def test_text_message_persists_turn_and_enqueues(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text_message → ai_reply 正常；DB 出现 user+assistant turn；ai_task
    出现 profile_extract 行。fake gateway 让抽取任务同步完成后，收到
    confirm_card（fake provider 返回 1 patch）。"""

    def respond(request: Any) -> InvokeOutcome:
        if request.session_kind == "master":
            return InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    patches=(
                        ExtractedPatch(
                            action="add",
                            category="interests",
                            content="喜欢看展，周末常去美术馆",
                            subject=ProfileSubject.PERSONAL,
                            source_quote="周末喜欢看展",
                            confidence=0.9,
                        ),
                    ),
                )
            )
        # 缺失硬字段定向抽取：对话里没提，返回空。
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps({"type": "session_start", "mode": "profile_build"})
        )
        _drain_until(ws, "ai_reply")  # 开场白
        ws.send_text(
            json.dumps(
                {
                    "type": "text_message",
                    "text": "我周末喜欢看展，常去美术馆",
                    "clientTurnId": "ct-001",
                }
            )
        )
        reply_msgs = _drain_until(ws, "ai_reply")
        push_msgs = _drain_until(ws, "confirm_card", limit=10)

    # 流式回复链路不受建构链路影响。
    types = [m["type"] for m in reply_msgs]
    assert "ai_thinking" in types
    assert "ai_reply" in types

    # 轮次落库：user turn（原文）+ assistant turn（墨相师回复）。
    session_row = next(iter(store.sessions.values()))
    turns = [
        t for t in store.turns if t["session_id"] == session_row["session_id"]
    ]
    assert any(
        t["role"] == "user"
        and t["answer_text"] == "我周末喜欢看展，常去美术馆"
        for t in turns
    )
    assert any(t["role"] == "assistant" and t["answer_text"] for t in turns)

    # 入队：profile_extract 任务一行，且已被同步 worker 消费到 succeeded。
    tasks = [
        t
        for t in store.task_store.tasks.values()
        if t["task_type"] == "profile_extract"
    ]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "succeeded"

    # 抽取落草稿：1 条 suggested entry（本会话新建草稿，无历史种子行）。
    assert len(store.draft_fields) == 1
    row = store.draft_fields[0]
    assert row["field_kind"] == "entry"
    assert row["confirmation_status"] == "suggested"
    assert row["category"] == "interests"
    assert row["content"] == "喜欢看展，周末常去美术馆"

    # 终态后先推 progress（确认口径仍为 0：suggested 不计分），再推卡片。
    assert [m["type"] for m in push_msgs] == ["progress", "confirm_card"]
    assert push_msgs[0]["percent"] == 0.0
    card = push_msgs[1]
    assert card["card_id"].startswith("c-")
    assert card["draft_id"] == store.drafts[0]["draft_id"]
    assert card["expected_revision"] == 0
    assert len(card["items"]) == 1
    item = card["items"][0]
    assert item["field_key"] == row["field_key"]
    assert item["kind"] == "entry"
    assert item["category"] == "interests"
    assert item["content"] == "喜欢看展，周末常去美术馆"
    assert card["draft_id"]
    assert session_row["status"] == "awaiting_confirmation"


def test_session_start_without_mode_keeps_pure_chat(
    build_env: _WSProfileStore,
) -> None:
    """不带 mode 的 session_start：无会话创建、无 progress 推送（回归兼容），
    后续 text_message 不落 turn、不入队。"""
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(json.dumps({"type": "session_start"}))
        msgs = _drain_until(ws, "ai_reply")
        # 纯聊协议逐字节等价：session_ready 后直接开场白，无 progress。
        assert [m["type"] for m in msgs] == ["session_ready", "ai_reply"]
        assert msgs[1]["text"]
        ws.send_text(
            json.dumps({"type": "text_message", "text": "随便聊聊天气"})
        )
        chat = _drain_until(ws, "ai_reply")

    assert "progress" not in [m["type"] for m in chat]
    assert "confirm_card" not in [m["type"] for m in chat]
    assert store.sessions == {}
    assert store.turns == []
    assert store.drafts == []
    assert store.task_store.tasks == {}


def test_switching_from_build_to_pure_chat_does_not_write_profile_turn(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一连接重新声明纯聊后，不能继续沿用之前的 master 绑定。"""

    def respond(request: Any) -> InvokeOutcome:
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps({"type": "session_start", "mode": "profile_build"})
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(json.dumps({"type": "session_start"}))
        msgs = _drain_until(ws, "ai_reply")
        assert [m["type"] for m in msgs] == ["session_ready", "ai_reply"]
        ws.send_text(json.dumps({"type": "text_message", "text": "只聊聊天"}))
        _drain_until(ws, "ai_reply")

    assert len(store.sessions) == 1
    assert store.turns == []
    assert store.task_store.tasks == {}


def test_failed_build_restart_clears_previous_profile_binding(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败的后续建构请求进入纯聊，不能继续写入此前的 master 会话。"""

    def respond(request: Any) -> InvokeOutcome:
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps({"type": "session_start", "mode": "profile_build"})
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build", "subject": "bad"}
            )
        )
        failed = _drain_until(ws, "ai_reply")
        assert any(m.get("code") == "AI_INPUT_INVALID" for m in failed)
        ws.send_text(json.dumps({"type": "text_message", "text": "只聊聊天"}))
        chat = _drain_until(ws, "ai_reply")

    assert "confirm_card" not in [m["type"] for m in chat]
    assert len(store.sessions) == 1
    assert store.turns == []
    assert store.task_store.tasks == {}


# ----------------------------------------------------------------------
# fail-closed：建构通道建立失败不吞、不污染纯聊（控制器交接约束）
# ----------------------------------------------------------------------


def test_session_start_build_mode_without_consent_fails_closed(
    build_env: _WSProfileStore,
) -> None:
    """无 profile_text_extract 授权的用户开建构模式 → error AI_CONSENT_REQUIRED，
    不创建会话、不推进度；session_ready + 开场白照常（纯聊保底）。"""
    store = build_env
    token = _make_access_token(user_id=99)  # 假库只为 user 10 预置授权
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "session_start", "mode": "profile_build"}
            )
        )
        msgs = _drain_until(ws, "ai_reply")

    types = [m["type"] for m in msgs]
    assert types == ["error", "session_ready", "ai_reply"]
    assert msgs[0]["code"] == "AI_CONSENT_REQUIRED"
    assert store.sessions == {}


def test_session_start_build_mode_db_failure_fails_closed(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 异常 → error AI_TEMPORARILY_UNAVAILABLE，build_session_id 不处于
    可用状态（无 progress、无会话行）；纯聊链路不受影响。"""

    class _BrokenFactory:
        def __call__(self) -> "_BrokenFactory":
            return self

        async def __aenter__(self) -> Any:
            raise RuntimeError("db down")

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr(
        "app.api.routes.voice_moxiang._db_session_factory", _BrokenFactory()
    )
    store = build_env
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps({"type": "session_start", "mode": "profile_build"})
        )
        msgs = _drain_until(ws, "ai_reply")

    types = [m["type"] for m in msgs]
    assert types == ["error", "session_ready", "ai_reply"]
    assert msgs[0]["code"] == "AI_TEMPORARILY_UNAVAILABLE"
    assert "progress" not in types
    assert store.sessions == {}


def test_build_context_renders_missing_fields_in_chinese(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建构模式提示词注入：缺失硬字段经 _missing_label 渲染成中文标签
    （控制器交接项 2：英文 key 不得透传进 build 上下文）。"""
    captured_messages: list[dict[str, str]] = []

    class _CapturingProvider:
        async def stream_chat(self, messages, *, json_mode=False):
            captured_messages.extend(messages)
            yield ("content", "好的")
            yield ("finish", "stop")

    monkeypatch.setattr(
        "app.services.voice.master_orchestrator.get_provider",
        lambda name: _CapturingProvider(),
    )
    _install_gateway(
        monkeypatch,
        lambda request: InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        ),
    )
    token = _make_access_token(user_id=10)
    with client.websocket_connect(
        f"/api/v1/voice/moxiang-master?token={token}"
    ) as ws:
        ws.send_text(
            json.dumps({"type": "session_start", "mode": "profile_build"})
        )
        _drain_until(ws, "ai_reply")
        ws.send_text(
            json.dumps({"type": "text_message", "text": "从哪开始都行"})
        )
        _drain_until(ws, "ai_reply")

    build_msgs = [
        m["content"]
        for m in captured_messages
        if m["role"] == "system" and "画像建构模式" in m["content"]
    ]
    assert build_msgs, "建构模式上下文未注入"
    context = build_msgs[0]
    assert "年龄" in context and "婚姻状况" in context
    # 英文 key 不得原样透传。
    assert "city_code" not in context
    assert "marriage_status" not in context


# ----------------------------------------------------------------------
# 软删除行复活（控制器交接项 1）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_structured_field_revives_deleted_suggested_row(
    build_env: _WSProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户经 REST 删除建议硬字段行后（confirmation_status='deleted'，行仍占
    (draft_id, field_key) 唯一键），下一轮抽取重定位成功：UPDATE 复活而非
    INSERT——假库已强制唯一键，重复 INSERT 会当场撞 IntegrityError。"""

    def respond(request: Any) -> InvokeOutcome:
        if request.session_kind == "master":
            return InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version=PROFILE_SCHEMA_VERSION
                )
            )
        if request.target_field_key == "city_code":
            return InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    fields=(
                        ExtractedField(
                            field_key="city_code",
                            subject=ProfileSubject.PERSONAL,
                            value="330100",
                            source_quote="我在杭州上班",
                            confidence=0.92,
                        ),
                    ),
                )
            )
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    _install_gateway(monkeypatch, respond)
    store = build_env
    session = await store.seed_master_session(status="extracting")
    sid = session["session_id"]

    # 第一轮：city_code 以 suggested 行落活动草稿。
    task1 = await _seed_turn_task(store, sid, "turn-001", "master-key-001")
    assert await worker_mod._process(store.db, task1, "worker-1") == "completed"
    city_rows = [r for r in store.draft_fields if r["field_key"] == "city_code"]
    assert len(city_rows) == 1
    assert city_rows[0]["confirmation_status"] == "suggested"

    # 用户经 REST 软删除该建议行（行保留占键）。
    city_rows[0]["confirmation_status"] = "deleted"

    # 第二轮：会话重新进入 extracting，city_code 回到缺失口径 → 重定位。
    session["status"] = "extracting"
    task2 = await _seed_turn_task(store, sid, "turn-002", "master-key-002")
    # 租约属于 worker-1（_seed_turn_task 固定），第二轮必须同一 worker 认领。
    assert await worker_mod._process(store.db, task2, "worker-1") == "completed"

    # 复活而非新增：仍只有一行，状态回到 suggested，值刷新。
    city_rows = [r for r in store.draft_fields if r["field_key"] == "city_code"]
    assert len(city_rows) == 1
    assert city_rows[0]["confirmation_status"] == "suggested"
    assert city_rows[0]["value"] == "330100"
    assert store.sessions[sid]["status"] == "awaiting_confirmation"
    assert all(t["role"] == "user" for t in store.turns)


# TestClient 需要在模块级可用。
client = TestClient(app)
