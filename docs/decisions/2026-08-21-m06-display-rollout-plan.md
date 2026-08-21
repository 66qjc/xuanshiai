# M06 匹配度外显 — 灰度切换方案

> **设计日期：** 2026-08-21
> **范围：** D3 前提 4 —— `display_eligible` 从 `false` 切到 `true` 的灰度方案
> **裁决依据：** ADR `2026-08-20-ai-full-feature-baseline.md` D3、`2026-08-21-m06-display-security-review.md`
> **状态：** proposed（待审阅）
> **前置依赖：** D3 前提 1（✅ shadow 验证 PASS）、前提 2（✅ 安全审查 CONDITIONAL PASS，3 项整改中）

## 1. 设计目标

在避免一次性全量切换风险的前提下，将 AI 匹配度（`compatibility-rule-v1`）从内部 shadow 解冻为对用户可见的「资料合拍参考」，替换旧 `match_score`/`legacy-rule-v1` 显示。

核心约束：
- **库中 shadow 语义不变**：`write_shadow_snapshot` 继续写 `display_eligible=0`、`experiment_bucket=shadow`，保持 shadow 纪律
- **外显是读取层决策**：通过新增 feature flag 在 `read_compatibility_snapshot` 返回时覆盖 `display_eligible`，不改变写入语义
- **可回滚**：关闭 flag 即回到 shadow 状态，无数据迁移

## 2. 技术方案

### 2.1 新增 feature flag

```python
# app/core/config.py
ai_compatibility_display_enabled: bool = False  # 外显灰度开关，默认关闭
ai_compatibility_display_rollout_pct: int = 0   # 灰度百分比 0-100，默认 0%
```

- `ai_compatibility_display_enabled`：外显总开关，false 时读取层强制 `display_eligible=False`
- `ai_compatibility_display_rollout_pct`：灰度比例，按 viewer_user_id hash 取模控制，避免每次请求抖动

### 2.2 读取层覆盖逻辑

在 `read_compatibility_snapshot` 返回 `CompatibilitySnapshotRead` 前，根据 flag 覆盖 `display_eligible`：

```python
# app/services/ai/compatibility.py — read_compatibility_snapshot 尾部
def _apply_display_gate(read: CompatibilitySnapshotRead, viewer_id: int) -> CompatibilitySnapshotRead:
    """根据外显灰度 flag 覆盖 display_eligible，不改库值。"""
    if not settings.ai_compatibility_display_enabled:
        return read.replace(display_eligible=False)
    if settings.ai_compatibility_display_rollout_pct >= 100:
        return read  # 全量外显，保留库值（仍为 0 → 需改为 True）
    # 灰度：按 viewer_id hash 决定是否外显
    bucket = hash(viewer_id) % 100
    if bucket < settings.ai_compatibility_display_rollout_pct:
        return read.replace(display_eligible=True)
    return read.replace(display_eligible=False)
```

注意：`display_eligible=True` 仅控制前端是否展示分数；`disclaimer` 和 reason_codes 始终返回，前端按 `display_eligible` 决定渲染。

### 2.3 consent scope 校验

灰度切换时需校验新 consent scope（安全审查 C1 整改项）：

```python
# 新增 scope
COMPATIBILITY_DISPLAY_CONSENT_SCOPE = "compatibility_display"

# read_compatibility_snapshot 中增加外显 consent 校验
if display_eligible and not await _has_display_consent(db, viewer_id):
    return read.replace(display_eligible=False)  # 无外显 consent 时不展示
```

### 2.4 legacy 双读/排空

外显灰度期间，旧 `match_score`/`legacy-rule-v1` 保持不变：

| 阶段 | 旧推荐流 `match_score` | 新 `compatibility` 对象 | 前端展示 |
|---|---|---|---|
| shadow（当前） | 正常返回 | `display_eligible=False` | 旧显示 |
| 灰度（flag 开，用户命中） | 正常返回 | `display_eligible=True` | 新显示（优先），旧显示备用 |
| 全量外显 | 排空/标 deprecated | `display_eligible=True` | 新显示 |
| 回滚（flag 关） | 正常返回 | `display_eligible=False` | 回到旧显示 |

排空方案：全量外显稳定后，分批次将 `match_score` 写为 null 并标 `algorithm_version=deprecated-legacy`，不突变删除。

## 3. 灰度阶段

| 阶段 | rollout_pct | 范围 | 观察指标 | 持续时间 | 回滚条件 |
|---|---|---|---|---|---|
| S0 验证 | 0% | 仅开发/测试 | shadow 验证 PASS（已完成） | — | — |
| S1 内部 | 5% | 开发团队账号 | 分数合理性、无报错、前端渲染正常 | 3 天 | 任一 P0 报错 |
| S2 小灰度 | 20% | 随机 20% 用户 | 用户反馈、分数分布、consent 拒绝率 | 5 天 | 误导投诉 ≥ 3 例 |
| S3 中灰度 | 50% | 随机 50% 用户 | 同上 + 稳定性 | 5 天 | 稳定性 < 99.9% |
| S4 全量 | 100% | 全部用户 | 同上 + legacy 排空准备 | 持续 | — |

每阶段进阶条件：上一阶段无 P0/P1 问题且观察指标达标。任一阶段触发回滚条件，关闭 `ai_compatibility_display_enabled` 立即回到 shadow。

## 4. 监控指标

| 指标 | 期望 | 告警阈值 |
|---|---|---|
| `display_eligible=True` 请求成功率 | ≥ 99.9% | < 99% |
| `coverage_insufficient` 占比 | ≤ 30% | > 50% |
| `consent_display` 拒绝率 | 记录基线 | 突增 > 20% |
| 用户投诉（误导/不准确） | < 0.1% | ≥ 0.5% |
| shadow 写入成功率（不受灰度影响） | ≥ 99.9% | < 99% |

## 5. 回滚方案

**一键回滚**：设置 `ai_compatibility_display_enabled=false`，读取层立即全部返回 `display_eligible=False`，前端回到 legacy 显示。无需数据迁移，无副作用。

回滚后 shadow 写入不受影响，已写入的 `display_eligible=0` 快照语义不变。

## 6. 实施清单

| # | 任务 | 依赖 | 状态 |
|---|---|---|---|
| G1 | 安全审查 C1：新增 `compatibility_display` consent scope | 本文档 | ⏳ |
| G2 | 安全审查 C2：`COMPATIBILITY_POLICY_REVISION` 同步 v2 | G1 | ⏳ |
| G3 | 安全审查 C3：prompt 工程优化 income_band 抽取 | 独立 | ⏳ |
| G4 | 新增 `ai_compatibility_display_enabled` + `rollout_pct` 配置 | G1 | ⏳ |
| G5 | `read_compatibility_snapshot` 增加 `_apply_display_gate` | G4 | ⏳ |
| G6 | 前端展示改造（替换 legacy-rule-v1 显示） | G5 | ⏳（第四子项目） |
| G7 | S1-S4 灰度执行 + 监控 | G5, G6 | ⏳ |

## 7. 不改动声明

本方案不改：
- `write_shadow_snapshot`（继续写 `display_eligible=0`，保持 shadow 纪律）
- `COMPATIBILITY_ALGORITHM_VERSION` / `SCORE_SEMANTICS` / `COMPATIBILITY_EXPERIMENT_BUCKET`（冻结常量）
- 维度权重和 coverage 阈值
- 旧 `match_score`/`legacy-rule-v1` 写入路径（排空期保持不变）
