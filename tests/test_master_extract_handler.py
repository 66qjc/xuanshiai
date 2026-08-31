"""master 抽取分支（设计 Task 6）：entry+structured 落草稿 / 空 patch 合法 / 白名单外不落库。

fake provider/gateway 注入形态仿 ``tests/test_ai_profile_publish.py``（其
``ProfileStore`` 在 Task 7 假库上补 ``ai_profile_revision`` 路由），另在
``MasterFakeSession`` 补 master 抽取分支需要的三处 SQL 路由：
``_load_session_dialogue``（用户陈述按 turn_no 排序）、``_load_field_keys`` 的
JOIN 查询（publish 假库的 draft_field 通用路由按 :draft_id 取行，接不住按
session_id 聚合的这条）与 draft_field 行的 field_kind/category/content 补齐。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import pytest

import app.services.ai.profile as profile_mod
from app.core.config import settings
from app.schemas.ai_profile import ProfileSubject
from app.services.content_filter import clear_sensitive_word_cache
from app.services.ai.base import (
    ExtractedField,
    ExtractedPatch,
    StructuredExtractResult,
)
from app.services.ai.gateway import InvokeOutcome
from app.services.ai.profile import (
    MASTER_HARD_FIELD_KEYS,
    PROFILE_POLICY_REVISION,
    PROFILE_SCHEMA_VERSION,
    create_master_session,
    extract_profile_turn,
    load_owned_session,
    submit_profile_turn,
)
from app.services.ai.prompts.profile_extract import (
    build_profile_master_extract_prompt,
)
from app.workers import ai_worker as worker_mod
from tests.test_ai_profile_publish import ProfileStore as PublishProfileStore
from tests.test_ai_profile_publish import PublishFakeSession
from tests.test_ai_profile_sessions import _MappingResult, _WriteResult, _now


class MasterFakeSession(PublishFakeSession):
    """在 publish 假库路由之上补 master 抽取分支需要的 SQL 路由。"""

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | Any:
        sql = str(statement)
        values = dict(params or {})
        store = self._store
        # ``_load_session_dialogue``：会话内用户陈述按 turn_no 排序。
        if "FROM ai_profile_turn" in sql and "role = 'user'" in sql:
            rows = sorted(
                (
                    r
                    for r in store.turns
                    if r["session_id"] == values["session_id"]
                    and r["role"] == "user"
                ),
                key=lambda r: r["turn_no"],
            )
            return _MappingResult(
                [{"answer_text": r["answer_text"]} for r in rows]
            )
        # ``_load_field_keys`` 的 JOIN 查询：structured 字段的 key/确认态。
        if (
            "FROM ai_profile_draft_field" in sql
            and "df.field_kind = 'structured'" in sql
        ):
            draft_ids = {
                d["draft_id"]
                for d in store.drafts
                if d["session_id"] == values["session_id"]
            }
            draft_ids |= {
                d["draft_id"]
                for d in store.drafts_by_id.values()
                if d.get("session_id") == values["session_id"]
            }
            rows = [
                {
                    "field_key": f["field_key"],
                    "confirmation_status": f["confirmation_status"],
                }
                for f in store.draft_fields
                if f["draft_id"] in draft_ids
                and f.get("field_kind", "structured") == "structured"
                and f["confirmation_status"] != "deleted"
            ]
            return _MappingResult(rows)
        # ``_load_active_draft_id_for_session``：按 session_id 取最新可编辑草稿
        # （publish 假库的 draft 通用路由按 :draft_id 取行，接不住这条）。
        if "FROM ai_profile_draft " in sql and "session_id = :session_id" in sql:
            editable = {"draft", "extracting", "awaiting_confirmation", "paused"}
            candidates = [
                d
                for d in store.drafts
                if d["session_id"] == values["session_id"]
                and d["status"] in editable
            ]
            if candidates:
                candidates.sort(key=lambda d: d["updated_at"], reverse=True)
                return _MappingResult([{"draft_id": candidates[0]["draft_id"]}])
            return _MappingResult([])
        # master 建壳 INSERT（带 :session_id）：走 Task 7 的 insert_draft，
        # 而不是 publish 假库按 restore 形态落 session_id=None 的路由。
        if "INSERT INTO ai_profile_draft" in sql and values.get("session_id"):
            store.insert_draft(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_draft_field" in sql:
            # master 写入层 entry 行以 SQL 字面量写 field_kind='entry'，
            # structured 行以字面量 'structured' 写；假库按真实 DB 语义补齐。
            if "field_kind" not in values:
                values = {
                    **values,
                    "field_kind": "entry" if "'entry'" in sql else "structured",
                }
            return await super().execute(statement, values)
        return await super().execute(statement, params)


class MasterProfileStore(PublishProfileStore):
    """publish 假库 + master 会话种子与 draft_field 行扩展列。"""

    def __init__(self) -> None:
        super().__init__()
        self.session = MasterFakeSession(self)
        self.db = self.session

    def insert_draft_field(self, params: dict[str, Any]) -> dict[str, Any]:
        row = super().insert_draft_field(params)
        # entry 行的正文在 content、value_json 恒 NULL；structured 行反之。
        # Task 7 假库不记录这四列，master 断言需要，按真实 DB 语义补齐。
        row["field_kind"] = str(params.get("field_kind") or "structured")
        row["category"] = params.get("category")
        row["content"] = params.get("content")
        row["replaces_field_key"] = params.get("replaces_field_key")
        return row

    async def seed_master_session(
        self,
        owner_user_id: int = 10,
        subject: str = "personal",
        status: str = "extracting",
    ) -> dict[str, Any]:
        row = await self.seed_session(
            owner_user_id=owner_user_id, subject=subject, status=status
        )
        row["session_kind"] = "master"
        return row


@pytest.fixture
def profile_store() -> MasterProfileStore:
    # 隔离 content_filter 的敏感词全局缓存（60s TTL）：turn 提交路径会触发
    # moderate_text，其他测试残留的词表可能误拒；退出时清掉本假库加载的
    # 空词表（口径同 tests/test_ai_profile_sessions.profile_store）。
    clear_sensitive_word_cache()
    store = MasterProfileStore()
    prior = worker_mod.TASK_HANDLERS.get("profile_extract")
    worker_mod.TASK_HANDLERS["profile_extract"] = extract_profile_turn
    yield store
    if prior is None:
        worker_mod.TASK_HANDLERS.pop("profile_extract", None)
    else:
        worker_mod.TASK_HANDLERS["profile_extract"] = prior
    clear_sensitive_word_cache()


def _master_gateway(
    requests: list[Any], responder: Callable[[Any], InvokeOutcome]
) -> type:
    """按请求形态分发的 fake gateway：记录全部请求，outcome 由测试闭包给出。"""

    class _StubGateway:
        def __init__(self, *, timeout_seconds: float = 30.0) -> None:
            self.timeout_seconds = timeout_seconds

        async def structured_extract(
            self, context: Any, request: Any
        ) -> InvokeOutcome:
            requests.append(request)
            return responder(request)

    return _StubGateway


_CONSENT_SNAPSHOT_JSON = {
    "scope": "profile_text_extract",
    "version": "profile-text-v1",
    "policy_revision": "ai-policy-2026-08-07-v1",
}
_SOURCE_REVISION_JSON = {
    "profile": 1,
    "preference": 0,
    "privacy": 0,
    "relationship": 0,
    "policy": 0,
}


def _payload_summary(session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "turn_id": turn["turn_id"],
        "client_turn_id": turn["client_turn_id"],
        "subject": "personal",
    }


async def _seed_turn_task(
    store: MasterProfileStore,
    session_id: str,
    client_turn_id: str,
    idempotency_key: str,
) -> Any:
    """在既有会话上种子一条用户 turn + 租约内抽取任务（多轮运行复用）。"""
    turn = await store.seed_turn(
        session_id, client_turn_id, "我在杭州上班，今年28岁，未婚，周末喜欢看展"
    )
    return await store.task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=_now() + timedelta(seconds=60),
        task_type="profile_extract",
        idempotency_key=idempotency_key,
        request_digest="digest",
        consent_snapshot_json=_CONSENT_SNAPSHOT_JSON,
        source_revision_json=_SOURCE_REVISION_JSON,
        payload_summary=_payload_summary(session_id, turn),
    )


def _enable_profile_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """完成门禁（complete_task 的 release gate 复查）要求画像特性开关打开；
    conftest 的 _hermetic_ai_settings 把它重置为 False，这里按
    test_ai_profile_sessions._enable_profile_feature 的口径打开。"""
    monkeypatch.setattr(settings, "ai_profile_enabled", True)
    monkeypatch.setattr(settings, "ai_master_enabled", True)


async def _run_master_extract(
    store: MasterProfileStore,
    monkeypatch: pytest.MonkeyPatch,
    gateway_type: type,
    *,
    status: str = "extracting",
) -> dict[str, Any]:
    """种子 master 会话 + 用户 turn + 租约内抽取任务，跑一次 worker 处理。"""
    session = await store.seed_master_session(status=status)
    task = await _seed_turn_task(
        store, session["session_id"], "turn-001", "master-extract-key-001"
    )
    monkeypatch.setattr(profile_mod, "AIGateway", gateway_type)
    _enable_profile_gate(monkeypatch)
    worker_outcome = await worker_mod._process(store.db, task, "worker-1")
    final = await store.task_store.get(task.task_id)
    assert final is not None
    return {
        "worker_outcome": worker_outcome,
        "task": final,
        "session_id": session["session_id"],
    }


def _session_draft_rows(store: MasterProfileStore, session_id: str) -> list[dict[str, Any]]:
    drafts = [d for d in store.drafts if d["session_id"] == session_id]
    draft_ids = {d["draft_id"] for d in drafts}
    return drafts, [
        f for f in store.draft_fields if f["draft_id"] in draft_ids
    ]


@pytest.mark.asyncio
async def test_master_patches_land_as_suggested_rows(profile_store, monkeypatch) -> None:
    """provider 返回 1 entry + 1 structured(city_code) → 草稿出现两行 suggested，
    会话状态 awaiting_confirmation，任务 result 含 draft_id。"""
    requests: list[Any] = []

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
        # 其余硬字段：对话里没提，定向抽取返回空。
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    store = profile_store
    result = await _run_master_extract(
        store, monkeypatch, _master_gateway(requests, respond)
    )

    assert result["worker_outcome"] == "completed"
    assert result["task"]["status"] == "succeeded"
    assert str(result["task"]["result_ref"]).startswith("profile-draft:")

    # 首个请求是 master 对话契约请求；其后每个缺失硬字段一次定向抽取。
    assert requests[0].session_kind == "master"
    assert requests[0].target_field_key is None
    assert len(requests) == 1 + len(MASTER_HARD_FIELD_KEYS)
    assert {r.target_field_key for r in requests[1:]} == set(MASTER_HARD_FIELD_KEYS)

    # 草稿：一个壳 + 两行 suggested（entry + structured）。
    drafts, rows = _session_draft_rows(store, result["session_id"])
    assert len(drafts) == 1
    assert len(rows) == 2
    assert all(r["confirmation_status"] == "suggested" for r in rows)
    entry_rows = [r for r in rows if r["field_kind"] == "entry"]
    assert len(entry_rows) == 1
    assert entry_rows[0]["category"] == "interests"
    assert entry_rows[0]["content"] == "喜欢看展，周末常去美术馆"
    structured_rows = [r for r in rows if r["field_kind"] == "structured"]
    assert [r["field_key"] for r in structured_rows] == ["city_code"]

    # 会话推进 awaiting_confirmation；全程不写 assistant 澄清 turn。
    assert store.sessions[result["session_id"]]["status"] == "awaiting_confirmation"
    assert all(t["role"] == "user" for t in store.turns)


@pytest.mark.asyncio
async def test_master_empty_patches_is_noop_success(profile_store, monkeypatch) -> None:
    """provider 返回 0 patch 0 question → 任务 succeeded、无草稿行、
    会话保持 draft、不新增 assistant turn。"""
    requests: list[Any] = []
    empty = InvokeOutcome(
        result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
    )
    store = profile_store
    result = await _run_master_extract(
        store,
        monkeypatch,
        _master_gateway(requests, lambda request: empty),
        status="draft",
    )

    assert result["worker_outcome"] == "completed"
    assert result["task"]["status"] == "succeeded"
    assert result["task"]["result_ref"] == "profile-master:no-op"

    drafts, rows = _session_draft_rows(store, result["session_id"])
    assert drafts == []
    assert rows == []

    # 会话保持 draft（不得推进 awaiting_confirmation、不得置 failed）。
    assert store.sessions[result["session_id"]]["status"] == "draft"
    assert all(t["role"] == "user" for t in store.turns)


@pytest.mark.asyncio
async def test_master_out_of_whitelist_patch_rejected(profile_store, monkeypatch) -> None:
    """provider 返回 category 不在白名单的 patch → 终态失败 AI_INPUT_INVALID
    （与 update 同纪律），会话不写行。"""
    requests: list[Any] = []
    # model_construct 绕过 Pydantic 校验：模拟适配器放行了白名单外分类，
    # worker 边界复核必须整单拒绝（不静默过滤）。
    bad_outcome = InvokeOutcome(
        result=StructuredExtractResult.model_construct(
            schema_version=PROFILE_SCHEMA_VERSION,
            patches=(
                ExtractedPatch.model_construct(
                    action="add",
                    category="mood",
                    content="用户今天心情很好",
                    subject=ProfileSubject.PERSONAL,
                    source_quote="今天心情很好",
                    source_span="今天心情很好",
                    confidence=0.9,
                    schema_version=PROFILE_SCHEMA_VERSION,
                    prompt_version="profile-extract-prompt-v1",
                    policy_revision=PROFILE_POLICY_REVISION,
                ),
            ),
        )
    )
    store = profile_store
    result = await _run_master_extract(
        store,
        monkeypatch,
        _master_gateway(requests, lambda request: bad_outcome),
    )

    assert result["task"]["status"] == "failed"
    assert result["task"]["error_code"] == "AI_INPUT_INVALID"
    # 白名单拒绝发生在 master 分支的边界复核（请求必须带 master 契约）。
    assert requests and requests[0].session_kind == "master"

    # 终态失败：无行落库，会话置 failed，无 assistant 澄清 turn。
    drafts, rows = _session_draft_rows(store, result["session_id"])
    assert drafts == []
    assert rows == []
    assert store.sessions[result["session_id"]]["status"] == "failed"
    assert all(t["role"] == "user" for t in store.turns)


def test_master_extract_prompt_carries_contract_and_digest() -> None:
    """master 契约段：允许 0 条 patch、禁止澄清问题（澄清由墨相师承担）。"""
    prompt = build_profile_master_extract_prompt(
        "personal",
        ("我最近搬到杭州工作，周末喜欢看展",),
        entry_digest="entry_values_seed01｜价值观：欣赏踏实上进的人",
    )
    assert "patches" in prompt
    assert "clarifying_question" in prompt
    assert "null" in prompt  # 契约示例中 clarifying_question 恒为 null
    assert "禁止输出 clarifying_question" in prompt
    assert "空数组" in prompt  # 0 条 patch 是合法结果
    assert "禁止编造" in prompt  # faithfulness 硬约束
    assert "entry_values_seed01｜价值观" in prompt  # modify 目标可定位
    assert "我最近搬到杭州工作" in prompt


@pytest.mark.asyncio
async def test_master_noop_bounces_session_back_to_draft(profile_store, monkeypatch) -> None:
    """空回合不卡死：no-op 后会话从 extracting 回弹 draft，对话可继续
    （与 update 澄清回路的 EXTRACTING→DRAFT 边同款守卫/迁移写法）。"""
    requests: list[Any] = []
    empty = InvokeOutcome(
        result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
    )
    store = profile_store
    result = await _run_master_extract(
        store,
        monkeypatch,
        _master_gateway(requests, lambda request: empty),
        status="extracting",
    )

    assert result["task"]["status"] == "succeeded"
    assert result["task"]["result_ref"] == "profile-master:no-op"
    # 关键断言：不再是 extracting（会话不卡死在"提取中"）。
    assert store.sessions[result["session_id"]]["status"] == "draft"

    drafts, rows = _session_draft_rows(store, result["session_id"])
    assert drafts == []
    assert rows == []
    assert all(t["role"] == "user" for t in store.turns)


@pytest.mark.asyncio
async def test_master_session_has_no_question_bank_after_turn(profile_store) -> None:
    """master 会话无题库推进：创建与 turn 提交后 current_question 恒 None
    （信息由墨相师对话自然收集，不硬走问答推进）。"""
    store = profile_store
    session = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    assert session.current_question is None

    await submit_profile_turn(
        store.db,
        session.session_id,
        10,
        "turn-001",
        "我在杭州上班，周末喜欢看展",
        "master-turn-key-001",
    )
    reloaded = await load_owned_session(store.db, session.session_id, 10)
    assert reloaded.session_kind == "master"
    assert reloaded.current_question is None


@pytest.mark.asyncio
async def test_master_hard_field_not_retargeted_when_suggested_in_draft(
    profile_store, monkeypatch
) -> None:
    """活动草稿已有 suggested city_code 行 → 第二轮 missing 不含 city_code：
    不重复定向抽取、不重复 INSERT（防 (draft_id, field_key) 唯一键撞车）。"""
    store = profile_store
    session = await store.seed_master_session(status="extracting")
    sid = session["session_id"]
    _enable_profile_gate(monkeypatch)

    def make_responder(with_city_code: bool) -> Any:
        def respond(request: Any) -> InvokeOutcome:
            if request.session_kind == "master":
                return InvokeOutcome(
                    result=StructuredExtractResult(
                        schema_version=PROFILE_SCHEMA_VERSION
                    )
                )
            if request.target_field_key == "city_code" and with_city_code:
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

        return respond

    # 第一轮：city_code 定向抽取命中，以 suggested 行落活动草稿。
    round1_requests: list[Any] = []
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _master_gateway(round1_requests, make_responder(with_city_code=True)),
    )
    task1 = await _seed_turn_task(store, sid, "turn-001", "master-extract-key-101")
    assert await worker_mod._process(store.db, task1, "worker-1") == "completed"
    drafts, rows = _session_draft_rows(store, sid)
    assert len(drafts) == 1
    assert len([r for r in rows if r["field_key"] == "city_code"]) == 1

    # 第二轮：会话重新进入 extracting（模拟第二次 turn 提交）。
    session["status"] = "extracting"
    round2_requests: list[Any] = []
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _master_gateway(round2_requests, make_responder(with_city_code=True)),
    )
    task2 = await _seed_turn_task(store, sid, "turn-002", "master-extract-key-102")
    assert await worker_mod._process(store.db, task2, "worker-1") == "completed"

    # 关键断言：city_code 已在活动草稿（suggested）→ 第二轮不再定向抽取
    # （过滤掉请求列表里的 master 对话请求，它不带 target_field_key）。
    assert {r.target_field_key for r in round2_requests[1:]} == (
        set(MASTER_HARD_FIELD_KEYS) - {"city_code"}
    )
    # 且不重复 INSERT：city_code 行仍只有一条。
    _, rows2 = _session_draft_rows(store, sid)
    assert len([r for r in rows2 if r["field_key"] == "city_code"]) == 1


@pytest.mark.asyncio
async def test_master_forged_hard_field_skipped_entry_still_lands(
    profile_store, monkeypatch
) -> None:
    """伪造 subject/span 的硬字段被整字段跳过（无行落库，数据层 fail-closed），
    entry patch 照常落库（一个坏定向调用不拖垮 entry）。"""
    store = profile_store
    requests: list[Any] = []

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
        if request.target_field_key == "city_code":
            # 跨 subject 伪造（model_construct 绕过 Pydantic 校验；容器同款
            # model_construct，否则结果模型构造期会先拦掉伪造子项）。
            return InvokeOutcome(
                result=StructuredExtractResult.model_construct(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    fields=(
                        ExtractedField.model_construct(
                            field_key="city_code",
                            subject=ProfileSubject.IDEAL_PARTNER,
                            value="330100",
                            source_quote="我在杭州上班",
                            confidence=0.92,
                            schema_version=PROFILE_SCHEMA_VERSION,
                        ),
                    ),
                )
            )
        if request.target_field_key == "age":
            # span/quote 证据不一致伪造。
            return InvokeOutcome(
                result=StructuredExtractResult.model_construct(
                    schema_version=PROFILE_SCHEMA_VERSION,
                    fields=(
                        ExtractedField.model_construct(
                            field_key="age",
                            subject=ProfileSubject.PERSONAL,
                            value=28,
                            source_quote="今年二十八",
                            source_span="今年二十九",
                            confidence=0.9,
                            schema_version=PROFILE_SCHEMA_VERSION,
                        ),
                    ),
                )
            )
        return InvokeOutcome(
            result=StructuredExtractResult(schema_version=PROFILE_SCHEMA_VERSION)
        )

    result = await _run_master_extract(
        store, monkeypatch, _master_gateway(requests, respond)
    )

    # 跳过而非终态失败：任务 succeeded，entry 照常落库。
    assert result["task"]["status"] == "succeeded"
    sid = result["session_id"]
    drafts, rows = _session_draft_rows(store, sid)
    assert len(drafts) == 1
    assert [r["field_kind"] for r in rows] == ["entry"]
    assert [r["category"] for r in rows] == ["interests"]
    # 三个硬字段都被尝试过（单字段跳过不终止其余字段）。
    assert {r.target_field_key for r in requests[1:]} == set(MASTER_HARD_FIELD_KEYS)
    assert store.sessions[sid]["status"] == "awaiting_confirmation"
