# 墨相师对话建构（Moxiang Conversational Profile）实施计划（已于 2026-09-02 被实时整理旅程替代）

> 历史记录：本计划描述的 `profile_build`、确认式 `progress` 和 master
> `profile_extract` 已退役。当前对外协议以
> [`docs/api/墨相师实时整理WebSocket.md`](../../api/墨相师实时整理WebSocket.md) 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把画像建构从固定题库问答改为墨相师 WS 对话——聊天中自然抽取落库、阶段轻确认、硬字段+条目折算 60% 门槛，题库降级为兜底。

**Architecture:** 扩展 `/api/v1/voice/moxiang-master` WS 通道：`session_start` 带 `mode=profile_build` 时创建 `session_kind='master'` 画像会话；每轮复用 `submit_profile_turn`（审核+落库+入队 `profile_extract`）；handler 新增 master 分支（对话抽取 entry + 缺失硬字段 structured，白名单过滤）；WS 侧后台轮询任务终态后推送 `progress`/`confirm_card`/`publish_ready`；确认/发布复用现有 REST。前端墨相师页加进度条+确认卡片+发布引导+退出修复；我的页入口直指墨相师。

**Tech Stack:** FastAPI WebSocket + MySQL(ai_task 队列) + DeepSeek/dots(OpenAI 兼容) + UniApp(uvue/uts) + pytest + node 断言测试。

**设计文档:** `docs/superpowers/specs/2026-08-30-moxiang-conversational-profile-design.md`（同分支内）

## Global Constraints

- 后端全部工作在 `xuanshiai-backend/.worktrees/moxiang-conv-profile`（分支 `feat/moxiang-conversational-profile`）；前端全部工作在 `xuanshiai-vue/.worktrees/moxiang-conv-profile`（分支 `feat/moxiang-conversational-profile-fe`）。**禁止在 main 提交；文档不单独提交**（spec+plan 随 Task 1 代码一起提交）。
- 后端测试命令：`"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest <path> -q`（在 BackendWT 目录执行）。
- 基线（不得新增红）：**759 passed / 6 failed / 76 errors**。6 红 = 5 个既有基线红（`test_voice_conversation`×3、`test_worker_lease_safety`、`test_start_task_rejects_expired_lease`）+ governance 路径问题（Task 1 修复后该红转绿）。76 errors 全部是 real_db 集成测试（本机 MySQL/Redis 未运行，主检出同样报错，属环境性，忽略）。
- 前端基线：工作树 20 passed / 13 failed（10 个既有红 + 3 个缺编译产物红）。新增测试只允许新增通过项。
- fail-closed 纪律：门禁检查失败必须拒绝；provider 不可用不得静默降级为假成功；抽取空结果不是失败。
- 用户消息落库走 `submit_profile_turn`（内容审核在落库前）；确认/编辑/删除一律走现有 REST（WS 只推送通知）。
- 所有用户可见文案为中文；代码注释用中文，风格与相邻代码一致。

---

### Task 1: 治理前置——PRODUCT.md 变更 + governance 测试路径修复 + 文档入库

**Files:**
- Modify: `D:\Users\ASUS\Desktop\宣誓爱\PRODUCT.md`（工作区根，不在 git 仓库内）
- Modify: `tests/test_ai_governance_contracts.py:1-6`（BackendWT）
- Commit 含（已在分支工作树、未跟踪）: `docs/superpowers/specs/2026-08-30-moxiang-conversational-profile-design.md`、`docs/superpowers/plans/2026-08-30-moxiang-conversational-profile.md`

**Interfaces:**
- Produces: `find_workspace_root() -> Path`（供 governance 测试使用）；PRODUCT.md 新增「墨相师对话建构」章节（后续任务的文案与门槛数字以此为准）。

- [ ] **Step 1: 更新 PRODUCT.md**

在工作区根 `D:\Users\ASUS\Desktop\宣誓爱\PRODUCT.md` 的 AI 画像章节后追加：

```markdown
## 墨相师对话建构（2026-08-30）

- 画像建构默认通过墨相师对话完成：AI 围绕白名单主题（自我认知、三观与感情观、
  生活方式与作息饮食、物理位置、基本情况）引导用户聊天，对话内容经 AI 抽取为
  画像条目/字段，未经用户确认不生效。
- 话题边界双层执行：提示词层越界温和拉回；抽取层仅接受白名单字段/分类，
  越界内容不抽取不落库。
- 建构门槛：硬信息（城市、年龄、婚姻状态）必须全部确认；总分（结构化字段
  1 分/个，条目 0.5 分/条且上限 2 分，满分 10）≥ 60% 方可发布（服务端可配置）。
- 确认节奏：对话中阶段性轻确认卡片（不阻塞对话）+ 发布前总览确认。
- 单入口：「我的」页建构入口直达墨相师；画像页保留为档案（成稿/条目管理/更新）。
  题库问答仅作为墨相师通道故障时的用户自选兜底，无常驻入口。
```

- [ ] **Step 2: 修复 governance 测试的工作区根定位**

`tests/test_ai_governance_contracts.py` 顶部改为向上查找 PRODUCT.md（工作树比主检出深一层，`parent` 假设失效）：

```python
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def find_workspace_root() -> Path:
    """向上查找包含 PRODUCT.md 的工作区根（兼容 worktree 目录深度）。"""
    current = BACKEND_ROOT
    for _ in range(5):
        if (current / "PRODUCT.md").exists():
            return current
        current = current.parent
    raise FileNotFoundError("PRODUCT.md not found above backend root")


def test_ai_product_and_security_decisions_are_frozen() -> None:
    product = (find_workspace_root() / "PRODUCT.md").read_text(encoding="utf-8")
```

（保留函数体其余部分不变，仅替换首行 `WORKSPACE_ROOT` 的取值来源。）

- [ ] **Step 3: 验证 governance 测试转绿**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_ai_governance_contracts.py -q`（在 BackendWT）
Expected: `1 passed`（若因 PRODUCT.md 新章节缺冻结决策断言而红，把新章节涉及的冻结决策同步补进 `docs/ai/AI_PRODUCT_SECURITY_DECISIONS.md`，以断言消息为准）

- [ ] **Step 4: 提交（spec + plan + 测试修复同一提交，不含 PRODUCT.md——它在仓库外）**

```bash
cd "/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.worktrees/moxiang-conv-profile"
git add docs/superpowers/specs/2026-08-30-moxiang-conversational-profile-design.md \
        docs/superpowers/plans/2026-08-30-moxiang-conversational-profile.md \
        tests/test_ai_governance_contracts.py
git commit -m "docs(moxiang): 墨相师对话建构设计/实施文档 + governance 测试工作树路径修复"
```

---

### Task 2: schema 迁移——session_kind 枚举增加 'master'

**Files:**
- Modify: `app/db/ai_schema.py:106`（建表 DDL）与 `app/db/ai_schema.py:630-633`（迁移字典）
- Test: `tests/test_ai_schema_master_kind.py`（新建）

**Interfaces:**
- Produces: MySQL 列 `ai_profile_session.session_kind` 取值 `build|update|master`；迁移在 `database_setup_marriage.py` 调用链自动生效。
- Consumes: 现有 `ensure` 迁移机制（ai_schema.py 中以 630 行附近的 DDL 字符串为比对基准做 ALTER）。

- [ ] **Step 1: 写失败测试**

```python
"""session_kind 枚举必须包含 master（墨相师对话建构会话）。"""
from app.db.ai_schema import AI_PROFILE_SESSION_KIND_DDL


def test_session_kind_enum_contains_master() -> None:
    assert "'master'" in AI_PROFILE_SESSION_KIND_DDL
    assert AI_PROFILE_SESSION_KIND_DDL.index("'build'") < AI_PROFILE_SESSION_KIND_DDL.index("'master'")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_ai_schema_master_kind.py -q`
Expected: FAIL（ImportError 或断言失败）

- [ ] **Step 3: 实现——抽常量并加入 'master'**

`app/db/ai_schema.py` 中把 106 行与 633 行两处重复的枚举串抽为模块常量并扩展（两处 DDL 必须同源，防止漂移）：

```python
# session_kind：build=建构问答 / update=对话式追加 / master=墨相师对话建构。
# 枚举追加只增不改，存量行零影响；默认 'build' 保证旧会话行为不变。
AI_PROFILE_SESSION_KIND_DDL = (
    "`session_kind` enum('build','update','master') NOT NULL DEFAULT 'build' "
    "COMMENT 'build=建构问答/update=对话式追加/master=墨相师对话建构'"
)
```

106 行建表 DDL 与 630 行附近的迁移字典值统一引用 `AI_PROFILE_SESSION_KIND_DDL`（若迁移机制对 ALTER 语句有独立的枚举串，同样替换为该常量）。

- [ ] **Step 4: 跑测试确认通过**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_ai_schema_master_kind.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/db/ai_schema.py tests/test_ai_schema_master_kind.py
git commit -m "feat(schema): session_kind 枚举增加 master（墨相师对话建构会话）"
```

---

### Task 3: 进度折算与门槛（纯函数）

**Files:**
- Modify: `app/core/config.py`（新增配置项，位置紧邻 `ai_profile_min_fields`）
- Modify: `app/services/ai/profile.py`（在 `min_confirmed_fields_to_publish`（约 2554 行）附近新增常量与函数）
- Test: `tests/test_master_progress.py`（新建）

**Interfaces:**
- Produces:
  - `MASTER_HARD_FIELD_KEYS: frozenset[str]` = `{"city_code","age","marriage_status"}`
  - `master_progress(confirmed_structured: frozenset[str], confirmed_entries: int) -> MasterProgress`（dataclass：`percent: float, hard_done: int, hard_total: int, entry_score: float, gate_met: bool`）
- Consumes: `settings.ai_master_build_gate`（本任务新增，默认 0.60）。

- [ ] **Step 1: 写失败测试**

```python
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
    assert result.percent == 40.0  # (2 + 2*0.5)/10*100
    assert result.gate_met is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_progress.py -q`
Expected: FAIL（ImportError: MASTER_HARD_FIELD_KEYS）

- [ ] **Step 3: 实现**

`app/core/config.py` 紧邻 `ai_profile_min_fields` 加：

```python
    # 墨相师对话建构门槛（设计 D2/D6）：硬字段全齐 + 折算总分百分比阈值。
    ai_master_build_gate: float = 0.60
```

`app/services/ai/profile.py` 在 `min_confirmed_fields_to_publish` 上方加：

```python
# 墨相师对话建构（设计 Task 3）：搜索与匹配强依赖的地基字段，发布前必须全部
# 确认。settings.ai_master_hard_fields 可覆盖（逗号分隔）。
MASTER_HARD_FIELD_KEYS: frozenset[str] = frozenset(
    k.strip() for k in settings.ai_master_hard_fields.split(",") if k.strip()
) if getattr(settings, "ai_master_hard_fields", "") else frozenset(
    {"city_code", "age", "marriage_status"}
)

_MASTER_PROGRESS_DENOMINATOR = 10.0
_MASTER_ENTRY_SCORE = 0.5
_MASTER_ENTRY_SCORE_CAP = 2.0


@dataclass
class MasterProgress:
    percent: float
    hard_done: int
    hard_total: int
    entry_score: float
    gate_met: bool


def master_progress(
    confirmed_structured: frozenset[str], confirmed_entries: int
) -> MasterProgress:
    """折算公式（设计第六节）：structured 1 分/个 + entry 0.5 分/条（上限 2 分），
    分母 10；门槛 = 硬字段全齐 且 percent >= settings.ai_master_build_gate*100。"""
    hard_total = len(MASTER_HARD_FIELD_KEYS)
    hard_done = len(confirmed_structured & MASTER_HARD_FIELD_KEYS)
    entry_score = min(float(max(confirmed_entries, 0)) * _MASTER_ENTRY_SCORE,
                      _MASTER_ENTRY_SCORE_CAP)
    score = len(confirmed_structured) + entry_score
    percent = min(100.0, score / _MASTER_PROGRESS_DENOMINATOR * 100.0)
    gate_met = hard_total > 0 and hard_done == hard_total and (
        percent >= settings.ai_master_build_gate * 100.0
    )
    return MasterProgress(percent=round(percent, 1), hard_done=hard_done,
                          hard_total=hard_total, entry_score=entry_score,
                          gate_met=gate_met)
```

`app/core/config.py` 同时加可覆盖项：

```python
    # 墨相师硬字段白名单（逗号分隔，空串=内置默认 城市/年龄/婚姻状态）。
    ai_master_hard_fields: str = ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_progress.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py app/services/ai/profile.py tests/test_master_progress.py
git commit -m "feat(profile): 墨相师建构进度折算与 60% 门槛（硬字段必达+条目折算）"
```

---

### Task 4: 提示词白名单 + 构建上下文注入

**Files:**
- Modify: `app/services/ai/prompts/moxiang_master.py`（`_SYSTEM_HEADER` 追加白名单段；新增 `build_build_context`）
- Modify: `app/services/voice/master_orchestrator.py`（新增 `set_build_context`，`stream_reply` 组装时附加）
- Test: `tests/test_moxiang_master_prompt.py`（新建）

**Interfaces:**
- Produces:
  - `build_build_context(missing_hard: list[str], confirmed_summary: str, percent: float) -> str`
  - `MoxiangMasterOrchestrator.set_build_context(context: str) -> None`
- Consumes: Task 3 的 `MASTER_HARD_FIELD_KEYS` 与 `fieldLabel` 语义（missing 用 field_key 中文名，由调用方格式化后传入，本模块不做枚举映射）。

- [ ] **Step 1: 写失败测试**

```python
"""墨相师人设提示词：白名单段必须存在；构建上下文注入 system 消息。"""
from app.services.ai.prompts.moxiang_master import (
    _SYSTEM_HEADER,
    build_build_context,
    build_master_prompt,
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_moxiang_master_prompt.py -q`
Expected: FAIL（ImportError: build_build_context）

- [ ] **Step 3: 实现**

`moxiang_master.py` 的 `_SYSTEM_HEADER` 末尾追加一条人格规则（编号顺延为 9）：

```python
    "9. 话题白名单：只围绕用户的自我认知、三观与感情观、生活方式与作息饮食、"
    "物理位置（城市/居住地）、基本情况（年龄/婚姻状态/学历/职业/身高/收入）提问"
    "与展开。用户把话题带到白名单之外时，不接话、不说教，温和地拉回："
    "先用一句话承认对方说的内容，再自然地把话题引回白名单。"
    "若用户明确拒绝回答某项，轻轻放下换角度，不再追问同一项。\n"
```

`build_master_prompt` 增加可选参数与新函数：

```python
def build_build_context(
    missing_hard: list[str], confirmed_summary: str, percent: float
) -> str:
    """构建模式上下文（独立 system 段）：缺什么、已知什么、当前进度。"""
    parts = [
        "当前处于画像建构模式（对话目标：自然收集齐用户画像），进度约 "
        f"{percent:.0f}%。",
    ]
    if missing_hard:
        parts.append("还缺少的基础信息：" + "、".join(missing_hard) +
                     "。在对话自然处把它们问出来，一次只问一个，不要像审表。")
    else:
        parts.append("基础信息已齐，继续丰富生活方式与三观类内容。")
    if confirmed_summary:
        parts.append("已确认的画像内容（呼应即可，不要复述）：\n" + confirmed_summary)
    return "\n".join(parts)


def build_master_prompt(
    user_message: str,
    history: list[dict[str, str]],
    narrative_context: str = "",
    build_context: str = "",
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_HEADER}
    ]
    if narrative_context:
        messages.append({"role": "system", "content": narrative_context})
    if build_context:
        messages.append({"role": "system", "content": build_context})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages
```

`master_orchestrator.py`：dataclass 字段加 `_build_context: str = ""`；新增：

```python
    def set_build_context(self, context: str) -> None:
        """设置建构模式上下文（缺失硬字段/已确认摘要/进度），空串=纯聊模式。"""
        self._build_context = context
```

`stream_reply` 内 `build_master_prompt(user_text, self._history, self._narrative_context)` 改为：

```python
        messages = build_master_prompt(
            user_text, self._history, self._narrative_context,
            build_context=self._build_context,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_moxiang_master_prompt.py -q`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add app/services/ai/prompts/moxiang_master.py app/services/voice/master_orchestrator.py tests/test_moxiang_master_prompt.py
git commit -m "feat(moxiang): 人设话题白名单 + 建构模式上下文注入"
```

---

### Task 5: master 会话服务函数（创建 + 助手回复落库）

**Files:**
- Modify: `app/services/ai/profile.py`（在 `create_update_session`（1129 行）之后新增两个函数）
- Test: `tests/test_master_session.py`（新建，仿 `tests/test_ai_profile_sessions.py` 的 fake db 形态；若该文件用 real_db skipif，同样加 `pytestmark`/skipif，本任务单测用 fake 注入不依赖真库）

**Interfaces:**
- Produces:
  - `async def create_master_session(db, owner_user_id: int, subject: ProfileSubject, consent_version: str) -> ProfileSession` —— 已有活动会话（任意 kind）时**复用**它（墨相师重连语义，与 update 的拒绝语义不同）；kind='master' 且已有 build 活动会话时同样复用原会话（会话内已聊内容不丢）。
  - `async def persist_master_assistant_reply(db, session_id: str, user_id: int, reply_text: str) -> None` —— 落 role='assistant' turn（复用 `_insert_assistant_turn`）。
- Consumes: `_load_consent_grant`、`_find_active_session`、`_reuse_active_session`、`_load_revision_vector`、`_consent_snapshot`、`_insert_assistant_turn`（全部已存在，同模块私有）。

- [ ] **Step 1: 写失败测试**（核心断言：新会话 kind=master；活动会话存在时复用同一 session_id；助手回复落库可读回）

```python
"""master 会话：创建/复用/助手回复落库。fake db 形态与 test_ai_profile_sessions 一致。"""
import pytest

from app.services.ai.profile import (
    create_master_session, persist_master_assistant_reply,
)
# conftest 里已有的 fake session factory / 承载对象按现有测试文件方式导入
from tests.fake_db import FakeAsyncSession  # 与相邻测试文件保持一致


@pytest.mark.asyncio
async def test_create_master_session_inserts_kind_master():
    db = FakeAsyncSession()
    session = await create_master_session(db, 1, "personal", "profile-text-v1")
    assert session.session_kind == "master"
    assert session.status.value == "draft"


@pytest.mark.asyncio
async def test_create_master_session_reuses_active_session():
    db = FakeAsyncSession.with_active_session(kind="build")
    first = await create_master_session(db, 1, "personal", "profile-text-v1")
    second = await create_master_session(db, 1, "personal", "profile-text-v1")
    assert first.session_id == second.session_id


@pytest.mark.asyncio
async def test_assistant_reply_persisted():
    db = FakeAsyncSession.with_active_session(kind="master")
    await persist_master_assistant_reply(db, "sess1", 1, "聊得不错，继续～")
    turns = db.table("ai_profile_turn").rows()
    assert any(t["role"] == "assistant" and t["answer_text"] == "聊得不错，继续～"
               for t in turns)
```

（fake 承载类若与现有测试基建不同名，按 `tests/test_ai_profile_sessions.py` 实际形态改写测试骨架；断言语义不变。）

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_session.py -q`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现**

```python
async def create_master_session(
    db: AsyncSession,
    owner_user_id: int,
    subject: ProfileSubject,
    consent_version: str,
) -> ProfileSession:
    """墨相师对话建构会话（设计 Task 5）。

    与 build/update 共用唯一活动槽位：已有活动会话时**复用**（WS 重连/重复
    session_start 不丢上下文），这与 update 的"拒绝新建"语义不同。无活动会话
    时新建 kind='master'、status=draft 行（复用 build 的 INSERT，仅 kind 不同）。
    校验授权；不 commit。
    """
    subject_value = subject.value if isinstance(subject, ProfileSubject) else str(subject)
    if subject_value not in {ProfileSubject.PERSONAL.value, ProfileSubject.IDEAL_PARTNER.value}:
        raise AIInputError("subject must be personal or ideal_partner")
    consent = await _load_consent_grant(
        db, owner_user_id, PROFILE_CONSENT_SCOPE, consent_version
    )
    if consent is None:
        raise AIConsentRequired()
    revision = await _load_revision_vector(db, owner_user_id)
    consent_snapshot = _consent_snapshot(consent)
    existing = await _find_active_session(db, owner_user_id, subject_value)
    if existing is not None:
        return await _reuse_active_session(
            db, existing, revision=revision, consent_snapshot=consent_snapshot
        )
    session_id = uuid.uuid4().hex
    expires_at = _now_utc() + timedelta(days=settings.ai_profile_session_expire_days)
    policy_revision = consent_snapshot.get("policy_revision") or PROFILE_POLICY_REVISION
    await db.execute(
        text(
            "INSERT INTO ai_profile_session "
            "(session_id, user_id, subject, input_mode, session_kind, status, active_status, "
            " consent_version, policy_revision, current_question_id, "
            " profile_revision, preference_revision, expires_at, created_at, updated_at) "
            "VALUES (:session_id, :user_id, :subject, 'text', 'master', 'draft', 1, "
            " :consent_version, :policy_revision, NULL, "
            " :profile_revision, :preference_revision, :expires_at, "
            " UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "session_id": session_id,
            "user_id": owner_user_id,
            "subject": subject_value,
            "consent_version": consent_version,
            "policy_revision": policy_revision,
            "profile_revision": revision.profile,
            "preference_revision": revision.preference,
            "expires_at": expires_at,
        },
    )
    row = {
        "session_id": session_id,
        "user_id": owner_user_id,
        "subject": subject_value,
        "input_mode": "text",
        "session_kind": "master",
        "status": ProfileSessionStatus.DRAFT.value,
        "active_status": 1,
        "consent_version": consent_version,
        "policy_revision": policy_revision,
        "current_question_id": None,
        "profile_revision": revision.profile,
        "preference_revision": revision.preference,
        "expires_at": expires_at,
        "ended_at": None,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
    }
    return _session_from_row(
        row, revision=revision, consent_snapshot=consent_snapshot,
        field_keys=frozenset(), confirmed_keys=frozenset(),
    )


async def persist_master_assistant_reply(
    db: AsyncSession, session_id: str, user_id: int, reply_text: str
) -> None:
    """把墨相师回复落为 assistant turn（对话全程可审计）。不 commit。"""
    if not reply_text.strip():
        return
    await _insert_assistant_turn(db, session_id, user_id, reply_text.strip())
```

（`_insert_assistant_turn` 的确切签名以现文件为准，必要时调整传参顺序；语义：写 role='assistant'、source_type='assistant_clarify' 同款行。）

- [ ] **Step 4: 跑测试确认通过**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_session.py -q`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add app/services/ai/profile.py tests/test_master_session.py
git commit -m "feat(profile): master 会话创建/复用与助手回复落库"
```

---

### Task 6: profile_extract handler 的 master 分支（抽取+白名单过滤+草稿写入）

**Files:**
- Modify: `app/services/ai/profile.py`（抽取 handler 约 1875 行的分支；新增 `_handle_master_extract`、`_write_master_draft_fields`；把 `_handle_update_extract` 内 2186-2229 行的 patch 校验循环抽为 `_validate_entry_patches` 供两分支共用）
- Modify: `app/services/ai/prompts/structured_extract.py` 或 provider 侧 prompt 组装处（`session_kind="master"` 契约：允许返回 0 条 patch，不许返回澄清问题——澄清由墨相师对话承担；若该分支按 `session_kind == "update"` 字符串组装 prompt，需增 master 同款契约段）
- Test: `tests/test_master_extract_handler.py`（新建，fake provider 注入形态仿现有 `tests/test_ai_profile_publish.py`）

**Interfaces:**
- Consumes: `StructuredExtractRequest(session_kind="master", turn_texts=<对话>, entry_digest=...)`；`gateway.structured_extract`；`master_progress`（Task 3）。
- Produces: master 分支把 entry patch 与 structured 字段写入会话当前草稿（suggested，待确认）；空 patch 合法完成任务（result `profile-master:no-op`）；**绝不写 assistant 澄清 turn**。

- [ ] **Step 1: 写失败测试**

```python
"""master 抽取分支：entry+structured 落草稿 / 空 patch 合法 / 白名单外不落库。"""
import pytest

# fake provider/gateway 形态仿 test_ai_profile_publish.py 的注入方式
from tests.conftest import fake_gateway_with_extract_outcome  # 按现有基建实际名调整


@pytest.mark.asyncio
async def test_master_patches_land_as_suggested_rows(...):
    """provider 返回 1 entry + 1 structured(city_code) → 草稿出现两行 suggested，
    会话状态 awaiting_confirmation，任务 result 含 draft_id。"""


@pytest.mark.asyncio
async def test_master_empty_patches_is_noop_success(...):
    """provider 返回 0 patch 0 question → 任务 succeeded、无草稿行、
    会话保持 draft、不新增 assistant turn。"""


@pytest.mark.asyncio
async def test_master_out_of_whitelist_patch_rejected(...):
    """provider 返回 category 不在白名单的 patch → 终态失败 AI_INPUT_INVALID
    （与 update 同纪律），会话不写行。"""
```

（三个测试的完整骨架复制 `tests/test_ai_profile_publish.py` 中最接近的 extract→draft 用例，替换 session_kind/断言；本步只写此三者。）

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_extract_handler.py -q`
Expected: FAIL（master 会话落入 build 分支或未知 kind 处理）

- [ ] **Step 3: 实现**

(a) 抽共用校验 helper（从 `_handle_update_extract` 2186-2229 行原样搬移，两处调用）：

```python
def _validate_entry_patches(
    patches: tuple[Any, ...],
    expected_subject: ProfileSubject,
    expected_policy_revision: str,
    existing_entry_keys: set[str],
) -> None:
    """entry patch 边界复核（update/master 共用纪律）：伪造证据=终态失败。"""
    for patch in patches:
        # ……原 _handle_update_extract 中 2187-2225 行的逐条校验原样搬入……
```

(b) handler 分支（在 `if session.session_kind == "update":` 之前插入）：

```python
    if session.session_kind == "master":
        return await _handle_master_extract(db, session, turn, context, task, worker_id)
```

(c) 新函数：

```python
async def _handle_master_extract(
    db: AsyncSession, session: ProfileSession, turn: ProfileTurn,
    context: AITaskContext, task: AiTaskRecord, worker_id: str,
) -> tuple[str, RevisionVector] | None:
    """master 会话抽取（设计 Task 6）：对话整段抽 entry + 逐缺失硬字段抽
    structured；白名单外=空 patch；空结果合法完成；不写澄清 turn。不 commit。"""
    expected_subject = session.subject
    expected_policy_revision = session.policy_revision or PROFILE_POLICY_REVISION
    dialogue = await _load_session_dialogue(db, session.session_id)
    entry_rows = await _load_published_entry_rows(
        db, int(session.owner_user_id), session.subject.value
    )
    gateway = AIGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    request = StructuredExtractRequest(
        subject=session.subject.value,
        turn_texts=tuple(dialogue),
        consent_version=session.consent_version,
        policy_revision=expected_policy_revision,
        session_kind="master",
        entry_digest=_entry_digest_with_keys(entry_rows),
    )
    outcome = await gateway.structured_extract(context, request)
    if outcome.result is None:
        await fail_task(db, task.task_id, worker_id,
                        error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
                        retryable=outcome.retryable)
        if not outcome.retryable:
            await _fail_extract_session(db, session.session_id)
        return None
    patches = tuple(outcome.result.patches)
    if outcome.result.clarifying_question:
        await fail_task(db, task.task_id, worker_id,
                        error_code="AI_INPUT_INVALID", retryable=False)
        await _fail_extract_session(db, session.session_id)
        return None
    existing_entry_keys = {str(r.get("field_key")) for r in entry_rows}
    try:
        _validate_entry_patches(patches, expected_subject,
                                expected_policy_revision, existing_entry_keys)
    except (AttributeError, TypeError, ValueError):
        await fail_task(db, task.task_id, worker_id,
                        error_code="AI_INPUT_INVALID", retryable=False)
        await _fail_extract_session(db, session.session_id)
        return None

    # 硬字段：对每个未确认的缺失硬字段做一次定向抽取（复用 build 校验纪律）。
    confirmed_keys = await _load_confirmed_field_keys(db, session)
    missing = sorted(MASTER_HARD_FIELD_KEYS - confirmed_keys)
    structured_fields: list[Any] = []
    for field_key in missing:
        hard_request = StructuredExtractRequest(
            subject=session.subject.value,
            turn_texts=tuple(dialogue),
            consent_version=session.consent_version,
            policy_revision=expected_policy_revision,
            target_field_key=field_key,
        )
        hard_outcome = await gateway.structured_extract(context, hard_request)
        if hard_outcome.result is None:
            continue  # 硬字段抽取失败不阻塞 entry 落库；下轮重试
        for field in tuple(hard_outcome.result.fields):
            if field.field_key != field_key:
                continue
            field.value = normalize_profile_extracted_value(
                field.subject, field.field_key, field.value)
            structured_fields.append(field)

    if not patches and not structured_fields:
        return "profile-master:no-op", session.revision_vector

    draft_id = await _write_master_draft_fields(
        db, session, turn, patches, structured_fields)
    if session.status is ProfileSessionStatus.EXTRACTING:
        assert_session_transition(session.status, ProfileSessionStatus.AWAITING_CONFIRMATION)
        await _update_session_status(db, session.session_id,
                                     ProfileSessionStatus.AWAITING_CONFIRMATION)
    return f"profile-draft:{draft_id}", session.revision_vector
```

(d) `_write_master_draft_fields`：无活动草稿时用 `_write_draft` 的建壳 SQL 建（status='draft'），然后照 `_write_update_draft` 的行写入方式插入 entry suggested 行、照 `_DRAFT_FIELD_COLUMNS` 插入 structured suggested 行（`field_kind='structured'`，`value_json`/`display_value` 由 `normalize_profile_extracted_value` 结果序列化）。全部字段 `confirmation_status='suggested'`。

- [ ] **Step 4: 跑测试确认通过**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_master_extract_handler.py tests/test_ai_profile_publish.py -q`
Expected: 新 3 用例 passed + publish 回归不新增红

- [ ] **Step 5: 提交**

```bash
git add app/services/ai/profile.py app/services/ai/prompts/ tests/test_master_extract_handler.py
git commit -m "feat(profile): profile_extract 的 master 分支——对话抽取/白名单过滤/草稿写入"
```

---

### Task 7: WS 路由接入（session_start 建构模式 + 轮次落库入队 + 进度/卡片推送）

**Files:**
- Modify: `app/api/routes/voice_moxiang.py`（`session_start` 分支 264-279 行、`text_message` 分支 281-300 行、`audio_end` 381-392 行；新增 `_wait_extract_and_push`、`_push_progress_snapshot`）
- Test: `tests/test_moxiang_ws_build_mode.py`（新建，仿现有 `tests/test_voice_ws.py` 的 fake WS/fake provider 形态）

**Interfaces:**
- Consumes: Task 5 `create_master_session`/`persist_master_assistant_reply`、`submit_profile_turn`（1477 行起，公开函数）、Task 6 handler、Task 3 `master_progress`、Task 4 `set_build_context`。
- Produces WS 协议（后端→前端新增三种消息）:

```json
{"type": "progress", "percent": 40.0, "hard_done": 1, "hard_total": 3, "entry_score": 1.5, "gate_met": false}
{"type": "confirm_card", "card_id": "c-...", "draft_id": "d-...", "expected_revision": 3,
 "items": [{"field_key": "entry_...", "kind": "entry", "category": "价值观", "content": "..."}]}
{"type": "publish_ready", "summary": "基础信息已齐，可以去成稿了"}
```

前端→后端：`session_start` 增可选 `"mode": "profile_build"` 与 `"subject": "personal"`；`text_message` 增可选 `"clientTurnId"`。

- [ ] **Step 1: 写失败测试**

```python
"""WS 建构模式：会话创建/进度推送/卡片推送/纯聊兼容。fake WS 仿 test_voice_ws.py。"""
import pytest


@pytest.mark.asyncio
async def test_session_start_with_build_mode_creates_master_session(...):
    """session_start(mode=profile_build) → session_ready 后收到 progress
    (percent 0, gate_met false)；DB 出现 kind='master' 会话。"""


@pytest.mark.asyncio
async def test_text_message_persists_turn_and_enqueues(...):
    """text_message → ai_reply 正常；DB 出现 user+assistant turn；ai_task
    出现 profile_extract 行。fake gateway 让抽取任务同步完成后，收到
    confirm_card（fake provider 返回 1 patch）。"""


@pytest.mark.asyncio
async def test_session_start_without_mode_keeps_pure_chat(...):
    """不带 mode 的 session_start：无会话创建、无 progress 推送（回归兼容）。"""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_moxiang_ws_build_mode.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`voice_moxiang.py` 顶部 import 增：

```python
from app.db.session import session_factory as _db_session_factory
from app.services.ai.profile import (
    create_master_session,
    master_progress,
    persist_master_assistant_reply,
    submit_profile_turn,
    ProfileSubject,
)
```

连接级状态（`asr_client` 声明处）加：

```python
    build_session_id: str = ""
    build_subject: str = "personal"
    poll_task: asyncio.Task[None] | None = None
```

`session_start` 分支改为（保留原纯聊路径）：

```python
            if msg_type == "session_start":
                narrative_ctx = await _load_narrative_context(user_id)
                orchestrator.set_narrative_context(narrative_ctx)
                build_mode = str(message.get("mode", "")) == "profile_build"
                if build_mode and _db_session_factory is not None:
                    try:
                        async with _db_session_factory() as db:
                            session = await create_master_session(
                                db, user_id,
                                ProfileSubject(build_subject),
                                str(message.get("consentVersion", "profile-text-v1")),
                            )
                            await db.commit()
                            build_session_id = session.session_id
                    except Exception as exc:
                        code = "AI_CONSENT_REQUIRED" if type(exc).__name__ == "AIConsentRequired" else "AI_TEMPORARILY_UNAVAILABLE"
                        await _send_error(ws, code, "画像建构通道暂不可用，可稍后重试")
                        build_session_id = ""
                await _send_json(ws, {"type": "session_ready"})
                if build_session_id:
                    await _push_progress_snapshot(ws, user_id, build_session_id)
                await _send_json(ws, {"type": "ai_reply", "text": OPENING_MESSAGE})
```

新增两个推送函数（模块级）：

```python
async def _push_progress_snapshot(ws: WebSocket, user_id: int, session_id: str) -> None:
    """读会话已确认字段/条目数并推 progress；读不到就静默跳过。"""
    from app.services.ai.profile import load_owned_session  # 现有公开函数

    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            session = await load_owned_session(db, session_id, user_id)
            confirmed_structured, confirmed_entries = await _count_confirmed(db, session)
            snap = master_progress(confirmed_structured, confirmed_entries)
            await _send_json(ws, {
                "type": "progress", "percent": snap.percent,
                "hard_done": snap.hard_done, "hard_total": snap.hard_total,
                "entry_score": snap.entry_score, "gate_met": snap.gate_met,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("moxiang_progress_push_failed err=%s", type(exc).__name__)


async def _wait_extract_and_push(
    ws: WebSocket, user_id: int, session_id: str, task_id: str,
) -> None:
    """轮询 profile_extract 任务终态（≤30s），终态后推 progress/confirm_card/
    publish_ready。断线由调用方 cancel，不产生幽灵任务。"""
    import asyncio as _asyncio
    from sqlalchemy import text as _text

    for _ in range(60):
        await _asyncio.sleep(0.5)
        if _db_session_factory is None:
            return
        async with _db_session_factory() as db:
            row = (await db.execute(
                _text("SELECT status FROM ai_task WHERE task_id = :tid"),
                {"tid": task_id},
            )).mappings().first()
        if row is None:
            return
        if str(row["status"]) in {"succeeded", "failed", "dead"}:
            break
    else:
        return  # 30s 未终态：本轮不推卡片，下轮对话或重连时补
    await _push_progress_snapshot(ws, user_id, session_id)
    await _push_confirm_card(ws, user_id, session_id)
```

`_push_confirm_card`：读会话活动草稿的 suggested 行（复用 `_load_active_draft_id_for_session` 与 `_load_draft_field_rows`），非空则组装 `confirm_card` 推送（`card_id` 用 `uuid.uuid4().hex` 前缀 `c-`；`gate_met` 为真时附推 `publish_ready`）。

`text_message` 分支在 `_push_streamed_reply` 前后接入建构链路：

```python
            elif msg_type == "text_message":
                # ……现有空值/长度校验不变……
                task_id = ""
                if build_session_id and _db_session_factory is not None:
                    async with _db_session_factory() as db:
                        submission = await submit_profile_turn(
                            db, build_session_id, user_id,
                            str(message.get("clientTurnId") or uuid.uuid4().hex),
                            text_content,
                        )
                        await db.commit()
                        task_id = submission.task.task_id
                await _push_streamed_reply(ws, orchestrator, text_content, request_id=request_id)
                if build_session_id:
                    if orchestrator._last_reply_text:  # noqa: SLF001
                        async with _db_session_factory() as db:
                            await persist_master_assistant_reply(
                                db, build_session_id, user_id, orchestrator._last_reply_text)
                            await db.commit()
                    if task_id:
                        if poll_task is not None:
                            poll_task.cancel()
                        poll_task = asyncio.create_task(
                            _wait_extract_and_push(ws, user_id, build_session_id, task_id))
```

`audio_end` 的 `final_transcript` 分支同样套用上述链路（clientTurnId 用 `uuid.uuid4().hex`，source 语义由 submit 内部落库承担）。

`orchestrator.set_build_context`：`session_start` 建构模式时调用 `_build_context_snapshot()`（查缺失硬字段+已确认摘要，格式化后传给 Task 4 的方法）；纯聊传 `""`。

`finally` 块补 `if poll_task is not None: poll_task.cancel()`。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest tests/test_moxiang_ws_build_mode.py tests/test_voice_ws.py tests/test_moxiang_ws.py -q`（第三个文件若不存在则省略）
Expected: 新用例 passed，既有 WS 测试不新增红

- [ ] **Step 5: 提交**

```bash
git add app/api/routes/voice_moxiang.py tests/test_moxiang_ws_build_mode.py
git commit -m "feat(moxiang): WS 建构模式——会话绑定/轮次落库/进度与确认卡片推送"
```

---

### Task 8: 前端 MasterWS 协议扩展（FrontendWT）

**Files:**
- Modify: `api/voice-master-ws.uts`（`MasterWSCallbacks` 增回调；服务端消息分发增 3 类型；`connect` 的 session_start 消息带 `mode`/`subject`/`consentVersion`；`sendTextMessage` 带随机 `clientTurnId`）
- Test: `tests/test-moxiang-master-page.js`（新建，node 断言，形态仿 `tests/test-ai-profile-page.js`）

**Interfaces:**
- Produces:

```uts
export interface MasterProgress { percent: number, hardDone: number, hardTotal: number, entryScore: number, gateMet: boolean }
export interface ConfirmCardItem { fieldKey: string, kind: string, category: string, content: string }
export interface ConfirmCardPayload { cardId: string, draftId: string, expectedRevision: number, items: ConfirmCardItem[] }
// MasterWSCallbacks 增：
onProgress?: (p: MasterProgress) => void
onConfirmCard?: (card: ConfirmCardPayload) => void
onPublishReady?: (summary: string) => void
// MasterWS 增方法：
startBuildMode(subject: string, consentVersion: string): void  // 连接后发 session_start{mode}
```

- [ ] **Step 1: 写失败测试**

```js
const { read } = require('./helpers/read')  // 若无 helper，直接 fs.readFileSync 相对仓库根
const ws = require('fs').readFileSync('api/voice-master-ws.uts', 'utf8')
const assert = require('assert')

assert.match(ws, /onProgress/, 'MasterWS must expose onProgress callback')
assert.match(ws, /onConfirmCard/, 'MasterWS must expose onConfirmCard callback')
assert.match(ws, /onPublishReady/, 'MasterWS must expose onPublishReady callback')
assert.match(ws, /profile_build/, 'session_start must carry build mode')
assert.match(ws, /clientTurnId/, 'text messages must carry clientTurnId')
```

- [ ] **Step 2: 跑测试确认失败** → Run: `node tests/test-moxiang-master-page.js`；Expected: FAIL

- [ ] **Step 3: 实现**（在 `voice-master-ws.uts` 的消息分发 if/else 链中，仿 `ai_reply` 分支增加）:

```uts
		} else if (type == 'progress') {
			this.callbacks.onProgress?.({
				percent: Number(msg.percent ?? 0),
				hardDone: Number(msg.hard_done ?? 0),
				hardTotal: Number(msg.hard_total ?? 0),
				entryScore: Number(msg.entry_score ?? 0),
				gateMet: msg.gate_met == true
			} as MasterProgress)
		} else if (type == 'confirm_card') {
			const items = (msg.items ?? []) as any[]
			this.callbacks.onConfirmCard?.({
				cardId: msg.card_id, draftId: msg.draft_id,
				expectedRevision: Number(msg.expected_revision ?? 0),
				items: items.map((it: any): ConfirmCardItem => ({
					fieldKey: it.field_key, kind: it.kind,
					category: it.category, content: it.content
				}))
			} as ConfirmCardPayload)
		} else if (type == 'publish_ready') {
			this.callbacks.onPublishReady?.(msg.summary ?? '')
		}
```

`session_start` 上行消息与 `sendTextMessage` 补字段（`clientTurnId` 用 `Date.now()+'-'+Math.floor(Math.random()*1e6)`）。

- [ ] **Step 4: 跑测试确认通过** → `node tests/test-moxiang-master-page.js`；Expected: PASS

- [ ] **Step 5: 提交（FrontendWT）**

```bash
git add api/voice-master-ws.uts tests/test-moxiang-master-page.js
git commit -m "feat(moxiang): MasterWS 协议扩展——进度/确认卡片/发布就绪回调"
```

---

### Task 9: 墨相师页建构 UI + 退出修复（FrontendWT）

**Files:**
- Modify: `pagesSub/profileExtra/my-portrait-master.uvue`（模板增进度条/卡片区块约 60 行；script 增状态与回调约 120 行；navBack 修复 427-436 行；样式追加）
- Test: `tests/test-moxiang-master-page.js`（Task 8 已建，追加断言）

**Interfaces:**
- Consumes: Task 8 回调；`api/ai-profile.uts` 的 `getProfileDraft`/`patchProfileDraft`/`fieldAction`（已存在导出）。

- [ ] **Step 1: 追加失败断言**

```js
const page = require('fs').readFileSync('pagesSub/profileExtra/my-portrait-master.uvue', 'utf8')
assert.match(page, /mm-progress/, 'page must render build progress bar')
assert.match(page, /confirm-card|mm-card/, 'page must render confirm card')
assert.match(page, /getCurrentPages/, 'navBack must guard empty page stack')
assert.match(page, /patchProfileDraft/, 'card actions must call REST confirm')
```

- [ ] **Step 2: 跑测试确认失败** → `node tests/test-moxiang-master-page.js`；Expected: FAIL

- [ ] **Step 3: 实现**

(a) 模板（`mm-nav` 之后插入）：

```html
		<!-- 建构进度（仅建构模式显示） -->
		<view v-if="buildMode" class="mm-progress">
			<view class="mm-progress-track">
				<view class="mm-progress-fill" :style="'width:' + progress.percent + '%'"></view>
			</view>
			<text class="mm-progress-text">{{ progress.hardDone }}/{{ progress.hardTotal }} 基础信息 · {{ progress.percent }}%</text>
		</view>
		<!-- 发布就绪 -->
		<view v-if="gateMet" class="mm-ready" @tap="goPortrait">
			<text class="mm-ready-text">基础信息已齐，去成稿 ›</text>
		</view>
```

消息流循环内追加卡片渲染（`v-if="msg.role == 'card'"`）：折叠头「我记下这些了（{{ msg.card.items.length }}）」+ 展开列表（单条：category+content +「改」/「删」按钮 +「都记对了」整体确认按钮）。

(b) script：状态 `const buildMode = ref(false)`、`const progress = ref({percent:0,hardDone:0,hardTotal:0,entryScore:0,gateMet:false})`、`const draftState = ref(null)`；`connectWS()` 组装 MasterWS 时接三个新回调（`onProgress` 更新 progress；`onConfirmCard` 把卡片 addMessage 进流并保存 draftState；`onPublishReady` 置 gateMet）；卡片动作：

```uts
import { getProfileDraft, patchProfileDraft, fieldAction } from '@/api/ai-profile.uts'

async function confirmAll(card: ConfirmCardPayload) {
	const actions = card.items.map((it: ConfirmCardItem): any =>
		fieldAction(it.fieldKey, 'confirm', null, draftState.value?.expectedRevision ?? 0))
	const res = await patchProfileDraft(card.draftId, actions, draftState.value?.expectedRevision ?? 0)
	// 成功后从消息流移除该卡片；失败 toast 原样提示
}

async function deleteItem(card: ConfirmCardPayload, item: ConfirmCardItem) {
	await patchProfileDraft(card.draftId, [fieldAction(item.fieldKey, 'delete', null, draftState.value?.expectedRevision ?? 0)], draftState.value?.expectedRevision ?? 0)
}
```

（`fieldAction` 的 action 枚举为 `confirm|replace|reject|delete`，与 `api/ai-profile.uts:17` 一致；`expectedRevision` 从 onConfirmCard payload 与 getProfileDraft 刷新。）

(c) `goPortrait()`：`uni.navigateTo({ url: '/pagesSub/profileExtra/my-portrait' })`。

(d) 退出修复（`navBack`，427 行）：

```uts
function navBack() {
	// ……现有 stopRecording/ws 清理不变……
	const pages = getCurrentPages()
	if (pages.length > 1) {
		uni.navigateBack({ delta: 1 })
	} else {
		uni.reLaunch({ url: '/pages/index/index' })  // 页面栈空（直开/分享进入）兜底
	}
}
```

模板 6 行 `‹` 改 `✕`，`.mm-nav-back` 加 `width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;`（热区 ≥44px）。

(e) `startBuildMode('personal','profile-text-v1')`：在 connectWS 成功后调用（`onSessionReady` 前发送），`buildMode.value = true`。

- [ ] **Step 4: 跑测试确认通过** → `node tests/test-moxiang-master-page.js`；Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add pagesSub/profileExtra/my-portrait-master.uvue tests/test-moxiang-master-page.js
git commit -m "feat(moxiang): 墨相师页建构进度/确认卡片/发布引导 + 退出兜底修复"
```

---

### Task 10: 入口统一（FrontendWT）

**Files:**
- Modify: `pages/profile/profile.uvue`（我的页：画像建构入口直指墨相师——定位既有跳转 `my-portrait` 的入口项，仿其结构新增「墨相师·开始建构」项，url `/pagesSub/profileExtra/my-portrait-master`）
- Modify: `pagesSub/profileExtra/my-portrait.uvue:112-118,944`（页内「墨相师入口」卡片删除或改为「和墨相师继续聊」跳转；删除时同步删 2201 行起的入口样式段）
- Test: `tests/test-moxiang-master-page.js` 追加

**Interfaces:** Consumes Task 9 的页面路径。Produces: 我的页单入口直达墨相师。

- [ ] **Step 1: 追加失败断言**

```js
const my = require('fs').readFileSync('pages/profile/profile.uvue', 'utf8')
assert.match(my, /my-portrait-master/, 'my page must link to moxiang master page')
const portrait = require('fs').readFileSync('pagesSub/profileExtra/my-portrait.uvue', 'utf8')
assert.doesNotMatch(portrait, /墨相师页面打开失败/, 'old master-entry toast must be removed from portrait page')
```

- [ ] **Step 2: 确认失败** → `node tests/test-moxiang-master-page.js`；Expected: FAIL

- [ ] **Step 3: 实现**（我的页入口项复制相邻画像入口项结构，仅换文案「墨相师 · 聊出你的画像」与 url；画像页 112-118 卡片文案改为「和墨相师继续聊」，跳转目标不变即 944 行 url 已是 master 页——若删除则一并清理 944-945 与样式段）

- [ ] **Step 4: 确认通过** → `node tests/test-moxiang-master-page.js`

- [ ] **Step 5: 提交**

```bash
git add pages/profile/profile.uvue pagesSub/profileExtra/my-portrait.uvue tests/test-moxiang-master-page.js
git commit -m "feat(moxiang): 建构入口统一到墨相师，画像页转档案角色"
```

---

### Task 11: 端到端验证与全量回归

**Files:**
- Test: `tests/integration/ai/test_master_e2e_real_db.py`（新建，`skipif` 无真库——与本目录既有 real_db 文件同款守卫）

**Interfaces:** Consumes 全部前序任务；验证设计第九节验收链路。

- [ ] **Step 1: 写 e2e 测试（真库 skipif，环境具备时运行）**

链路：`create_master_session` → `submit_profile_turn`（"我在杭州，今年28，未婚，做设计的"）→ fake provider 返回 entry+structured → `run extract handler` → 断言草稿 2 行 suggested → `confirm_profile_draft` 确认 → `master_progress` 达标 → publish 任务入队。断言全程 turn 表含 user+assistant 行。

- [ ] **Step 2: 后端全量回归对比基线**

Run（BackendWT）: `"/d/Users/ASUS/Desktop/宣誓爱/xuanshiai-backend/.venv/Scripts/python.exe" -m pytest -q 2>&1 | tail -3`
Expected: passed ≥ 759+新增；failed ⊆ {5 个既有基线红}（governance 已修复转绿）；errors 仍 76（本机无 MySQL/Redis，环境性）

- [ ] **Step 3: 前端全量测试**

Run（FrontendWT）: `for f in tests/test-*.js; do node "$f" >/dev/null 2>&1 || echo "FAIL $f"; done`
Expected: 失败集 ⊆ 既有 13 红（不新增）

- [ ] **Step 4: 小程序编译验证**

Run: `"D:\Users\ASUS\tools\HBuilderX\cli.exe" launch --compile true`（或按 HBuilderX cli 既有调用方式，对 FrontendWT 主目录发编译）
Expected: mp-weixin 编译成功无报错

- [ ] **Step 5: 收尾提交**

```bash
git add tests/integration/ai/test_master_e2e_real_db.py
git commit -m "test(moxiang): 对话建构端到端用例（真库 skipif）"
```

---

## Self-Review 记录

- 规格覆盖：PRODUCT 变更（T1）、枚举迁移（T2）、折算门槛（T3）、白名单与上下文（T4）、会话与助手落库（T5）、抽取分支（T6）、WS 协议三推送（T7/T8）、卡片与发布 UI（T9）、退出修复（T9）、单入口（T10）、降级提示（T7 的 error 分支 `AI_CONSENT_REQUIRED/AI_TEMPORARILY_UNAVAILABLE` + 前端跳题库兜底沿用画像页既有入口）、e2e（T11）——设计 1-9 节均有对应任务。
- 类型一致性：`master_progress`/`MasterProgress`/`ConfirmCardPayload`/`build_build_context` 前后任务签名一致；`fieldAction('confirm'|'delete')` 与 `api/ai-profile.uts:17` 枚举一致。
- 已知留白（执行者按现场基建对齐，断言语义不变）：T5 fake 承载类名、T6 fake gateway 注入名、T6 prompt 文件的确切路径、T9 卡片折叠组件细节。这些在对应测试文件里有现成形态可抄，不构成设计缺口。
