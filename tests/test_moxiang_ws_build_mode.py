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
