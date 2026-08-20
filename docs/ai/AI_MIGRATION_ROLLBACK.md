# AI 迁移回滚计划（ai-migration-rollback-plan-v1）

> **范围：** 本文档仅适用于 disposable 测试数据库的迁移演练，禁止直接用于生产数据库。生产回滚需另行授权、备份与 DPA 评审。
> **状态：** NOT_RUN（本轮证据治理分支只写计划结构，不执行真实 migration）
> **关联：** 计划 `docs/superpowers/plans/2026-08-15-ai-feature-contract-remediation.md` Task 10 Step6；模板 `artifacts/templates/ai-migration-rollback-plan.template.json`；manifest `migrations/ai/manifest.json`。

## 1. 前置条件

- 仅在 disposable 测试数据库执行；禁止连接生产或共享 staging 数据库。
- 执行前必须创建快照/备份并记录引用与 SHA-256。
- `AI_MASTER_ENABLED=true` 时 `manage_ai_migration.py down` 会被安全门禁拒绝；回滚演练需在 `AI_MASTER_ENABLED=false` 的隔离环境执行。
- 每一步 DDL 必须记录开始/结束时间、退出码与影响行数。

## 2. 快照/备份引用

| 项 | 值 |
|---|---|
| 快照引用（路径或卷标） | NOT_RUN |
| 快照 SHA-256 | NOT_RUN |
| 创建时间 | NOT_RUN |
| 已验证可恢复 | false |

## 3. DDL 步骤记录

每一步记录 `id`、`direction`（up/down）、`status`、`last_durable_step`、`started_at`、`finished_at`、`exit_code`、`affected_rows`。当前全部 NOT_RUN。

| step_id | direction | status | last_durable_step | started_at | finished_at | exit_code | affected_rows |
|---|---|---|---|---|---|---|---|
| up-01 | up | NOT_RUN | 0 | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |
| up-02 | up | NOT_RUN | 0 | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |
| down-02 | down | NOT_RUN | 0 | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |
| down-01 | down | NOT_RUN | 0 | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |

说明：
- `up-01`/`up-02` 对应 `migrations/ai/20260809_01_hardening_up.sql` 与 `20260809_02_outbox_cleanup_up.sql`。
- `down-02`/`down-01` 为反向回滚，顺序必须与 up 相反。
- `last_durable_step` 标记最后一个已提交且可恢复的步骤索引；中途失败时从该步补偿。

## 4. 迁移矩阵（disposable DB 演练）

计划 Task 11 Step3 要求在 disposable database 完成：fresh up → verify → repeated up → down → previous verify → restore up；再对旧 schema fixture 升级。中途 DDL 故障注入必须可恢复。

| 阶段 | 命令 | 预期 | 实际 | exit_code | 状态 |
|---|---|---|---|---|---|
| fresh up | `python scripts/manage_ai_migration.py up` | 全部 up 应用成功 | NOT_RUN | NOT_RUN | NOT_RUN |
| verify after up | checksum 校验 + 表结构核对 | 4/4 checksum 匹配 | NOT_RUN | NOT_RUN | NOT_RUN |
| repeated up | 二次 up | 幂等无变化 | NOT_RUN | NOT_RUN | NOT_RUN |
| down | `python scripts/manage_ai_migration.py down` | 全部 down 回滚成功 | NOT_RUN | NOT_RUN | NOT_RUN |
| verify after down | 表已删除/回退 | 与 up 前一致 | NOT_RUN | NOT_RUN | NOT_RUN |
| restore up | 再次 up | 与首次 up 一致 | NOT_RUN | NOT_RUN | NOT_RUN |
| 旧 schema fixture 升级 | 从旧 fixture up | 兼容升级 | NOT_RUN | NOT_RUN | NOT_RUN |

## 5. 中途失败补偿

- 每一步 DDL 失败时，先记录失败步骤与错误，不得继续后续步骤。
- 从 `last_durable_step` 对应的快照恢复，或从最近一次成功 up 的状态继续。
- 若 down 步骤失败，必须从快照完整恢复，不得半自动修补 schema。
- 故障注入演练：在 `up-02` 中途 kill 进程，验证可从 `last_durable_step=1` 恢复。

| 项 | 值 |
|---|---|
| 故障注入步骤 | NOT_RUN |
| 补偿验证 | false |

## 6. down 丢列前数据损失范围

回滚前必须明确每个 down 步骤会删除哪些列/表，以及对应的数据损失范围。当前全部 NOT_RUN，需在真实演练时逐列填写。

| down 步骤 | 删除对象 | 数据损失范围 | 是否可恢复 | 备注 |
|---|---|---|---|---|
| down-02 | NOT_RUN | NOT_RUN | NOT_RUN | 需在演练时填写 |
| down-01 | NOT_RUN | NOT_RUN | NOT_RUN | 需在演练时填写 |

## 7. 操作员确认

| 项 | 值 |
|---|---|
| 操作员 | NOT_RUN |
| 确认时间 | NOT_RUN |
| 确认内容 | NOT_RUN |

## 8. 结果

- `result`: NOT_RUN
- `instructions`: 仅在 disposable DB 演练时填写；禁止用作生产命令。
- 本计划不执行真实 migration；待 Task 11 容器重建授权后在 disposable DB 填充。
