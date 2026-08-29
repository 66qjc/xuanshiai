# 良配 AI 体验完善 — 阶段0+阶段1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复搜索条件微调静默丢失的 P0 缺陷，并落地 5 项体验快赢（67% 提前建构、叙事确认闭环、搜索百分比进度、匹配度外显灰度开关、.env 治理），对齐《良配AI体验完善方案.md》阶段0+1。

**Architecture:** 全部改动落在 xuanshiai-backend 现有三层结构（routes → services → db/ai_schema）。遵守仓库既有纪律：**服务层函数不 commit，由路由层成功路径显式 `await db.commit()`**；旧库补列走 `ai_schema.py` 的幂等 `ensure_*_columns` 模式；异步进度挂在 `ai_task.stage` 旁新增 `progress_percent` 列；外显灰度用配置驱动的写路径开关，读路径门禁不动。

**Tech Stack:** FastAPI + SQLAlchemy(async) 裸 SQL（无 ORM）、MySQL、pytest + real_db 集成测试（compose.ai-test.yml 专用库）、pydantic-settings。

**规格来源:** `D:\Users\ASUS\Desktop\宣誓爱\良配思维导图总结\良配AI体验完善方案.md`（阶段0: WP-S0；阶段1: WP-P2/P3/S1/C2 + 治理）。S3（猜你喜欢 AI 化）、S2（中途模糊候选）移入阶段2计划。

## Global Constraints

- 服务层不 commit：所有 `app/services/ai/*.py` 业务函数保持"不 commit，由调用方控制事务"（docstring 已声明的契约），提交点只在路由成功路径。
- 生产 fail-closed 门禁不变：provider=mock 或三道审批不全时 503（`app/services/ai/flags.py`），本计划不放松任何门禁。
- 旧库迁移只用幂等补列（`SHOW COLUMNS` → `ALTER TABLE ADD COLUMN`），不改已有列，不删数据。
- 每个任务完成必须全量跑 `python -m pytest tests/ -x -q`（636+ 用例）通过后才能 commit。
- 集成测试专用库由 `tests/integration/ai/conftest.py` 自举（`AI_TEST_DATABASE_URL`，默认 127.0.0.1:3307）；跑集成测试：`python -m pytest tests/integration/ai -q`。无该库环境时，单测（`tests/test_*.py`）必须绿，集成红需注明环境原因。
- 注释风格：中文，说明"为什么/约束"，与现有文件一致；commit message 用 `fix:`/`feat:`/`docs:`/`chore:` 前缀。
- 决策点已锁定（方案 9.2）：D2 条目模型并存扩展（本阶段不涉及）；D3 阈值可配置、默认 7；D6 外显灰度沿用 off→bucket→on 纪律。

**工作目录：** 除 Task 1 外均在 `D:\Users\ASUS\Desktop\宣誓爱\xuanshiai-backend`（独立 git 仓库）。开工前先 `git status` 确认干净、建分支 `feat/liangpei-ai-phase0-1`。

---

### Task 1: PRODUCT.md 产品范围更新（治理前置）

**Files:**
- Modify: `D:\Users\ASUS\Desktop\宣誓爱\PRODUCT.md`（工作区根，445 行；工作区 AGENTS.md 要求产品范围变更先改此文件）

**Interfaces:**
- Produces: 产品定义小节"AI 体验增强（良配对齐）"，为 Task 6（匹配度外显）提供产品依据。

- [ ] **Step 1: 在 PRODUCT.md 末尾追加以下小节**（保持既有标题层级风格，`##` 一级）

```markdown
## AI 体验增强（良配对齐，2026-08）

### AI 合拍度外显
- 双向合适概率（A 适合 B / B 适合 A 两个独立百分数）可在匹配/查看对方时向用户展示，标注"来自良配AI算法"。
- 展示只是资料整理与参考，不承诺关系结果（延续 Product Promise 第 6 条）。
- 灰度节奏 off → bucket → on，未灰度用户不可见。

### 画像叙事确认
- AI 生成的理想型总结先以"待确认"呈现，用户确认后才作为正式画像叙事；修改画像后旧总结标记过期。

### 提前建构
- 画像问答无需答完全部题目，确认字段覆盖约 67% 即可发布画像；进度中给出可提前建构提示。

### 搜索进度体验
- AI 搜索任务对外暴露百分比进度，支撑等待页动画；本阶段不含中途模糊候选。
```

- [ ] **Step 2: 自查** 上面四小节与 PRODUCT.md 既有"Product Promise / Trust, Safety and Privacy"无冲突（特别是"AI 不替人承诺"与"商业化不破坏信任"两条）。

- [ ] **Step 3: Commit（工作区根无 git 仓库则跳过 commit，仅保存）**

```bash
cd "D:\Users\ASUS\Desktop\宣誓爱\xuanshiai-backend"
git status   # 确认工作区根 PRODUCT.md 不属于本仓库（仓库根是 xuanshiai-backend）
```

预期：PRODUCT.md 在仓库外，无需 git 操作；若 `git status` 显示 PRODUCT.md 被跟踪则停下向用户确认。

---

### Task 2: [P0] 修复 PATCH /search-drafts 永不落库（WP-S0 / F1）

**Files:**
- Modify: `app/api/routes/ai_search.py:208-226`（`patch_search_draft_route`）
- Test: `tests/integration/ai/test_ai_search_real_db.py`（追加用例）

**Interfaces:**
- Consumes: `patch_search_draft(db, draft_id, owner_user_id, patches, expected_condition_revision, idempotency_key) -> SearchDraftRead`（`app/services/ai/search.py:1211`，签名不变）
- Produces: 修复后 PATCH 成功路径持久化；无新接口。

- [ ] **Step 1: 写失败的回归测试**（追加到 `tests/integration/ai/test_ai_search_real_db.py` 末尾。路由签名见 `app/api/routes/ai_search.py:185-194`：body 为 `list[SearchConditionPatchRequest]`（`condition_no/action`，`app/schemas/ai_search.py:139-141`），且 `_check_idempotency_key` 强制要求 8-128 位 ASCII 的 Idempotency-Key；门禁 `_require_search_feature` 需 monkeypatch 打开。`_clean`/`OWNER_ID`/`text`/`json` 均已在该测试文件中存在）

```python
@pytest.mark.asyncio
async def test_real_patch_search_draft_persists_condition_edit(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归（审查 I-1）：PATCH 条件微调必须在请求结束后持久化。

    修复前：patch_search_draft 服务层不 commit，路由的 commit 位于
    try/return/except 之后不可达，get_db 只 close 不提交 → 编辑静默丢失。
    """
    from types import SimpleNamespace

    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_search_enabled", True)

    await _clean(real_db_session)
    draft_id = "draft-patch-regression-0001"
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_draft (draft_id, user_id, query_text, status, "
            " condition_revision, condition_schema_version, policy_revision) "
            "VALUES (:draft_id, :user_id, '希望对方性格开朗', 'awaiting_confirmation', "
            " 1, 'search-condition-v1', 'ai-policy-2026-08-07-v1')"
        ),
        {"draft_id": draft_id, "user_id": OWNER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_condition (draft_id, condition_revision, condition_no, "
            " field_key, operator, value_json, condition_kind, user_action) "
            "VALUES (:draft_id, 1, 1, 'personality', 'include', :value_json, "
            " 'soft', 'pending')"
        ),
        {
            "draft_id": draft_id,
            "value_json": json.dumps({"text": "开朗"}, ensure_ascii=False),
        },
    )
    await real_db_session.commit()

    from app.api.routes.ai_search import patch_search_draft_route
    from app.schemas.ai_search import SearchConditionPatchRequest

    await patch_search_draft_route(
        draft_id=draft_id,
        body=[SearchConditionPatchRequest(condition_no=1, action="confirm")],
        current=SimpleNamespace(id=OWNER_ID),
        db=real_db_session,
        idempotency_key="patch-test-0001",
        expected_condition_revision=1,
    )

    row = (
        await real_db_session.execute(
            text(
                "SELECT user_action, condition_revision FROM ai_search_condition "
                "WHERE draft_id = :draft_id AND condition_no = 1"
            ),
            {"draft_id": draft_id},
        )
    ).mappings().first()
    assert row is not None
    assert row["user_action"] == "confirmed", "PATCH 编辑必须在请求后持久化"
    assert int(row["condition_revision"]) == 2, "条件版本号必须已自增"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/integration/ai/test_ai_search_real_db.py::test_real_patch_search_draft_persists_condition_edit -q`
Expected: FAIL，断言 `user_action` 仍为 `pending`（写丢失）。

- [ ] **Step 3: 修复路由提交点**（`app/api/routes/ai_search.py:208-226`，把 commit 移入成功路径，对齐 `ai_compatibility.py:100-124` 的模式）

```python
    try:
        updated = await patch_search_draft(
            db,
            draft_id,
            current.id,
            body,
            expected_condition_revision,
            idempotency_key,
        )
    except SearchInputInvalid as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotConfirmed as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except DraftVersionConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    # 服务层按仓库纪律不 commit；get_db 请求结束只 close 不提交。提交必须在
    # 成功路径显式执行，否则条件微调静默丢失（审查 I-1 回归）。
    await db.commit()
    return updated
```

- [ ] **Step 4: 全仓排查同类模式**

Run: `grep -rn "await db.commit()" app/api/routes/ | grep -v "^Binary"` 逐一核对每个写路由的 commit 是否在成功路径可达；`grep -rLn "commit" app/api/routes/` 列出无 commit 的路由文件，人工确认其是否只读。
Expected: 无第二个"commit 不可达"实例；发现问题按 Step 3 同法修复并追加回归测试。

- [ ] **Step 5: 跑测试与全量**

Run: `python -m pytest tests/integration/ai/test_ai_search_real_db.py -q && python -m pytest tests/ -q`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/api/routes/ai_search.py tests/integration/ai/test_ai_search_real_db.py
git commit -m "fix(search): PATCH search-draft 条件微调在成功路径显式提交，修复静默丢失"
```

---

### Task 3: 提前建构阈值可配置 + 67% 引导（WP-P2 / F2）

**Files:**
- Modify: `app/core/config.py`（AI 门禁区，约 125 行 `ai_compatibility_shadow_enabled` 之后）
- Modify: `app/services/ai/profile.py:1821`（常量 → 配置函数）、`profile.py:2575-2577`（发布门槛判断）
- Modify: `app/schemas/ai_profile.py`（`ProfileProgress` 模型，位于 `ProfileSessionRead` 之前）
- Modify: `app/api/routes/ai_profile.py:129-158`（`_to_session_read`）
- Test: `tests/test_ai_profile_sessions.py`（追加单测）；既有 publish 测试 fixture 校准

**Interfaces:**
- Consumes: `progress_value(confirmed_keys)`（profile.py:677）、`ProfileSession.confirmed_keys`、路由已导入的 `settings`
- Produces: `min_confirmed_fields_to_publish() -> int`（profile.py）；`ProfileProgress.can_early_publish: bool = False`、`ProfileProgress.early_publish_hint: str = ""`（schemas）

- [ ] **Step 1: 写失败的单测**（追加到 `tests/test_ai_profile_sessions.py`；`SimpleNamespace` 鸭子类型构造会话，构造参数以 `_to_session_read` 实际读取的属性为准：`session_id/subject/status/input_mode/current_question/confirmed_keys/draft_id/profile_revision/preference_revision/expires_at/created_at`；枚举用按值构造 `ProfileSubject("personal")`、`ProfileSessionStatus("draft")`）

```python
from types import SimpleNamespace


def test_session_read_can_early_publish_follows_threshold(monkeypatch):
    """进度引导（方案 WP-P2）：确认字段数达到可配置阈值时 can_early_publish=True。

    阈值来自 settings.ai_profile_min_fields（默认 7 = 10 字段的 67%），
    提前建构与发布共用同一阈值，避免两套数字漂移。
    """
    from app.api.routes.ai_profile import _to_session_read
    from app.core.config import settings
    from app.schemas.ai_profile import ProfileSessionStatus, ProfileSubject

    def _session(confirmed):
        return SimpleNamespace(
            session_id="s-1",
            subject=ProfileSubject("personal"),
            status=ProfileSessionStatus("draft"),
            input_mode="text",
            current_question=None,
            confirmed_keys=frozenset(confirmed),
            draft_id=None,
            profile_revision=0,
            preference_revision=0,
            expires_at=None,
            created_at=None,
        )

    monkeypatch.setattr(settings, "ai_profile_min_fields", 7)
    below = _to_session_read(_session(["age", "city", "height", "education", "income", "occupation"]))
    assert below.progress.can_early_publish is False
    assert below.progress.early_publish_hint == ""

    at = _to_session_read(_session(["age", "city", "height", "education", "income", "occupation", "interest"]))
    assert at.progress.can_early_publish is True
    assert "提前" in at.progress.early_publish_hint

    monkeypatch.setattr(settings, "ai_profile_min_fields", 5)
    lowered = _to_session_read(_session(["age", "city", "height", "education", "income"]))
    assert lowered.progress.can_early_publish is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_profile_sessions.py -q -k can_early_publish`
Expected: FAIL（`ai_profile_min_fields` 属性不存在 / `can_early_publish` 字段不存在）。

- [ ] **Step 3: 实现**

3a. `app/core/config.py` AI 门禁区（`ai_compatibility_shadow_enabled` 之后）加：

```python
    # 画像发布门槛：至少确认多少个字段才允许 publish（提前建构阈值）。
    # 良配对齐：默认 7/10 ≈ 67%，"无需完成全部题目，进度 67% 左右可提前
    # 建构画像"。进度提示与发布硬门槛共用此值，避免两套数字漂移。
    ai_profile_min_fields: int = Field(default=7, ge=1, le=20)
```

3b. `app/services/ai/profile.py:1821` 把常量改为函数（`settings` 已在 profile.py:56 导入）：

```python
def min_confirmed_fields_to_publish() -> int:
    """发布门槛：至少确认字段数，来自 settings.ai_profile_min_fields（默认 7）。"""
    return settings.ai_profile_min_fields
```

删除旧常量 `MIN_CONFIRMED_FIELDS_TO_PUBLISH = 5`（含其上方注释一并改写为函数 docstring）。

3c. `profile.py:2575-2577` 调用点改为：

```python
    min_fields = min_confirmed_fields_to_publish()
    if len(fields) < min_fields:
        raise AIInputError(
            f"at least {min_fields} confirmed fields are required"
        )
```

3d. `app/schemas/ai_profile.py` 的 `ProfileProgress` 增加两个有默认值的字段（向后兼容）：

```python
    can_early_publish: bool = False
    early_publish_hint: str = ""
```

3e. `app/api/routes/ai_profile.py` `_to_session_read` 中 `progress=ProfileProgress(...)` 改为：

```python
        progress=ProfileProgress(
            basis="confirmed_field_coverage",
            value=progress_value(session.confirmed_keys),
            can_early_publish=(
                len(session.confirmed_keys) >= min_confirmed_fields_to_publish()
            ),
            early_publish_hint=(
                "已满足提前建构条件，可以直接生成画像啦"
                if len(session.confirmed_keys) >= min_confirmed_fields_to_publish()
                else ""
            ),
        ),
```

并在该文件 import 区补 `from app.services.ai.profile import min_confirmed_fields_to_publish`（合并进既有 profile 导入语句）。

- [ ] **Step 4: 校准既有 publish 测试**

Run: `grep -rn "MIN_CONFIRMED_FIELDS_TO_PUBLISH\|confirmed fields are required" tests/ app/`
Expected: 除新函数外无旧常量残留。若 `tests/test_ai_profile_publish.py` 的 fixture 恰好只确认 5 个字段（为满足旧阈值），把这些 fixture 的 confirmed 字段补到 7 个（字段从 `app/schemas/ai_common.py:16-29` 的 10 字段白名单中取：age/city/marriage/education/height/income/occupation/interest/lifestyle/relationship_goal）。

- [ ] **Step 5: 跑测试与全量**

Run: `python -m pytest tests/test_ai_profile_sessions.py tests/test_ai_profile_publish.py -q && python -m pytest tests/ -q`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py app/services/ai/profile.py app/schemas/ai_profile.py app/api/routes/ai_profile.py tests/test_ai_profile_sessions.py tests/test_ai_profile_publish.py
git commit -m "feat(profile): 发布门槛可配置(默认7≈67%)并下发提前建构引导"
```

---

### Task 4: 叙事层"确认闭环"（WP-P3 / F3）

**Files:**
- Modify: `app/services/ai/profile.py:3602`（INSERT 的 status 值）、`profile.py:3616-3645`（`load_published_narrative` docstring 与过滤不变，仅注释）
- Modify: `app/api/routes/ai_profile.py`（新增 confirm/regenerate 两个路由，插在 narrative GET 路由之后）
- Modify: `app/schemas/ai_profile.py:427-441`（`ProfileNarrativeRead.status` docstring）
- Test: `tests/integration/ai/test_ai_profile_real_db.py`（追加用例；沿用该文件既有 fixture 风格）

**Interfaces:**
- Consumes: `load_published_narrative(db, user_id, subject)`（profile.py:3616）；narrative 任务创建调用（publish 流程内，`profile.py:2580-2615` 区域对 `enqueue_task` 的调用）
- Produces: `confirm_profile_narrative(db, user_id, subject) -> bool`、`request_narrative_regenerate(db, user_id, subject, idempotency_key) -> AiTaskRecord`（profile.py 新增）；状态机 `published 旧值 → generating(任务排队) → pending_confirmation → confirmed`，画像变更使旧总结自然被新 revision 的 pending_confirmation 取代（读取端只取最新一条，无需显式 stale 化）

- [ ] **Step 1: 写失败的集成测试**（追加到 `tests/integration/ai/test_ai_profile_real_db.py`；`ai_profile_summary` 列见 `app/db/ai_schema.py:234-252`，直接 SQL 造数绕开 LLM）

```python
import json

from app.services.ai.profile import (
    confirm_profile_narrative,
    load_published_narrative,
)

NARRATIVE_USER = 9_988_700_001


async def _clean_narrative(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_profile_summary WHERE user_id = :u",
        "DELETE FROM ai_task WHERE owner_user_id = :u",
    ):
        await db.execute(text(statement), {"u": NARRATIVE_USER})
    await db.commit()


def _summary_row(status: str) -> dict:
    data = json.dumps(
        {"persona_title": "温和笃定的人", "insight": "测试", "dimensions": [],
         "ideal_weights": [], "persona_tags": []},
        ensure_ascii=False,
    )
    return {
        "user_id": NARRATIVE_USER,
        "subject": "personal",
        "summary_text": data,
        "status": status,
        "content_hash": "0" * 64,
    }


@pytest.mark.asyncio
async def test_real_narrative_confirmation_loop(real_db_session: AsyncSession) -> None:
    await _clean_narrative(real_db_session)
    row = _summary_row("pending_confirmation")
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_summary (user_id, subject, summary_text, status, "
            " content_hash) VALUES (:user_id, :subject, :summary_text, :status, "
            " :content_hash)"
        ),
        row,
    )
    await real_db_session.commit()

    loaded = await load_published_narrative(real_db_session, NARRATIVE_USER, "personal")
    assert loaded is not None and loaded["status"] == "pending_confirmation"

    changed = await confirm_profile_narrative(
        real_db_session, NARRATIVE_USER, "personal"
    )
    assert changed is True
    confirmed = await load_published_narrative(
        real_db_session, NARRATIVE_USER, "personal"
    )
    assert confirmed["status"] == "confirmed"

    missing = await confirm_profile_narrative(
        real_db_session, NARRATIVE_USER, "ideal_partner"
    )
    assert missing is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/integration/ai/test_ai_profile_real_db.py -q -k narrative_confirmation`
Expected: FAIL `ImportError: cannot import name 'confirm_profile_narrative'`。

- [ ] **Step 3: 实现服务函数**（`app/services/ai/profile.py`，放在 `load_published_narrative` 之后）

```python
async def confirm_profile_narrative(
    db: AsyncSession, user_id: int, subject: str
) -> bool:
    """将用户某 subject 最新一条叙事层标记为 confirmed（方案 WP-P3）。

    只命中最新一行（MySQL 不允许 UPDATE 直接子查询同表，用派生表包装）。
    不 commit，由调用方控制事务。无行时返回 False（路由层 404）。
    """
    result = await db.execute(
        text(
            "UPDATE ai_profile_summary SET status = 'confirmed', "
            " updated_at = UTC_TIMESTAMP() "
            "WHERE id = (SELECT id FROM ("
            "  SELECT id FROM ai_profile_summary "
            "  WHERE user_id = :user_id AND subject = :subject "
            "  ORDER BY created_at DESC LIMIT 1"
            ") AS latest)"
        ),
        {"user_id": user_id, "subject": subject},
    )
    return bool(result.rowcount)
```

同文件 handler 中 `profile.py:3602` 的 INSERT 值 `'published'` 改为 `'pending_confirmation'`，并把该 INSERT 上方注释补一句："叙事层生成后先进入待确认态，用户确认（POST narrative/confirm）后才为 confirmed；读取端透传状态由前端驱动确认 UI（良配对齐 WP-P3）。"

`app/schemas/ai_profile.py:430-432` docstring 的 status 说明改为：`'pending' | 'pending_confirmation' | 'confirmed'`（`'published'` 仅为历史行兼容值）。

- [ ] **Step 4: 新增两个路由**（`app/api/routes/ai_profile.py`，紧随 narrative GET 路由之后；复用该文件已有的 `_error_response`/`_require_profile_feature`/`ProfileSubject` 模式）

```python
@router.post(
    "/profiles/{subject}/narrative/confirm",
    response_model=ProfileNarrativeRead,
    status_code=status.HTTP_200_OK,
    summary="确认画像叙事层（待确认 → 已确认）",
)
async def confirm_profile_narrative_route(
    subject: ProfileSubject,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileNarrativeRead:
    _require_profile_feature()
    changed = await confirm_profile_narrative(db, current.id, subject.value)
    if not changed:
        raise _error_response(
            "NARRATIVE_NOT_FOUND",
            "暂无可确认的画像叙事层",
            status.HTTP_404_NOT_FOUND,
        )
    await db.commit()
    narrative = await load_published_narrative(db, current.id, subject.value)
    data: dict = narrative["data"] if narrative and narrative.get("data") else {}
    return ProfileNarrativeRead(
        subject=subject.value,
        status=str((narrative or {}).get("status") or "confirmed"),
        persona_title=str(data.get("persona_title") or ""),
        persona_tags=list(data.get("persona_tags") or []),
        insight=str(data.get("insight") or ""),
    )
```

`regenerate` 路由：`POST /profiles/{subject}/narrative/regenerate`，202 返回 `{task_id, status}`（形状对齐本文件 publish 路由的 202 响应模型）。服务函数 `request_narrative_regenerate(db, user_id, subject, idempotency_key)`：先用 `SELECT COUNT(*) FROM ai_task WHERE owner_user_id=:u AND task_type='profile_narrative' AND created_at > UTC_TIMESTAMP() - INTERVAL 24 HOUR` 限 5 次/天（超出抛 `AIInputError("今日叙事重新生成次数已达上限")`），然后**复用 publish 流程中创建 profile_narrative 任务的同一调用**（读 `profile.py:2580-2615`，提取该 enqueue 调用为模块内私有助手 `_enqueue_narrative_task(db, user_id, subject, idempotency_key)`，publish 与 regenerate 两处共用，避免两份任务创建逻辑漂移）。

- [ ] **Step 5: 跑测试与全量**

Run: `python -m pytest tests/integration/ai/test_ai_profile_real_db.py -q && python -m pytest tests/ -q`
Expected: 全 PASS（含 `test_ai_release_gates.py`——新路由在门禁关闭时同样 503，无需额外处理）。

- [ ] **Step 6: Commit**

```bash
git add app/services/ai/profile.py app/api/routes/ai_profile.py app/schemas/ai_profile.py tests/integration/ai/test_ai_profile_real_db.py
git commit -m "feat(profile): 叙事层确认闭环 pending_confirmation/confirmed + regenerate 限频"
```

---

### Task 5: 搜索任务百分比进度（WP-S1 / F9）

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_task` DDL 第 50 行 `stage` 之后加列；新增 `ensure_ai_task_columns`，模式照抄 `ensure_ai_projection_columns`（461-500））
- Modify: `app/services/ai/tasks.py:100-106`（`_SELECT_COLUMNS`）、`tasks.py:154`（`AiTaskRecord` 字段）、`tasks.py:172-199`（`from_row`）
- Modify: `app/services/ai/search.py:2098-2106`（`_set_search_task_stage`）及 4 个调用点（2126/2135/2189/2247）
- Modify: `app/api/routes/ai_tasks.py:47-54`（`TaskDetailResponse`）与 GET 响应组装（152-164）
- Test: `tests/integration/ai/test_ai_tasks.py`（追加）+ `tests/test_ai_tasks.py`（from_row 单测）

**Interfaces:**
- Produces: `ai_task.progress_percent TINYINT UNSIGNED NULL`；`_set_search_task_stage(db, task_id, stage, progress=None)`；`AiTaskRecord.progress_percent: int | None`；`TaskDetailResponse.progress_percent: int | None = None`。搜索各阶段进度契约：validating=10、filtering=30、ranking=85、终态 completed=100（方案原 60 档扫描点在现有代码中无独立 stage 调用点，不虚构）。

- [ ] **Step 1: 写失败测试**（`tests/test_ai_tasks.py` 追加单测）

```python
def test_task_record_from_row_maps_progress_percent():
    """进度字段（方案 WP-S1）：from_row 必须透传 progress_percent。"""
    from app.services.ai.tasks import AiTaskRecord

    row = {
        "id": 1, "task_id": "t1", "owner_user_id": 1, "task_type": "search_execute",
        "scene": "ai", "idempotency_key": "k1", "request_digest": None,
        "status": "running", "stage": "filtering", "progress_percent": 30,
        "attempt_count": 0, "max_attempts": 3, "next_run_at": None,
        "lease_owner": None, "lease_until": None, "consent_snapshot_json": None,
        "source_revision_json": None, "payload_summary": None,
        "error_code": None, "error_message": None, "result_ref": None,
        "created_at": None, "updated_at": None, "started_at": None,
        "finished_at": None,
    }
    record = AiTaskRecord.from_row(row)
    assert record.progress_percent == 30
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_tasks.py -q -k progress_percent`
Expected: FAIL（`from_row` 收到意外关键字 `progress_percent` 或字段缺失）。

- [ ] **Step 3: 实现**

3a. `app/db/ai_schema.py`：`ai_task` CREATE TABLE 中 `` `stage` varchar(32) DEFAULT NULL, ``（第 50 行）后加：

```sql
            `progress_percent` tinyint unsigned DEFAULT NULL COMMENT '0-100 阶段进度，仅展示用途',
```

文件末尾 `ensure_ai_projection_columns` 之后新增（照抄其容错风格）：

```python
AI_TASK_REQUIRED_COLUMNS: dict[str, str] = {
    "progress_percent": (
        "`progress_percent` tinyint unsigned DEFAULT NULL "
        "COMMENT '0-100 阶段进度，仅展示用途'"
    ),
}


def ensure_ai_task_columns(cursor: Any) -> None:
    """Idempotently add legacy-DB columns to ``ai_task``（如 progress_percent）。"""
    try:
        cursor.execute("SHOW COLUMNS FROM `ai_task`")
        existing = {row["Field"] for row in cursor.fetchall()}
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        return
    for column_name, column_def in AI_TASK_REQUIRED_COLUMNS.items():
        if column_name not in existing:
            cursor.execute(f"ALTER TABLE `ai_task` ADD COLUMN {column_def}")
```

然后 `grep -rn "ensure_ai_projection_columns(" app/ main.py database_setup_marriage.py | grep -v def` 找到调用点，在其后追加 `ensure_ai_task_columns(cursor)`（同一 cursor 作用域内）。

3b. `tasks.py`：`_SELECT_COLUMNS` 的 `status, stage` 后插 `progress_percent`；`AiTaskRecord` 在 `stage: str | None`（154 行）后加 `progress_percent: int | None`；`from_row` 在 `stage=...` 后加：

```python
            progress_percent=(
                int(row["progress_percent"])
                if row.get("progress_percent") is not None
                else None
            ),
```

3c. `search.py:2098-2106`：

```python
async def _set_search_task_stage(
    db: AsyncSession, task_id: str, stage: str, progress: int | None = None
) -> None:
    await db.execute(
        text(
            "UPDATE ai_task SET stage = :stage, "
            "progress_percent = COALESCE(:progress, progress_percent), "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {"task_id": task_id, "stage": stage, "progress": progress},
    )
```

调用点改为：2126 `..., "validating", progress=10)`；2135 `..., "filtering", progress=30)`；2189 `..., "ranking", progress=85)`；2247 终态改为：

```python
        await _set_search_task_stage(
            db, task_id, status_value,
            progress=100 if status_value == "completed" else None,
        )
```

3d. `ai_tasks.py`：`TaskDetailResponse` 增 `progress_percent: int | None = None`；GET 响应组装（`return TaskDetailResponse(...)`）增一行 `progress_percent=task.progress_percent,`。

- [ ] **Step 4: 集成测试**（`tests/integration/ai/test_ai_tasks.py` 追加；`ai_task` 最小 INSERT 只填 NOT NULL 列：task_id/scene/task_type/idempotency_key）

```python
@pytest.mark.asyncio
async def test_real_set_search_task_stage_writes_progress(
    real_db_session: AsyncSession,
) -> None:
    from app.services.ai.search import _set_search_task_stage

    task_id = "task-progress-check-0001"
    await real_db_session.execute(
        text("DELETE FROM ai_task WHERE task_id = :t"), {"t": task_id}
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_task (task_id, owner_user_id, task_type, scene, "
            " idempotency_key, status) VALUES (:t, 1, 'search_execute', 'ai', "
            " 'k-progress-1', 'running')"
        ),
        {"t": task_id},
    )
    await real_db_session.commit()

    await _set_search_task_stage(real_db_session, task_id, "filtering", progress=30)
    row = (
        await real_db_session.execute(
            text("SELECT stage, progress_percent FROM ai_task WHERE task_id = :t"),
            {"t": task_id},
        )
    ).mappings().first()
    assert row["stage"] == "filtering"
    assert int(row["progress_percent"]) == 30
    await real_db_session.commit()
```

- [ ] **Step 5: 跑测试与全量**

Run: `python -m pytest tests/test_ai_tasks.py tests/integration/ai/test_ai_tasks.py -q && python -m pytest tests/ -q`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/db/ai_schema.py app/services/ai/tasks.py app/services/ai/search.py app/api/routes/ai_tasks.py tests/test_ai_tasks.py tests/integration/ai/test_ai_tasks.py
git commit -m "feat(search): ai_task 增加 progress_percent，搜索任务下发 10/30/85/100 进度"
```

---

### Task 6: 匹配度外显灰度开关（WP-C2 / F11，依赖 Task 1 的 PRODUCT.md）

**Files:**
- Modify: `app/core/config.py`（AI 门禁区）
- Modify: `app/services/ai/compatibility.py`（写路径 `display_eligible` 计算助手 + INSERT 参数处约 992 行）
- Test: `tests/test_ai_compatibility.py`（追加单测）

**Interfaces:**
- Consumes: `settings`（compatibility.py 已导入）；INSERT 参数字典（compatibility.py:988-995 区域）
- Produces: `_resolve_display_eligible(user_id: int) -> bool`（compatibility.py 模块级）；settings 新项 `ai_compatibility_display_mode: Literal["off","bucket","on"] = "off"`、`ai_compatibility_display_bucket_pct: int = Field(default=0, ge=0, le=100)`。读路径门禁（compatibility.py:1185-1193）不改——它按快照库值放行，写路径打开后自然生效。

- [ ] **Step 1: 写失败单测**（追加到 `tests/test_ai_compatibility.py`）

```python
def test_resolve_display_eligible_modes(monkeypatch):
    """外显灰度（方案 WP-C2/D6）：off 恒 False；on 恒 True；bucket 按稳定哈希命中。"""
    from app.core.config import settings
    from app.services.ai.compatibility import _resolve_display_eligible

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "off")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 100)
    assert _resolve_display_eligible(12345) is False  # off 优先于 pct

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "on")
    assert _resolve_display_eligible(12345) is True

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "bucket")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 0)
    assert _resolve_display_eligible(12345) is False

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "bucket")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 100)
    assert _resolve_display_eligible(12345) is True

    # 同一用户重复判定必须一致（稳定分桶，不随调用漂移）。
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 50)
    first = _resolve_display_eligible(987654321)
    for _ in range(5):
        assert _resolve_display_eligible(987654321) == first
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_compatibility.py -q -k resolve_display`
Expected: FAIL（属性/函数不存在）。

- [ ] **Step 3: 实现**

3a. `app/core/config.py`（`ai_compatibility_shadow_enabled` 之后）：

```python
    # 匹配度外显灰度（方案 WP-C2 / 决策 D6）：off=影子运行不外显（现状）；
    # bucket=按 viewer 稳定哈希放量 ai_compatibility_display_bucket_pct%；
    # on=全量外显。仅改变 ai_compatibility_snapshot.display_eligible 的写入值，
    # 读取端门禁（_apply_display_gate）与 shadow 纪律测试在 off 下保持不变。
    ai_compatibility_display_mode: Literal["off", "bucket", "on"] = "off"
    ai_compatibility_display_bucket_pct: int = Field(default=0, ge=0, le=100)
```

3b. `app/services/ai/compatibility.py`：在写快照函数附近（INSERT 之前）加模块级助手：

```python
def _resolve_display_eligible(viewer_user_id: int) -> bool:
    """按灰度模式决定该用户的匹配度快照是否外显（方案 WP-C2/D6）。

    off 恒 False（影子纪律不变）；on 恒 True；bucket 用乘法哈希做稳定
    分桶——同一用户永远得到同一结果，不随调用顺序漂移。
    """
    if settings.ai_compatibility_display_mode == "on":
        return True
    if settings.ai_compatibility_display_mode != "bucket":
        return False
    bucket = (viewer_user_id * 2654435761) % 100
    return bucket < settings.ai_compatibility_display_bucket_pct
```

然后读 `compatibility.py:920-1000`（写快照的 INSERT）。两处修改：

其一，参数字典（约 992-993 行）：

```python
            "display_eligible": 1 if _resolve_display_eligible(int(viewer_id)) else 0,
```

其二，**同一 INSERT 的 `ON DUPLICATE KEY UPDATE` 子句（约 958-981 行）必须追加一行**，否则灰度放量后已存在的旧快照 `display_eligible` 永远停留在 0、放量不生效：

```python
            " display_eligible = VALUES(display_eligible), "
```

同文件 16 行与 884-901 行注释中"display_eligible 固定 0"的表述同步改为"默认 0，外显灰度打开后按 `_resolve_display_eligible` 写入"。

- [ ] **Step 4: 跑测试与全量**

Run: `python -m pytest tests/test_ai_compatibility.py -q && python -m pytest tests/ -q`
Expected: 全 PASS。特别注意 `test_ai_compatibility.py` 既有 shadow 纪律用例必须依旧绿（默认 off 下行为零变化）。

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py app/services/ai/compatibility.py tests/test_ai_compatibility.py
git commit -m "feat(compatibility): 外显灰度开关 off/bucket/on，写路径按 viewer 稳定分桶"
```

---

### Task 7: .env 密钥治理（方案 §7）

**Files:**
- Verify/Modify: `.gitignore`、`.env.example`（均在 `xuanshiai-backend/`）
- 用户动作（不可代办）：在 dots/DeepSeek/阿里云控制台轮换密钥

- [ ] **Step 1: 核验 .gitignore**

Run: `git check-ignore -v .env ; grep -n "^\.env" .gitignore`
Expected: `.env` 被忽略。若未被忽略：把 `.env` 加入 `.gitignore` 并 `git rm --cached .env`（若曾被跟踪，需在 commit message 标注密钥已泄露必须轮换）。

- [ ] **Step 2: 核验 .env.example 无真实密钥**

Run: `grep -nE "(sk-|ak-|api_key\s*=\s*[^Y<])" .env.example`
Expected: 只有 `YOUR_DEEPSEEK_API_KEY` 类占位符。发现真实密钥立即替换为占位符。

- [ ] **Step 3: 向用户输出轮换清单**（聊天里列出，不写入仓库）：dots 控制台、DeepSeek 控制台、阿里云 AccessKey（语音）三处旧 key 作废重发 → 更新服务器 `.env` → 重启后端 → `python -m pytest tests/test_dots_provider.py -q` 验证。

- [ ] **Step 4: Commit（仅当 .gitignore/.env.example 有改动）**

```bash
git add .gitignore .env.example
git commit -m "chore(security): 密钥不落库核验，.env.example 占位符化"
```

---

## 阶段完成定义（DoD）

1. `python -m pytest tests/ -q` 全绿（636+ 用例，含新增用例）
2. 手工冒烟（本地起后端）：PATCH 搜索条件→重进页面编辑仍在；画像会话确认 7 字段→进度返回 `can_early_publish=true`；轮询 search 任务见 `progress_percent` 递增；`AI_COMPATIBILITY_DISPLAY_MODE=on` 时快照 `display_eligible=1`
3. `docs/superpowers/plans/` 本文件各任务 checkbox 勾选完毕，随后为阶段2（WP-P1/P4/P5/S3/S2）另立计划
