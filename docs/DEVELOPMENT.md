# 项目操作文档

本文档记录 Xuanshi AI API 后端项目的常用环境、启动、测试、检查和维护命令。默认命令使用 Windows PowerShell，并在项目根目录 `E:\houduan\xuanshiai` 执行。

## 一、环境要求

- Python 3.11 或更高版本
- MySQL 8 或更高版本
- Redis 7 或更高版本
- Git
- 推荐安装 `uv`，用于创建虚拟环境和管理依赖

检查工具是否可用：

```powershell
python --version
git --version
uv --version
```

## 二、首次初始化

### 方式 A：使用 uv（推荐）

```powershell
uv sync --extra dev
```

该命令会创建 `.venv`、安装运行依赖和开发依赖，并根据 `pyproject.toml` 更新 `uv.lock`。

### 方式 B：使用 Python venv 和 pip

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

如果 PowerShell 阻止激活脚本，可以直接使用 `.venv\Scripts\python.exe` 和 `.venv\Scripts\pytest.exe`。

## 三、环境变量配置

首次使用时复制模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```powershell
code .env
```

重点配置项：

| 配置项 | 用途 | 本地默认值 |
| --- | --- | --- |
| `DATABASE_URL` | MySQL 异步连接地址 | `mysql+aiomysql://root:YOUR_MYSQL_PASSWORD@127.0.0.1:3306/xuanshiai` |
| `AUTO_INIT_DB` | 启动时自动创建数据库和表 | 开发/测试环境 `true`，staging/production 必须为 `false` |
| `REDIS_URL` | Redis 连接地址 | `redis://127.0.0.1:6379/0` |
| `SECRET_KEY` | JWT 签名密钥 | 仅开发占位值，部署前必须替换 |
| `CORS_ORIGINS_RAW` | 允许跨域的前端地址 | `http://localhost:3000,http://localhost:5173` |
| `UPLOAD_DIR` | 上传文件目录 | `storage/uploads` |

`.env` 包含本地密钥和连接信息，不要提交到 Git。完整配置说明见 `.env.example`。

### Mock 认证服务

没有短信服务商或微信小程序配置时，可以在开发/测试环境使用 Mock：

```env
ENVIRONMENT=testing
SMS_PROVIDER=mock
SMS_MOCK_CODE=123456
WECHAT_PROVIDER=mock
WECHAT_MOCK_OPENID_PREFIX=mock-openid-
```

Mock 短信验证码固定为 `123456`，Mock 微信登录凭证使用 `mock-code-001`、`mock-code-002` 等格式。Mock 只在 `development` 和 `testing` 环境允许，生产环境启用 Mock 时应用配置校验会失败。Mock 不会改变现有认证接口的路径和请求响应结构，也不会新增公开的验证码查询接口。

### MySQL 项目数据库配置

1. 打开 `.env`，把 `YOUR_MYSQL_PASSWORD` 替换为你安装 MySQL 时设置的 `root` 密码：

```env
DATABASE_URL=mysql+aiomysql://root:你的密码@127.0.0.1:3306/xuanshiai
```

2. 使用 MySQL 客户端登录并创建项目数据库。下面命令会提示输入密码，不会把密码写进命令历史：

```powershell
& 'H:\mysql\bin\mysql.exe' --protocol=TCP --host=127.0.0.1 --port=3306 --user=root --password
```

登录后执行：

```sql
CREATE DATABASE IF NOT EXISTS xuanshiai
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

退出 MySQL：

```sql
EXIT;
```

3. 如果密码中包含 `@`、`:`、`/`、`#` 或空格，需要先进行 URL 编码，再填入 `DATABASE_URL`。例如 `@` 编码为 `%40`。

4. 验证数据库连接。命令会提示输入密码，并执行 `SELECT 1`：

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
& 'H:\mysql\bin\mysql.exe' --protocol=TCP --host=127.0.0.1 --port=3306 --user=root --password --database=xuanshiai --execute="SELECT 1 AS connection_ok;"
```

当前项目使用 `database_setup_marriage.py` 初始化基础表和一期商业化表。开发/测试环境启动 `uv run uvicorn app.main:app --reload` 时会自动执行幂等初始化；也可以手动运行：

```powershell
python database_setup_marriage.py
```

该脚本使用 `CREATE TABLE IF NOT EXISTS`，但生产环境仍应先备份并在发布窗口执行；真实支付、提现和第三方回调配置不能使用开发环境 Mock。

生产或预发布环境必须配置：

```env
AUTO_INIT_DB=false
```

生产环境禁止在应用启动时自动创建数据库和执行结构变更。数据库账号、密码、主机、端口和库名仍需填写在 `DATABASE_URL` 中，应用不会自动推断或生成这些敏感配置。

## 四、启动服务

### 开发模式

```powershell
uv run uvicorn app.main:app --reload
```

兼容入口：

```powershell
python main.py
```

默认访问地址：

```text
根路径：   http://127.0.0.1:8000/
健康检查： http://127.0.0.1:8000/api/v1/health
Swagger：   http://127.0.0.1:8000/docs
ReDoc：     http://127.0.0.1:8000/redoc
```

指定其他端口：

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

生产模式示例：

```powershell
$env:ENVIRONMENT = "production"
$env:DEBUG = "false"
$env:DOCS_ENABLED = "false"
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 10.6 AI 一期真实依赖验收环境（Task 1）

`compose.ai-test.yml` 只用于一期集成测试，不连接本地开发库。它启动 MySQL 8、Redis 7 以及两个独立的 AI Worker 进程：

```powershell
docker compose -f compose.ai-test.yml up -d mysql redis worker-a worker-b
docker compose -f compose.ai-test.yml ps
```

默认映射到 `127.0.0.1:3307`（MySQL）和 `127.0.0.1:6380`（Redis），数据库为 `xuanshiai_ai_test`，root 空密码只在这个临时测试服务中启用。可以用 `AI_TEST_MYSQL_DATABASE`、`AI_TEST_MYSQL_PORT` 和 `AI_TEST_REDIS_PORT` 覆盖默认值。

启动服务后，测试 fixture 会运行现有 `database_setup_marriage.initialize_database()`，然后从真实 MySQL 读取 `information_schema` 并启动真实子进程：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/ai/test_ai_schema_real_db.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/ai/test_ai_worker_real_db.py -q
```

测试不在依赖不可用时跳过；连接失败、bootstrap 失败和真实 schema/Worker 合约失败都必须显式暴露。Task 1 的初始 P0 已在 Task 2 收口：补齐 `ai_profile_turn`、`ai_search_result`、projection/session 生命周期字段，active-session 历史唯一约束已兼容；真实 bootstrap、schema、迁移和双 Worker 验收由 `tests/integration/ai/` 覆盖。

停止并清理这组专用服务和测试卷：

```powershell
docker compose -f compose.ai-test.yml down -v --remove-orphans
```

如果服务停在 `Created` 或健康检查异常，先执行 `docker compose -f compose.ai-test.yml ps --all` 和 `docker compose -f compose.ai-test.yml logs mysql redis worker-a worker-b`；确认只涉及 `xuanshiai-ai-test-*` 后，再执行上面的 `down -v` 重新建立干净的测试卷。

## 五、测试与代码检查

运行全部测试：

```powershell
uv run pytest
```

运行单个测试文件或指定测试：

```powershell
uv run pytest tests/test_health.py -v
uv run pytest tests/test_health.py::test_health_endpoint -v
```

运行 Ruff 代码检查：

```powershell
uv run ruff check .
```

自动修复 Ruff 可以修复的问题：

```powershell
uv run ruff check . --fix
```

检查 Python 语法和编译：

```powershell
uv run python -m compileall -q app main.py
```

每次修改代码后至少执行：

```powershell
uv run ruff check .
uv run pytest
```

## 六、数据库和 Redis 检查

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
Test-NetConnection 127.0.0.1 -Port 6379
```

项目已经预留 `app/db`、`app/models`、`app/schemas` 和 `app/services` 目录。新增业务模块时，先确认数据库模型、迁移方案和接口契约，再接入实际数据库连接。

## 七、Git 常用命令

```powershell
git status
git diff
git status --short
```

提交前建议依次执行：

```powershell
uv run ruff check .
uv run pytest
git diff --check
git status
```

不要提交 `.env`、真实密钥、数据库密码、`.venv`、`.uv-cache`、缓存、上传文件和运行日志。

## 八、项目目录

```text
app/
  api/       HTTP 路由和 API 聚合
  core/      配置和基础设施
  db/        数据库连接与会话
  models/    ORM 模型
  schemas/   Pydantic 请求/响应模型
  services/  业务服务层
docs/        项目操作和开发文档
tests/       自动化测试
storage/     本地运行时文件
logs/        本地日志目录
```

会员价格和积分数值也支持环境变量配置。`MEMBERSHIP_<套餐>_PRICE`、`MEMBERSHIP_<套餐>_ORIGINAL_PRICE`、`MEMBERSHIP_<套餐>_DAILY_PRICE` 覆盖对应会员套餐的数据库价格字段；`POINT_COST_<功能编码>` 覆盖积分商品每次兑换消耗。未设置的价格继续使用数据库值，未设置的积分商品消耗继续使用 `config_point_product.points_cost`。签到和任务奖励使用 `POINT_CHECKIN_REWARD`、`POINT_PROFILE_COMPLETE_REWARD`、`POINT_REALNAME_VERIFIED_REWARD` 配置。

## 九、AI 编码工具规则

使用 Codex 或 Claude Code 修改代码前，必须先阅读项目根目录的 `AGENTS.md` 或 `CLAUDE.md`，并遵守其中引用的 `PROJECT_RULES.md`。

规则正文预留在 `PROJECT_RULES.md`，由项目负责人持续补充。

## 十、AI 功能（画像/搜索/匹配度）运行说明

AI 功能一期全部默认关闭。开发/测试环境可通过 `.env` 打开开关并使用 `mock` Provider；生产环境在 `ai_policy_approved`、`ai_provider_approved`、`ai_retention_policy_version` 未全部满足且 Provider 非 mock 之前，应用配置校验会失败，对外恒返回 `503 AI_FEATURE_DISABLED`（retryable=false），普通资料编辑与手工筛选不受影响。

### 10.1 开关与批准门禁

| 配置项 | 用途 | 默认值 |
| --- | --- | --- |
| `AI_MASTER_ENABLED` | AI 总开关 | `false` |
| `AI_PROFILE_ENABLED` | AI 画像模块开关 | `false` |
| `AI_SEARCH_ENABLED` | AI 搜索模块开关 | `false` |
| `AI_COMPATIBILITY_SHADOW_ENABLED` | 匹配度 shadow 模块开关 | `false` |
| `AI_POLICY_APPROVED` | 合规批准标记（生产启用前置） | `false` |
| `AI_PROVIDER_APPROVED` | Provider 批准标记（生产启用前置） | `false` |
| `AI_RETENTION_POLICY_VERSION` | 保留期策略版本（生产启用前置） | 空 |
| `AI_PROVIDER` | 一期唯一 Provider | `mock` |
| `AI_AUDIT_ENABLED` | `ai_generation_audit` 审计写入开关 | `true` |
| `AI_METRICS_BACKLOG_WARN_THRESHOLD` | outbox/purge 积压指标告警阈值 | `1000` |

生产环境启用任一 AI 开关必须同时满足三个批准项且 Provider 不是 mock，否则 `Settings` 校验失败（fail-closed）。`evaluate_ai_release_gate` 在运行期再次校验同一门禁，任何 blocker 都返回 `AI_FEATURE_DISABLED`。

### 10.2 启动 Worker

```powershell
# 单轮运行（安全空转预览，不访问数据库、不写任何数据）
uv run python -m app.workers.ai_worker --once --dry-run

# 单轮真实运行（reap 过期租约 → claim → start → 分发已注册 handler）
uv run python -m app.workers.ai_worker --once

# 常驻循环（默认每 5 秒一轮，可 --idle-seconds 调整）
uv run python -m app.workers.ai_worker

# 指定每轮领取/回收上限
uv run python -m app.workers.ai_worker --batch-size 20
```

- 业务 handler 在导入时全部显式注册：`profile_extract` / `search_parse` /
  `search_execute` / `compatibility` / `profile_projection`（发布后投影重建）/
  `cleanup`（删除/撤回物理清理）。独立 `python -m app.workers.ai_worker`
  进程即可处理全部 `ai_task` 业务任务，不依赖路由导入的副作用注册。
- 没有已注册业务 handler 时 Worker 绝不触碰数据库（`--once` 非 dry-run 也是纯只读空转）。
- 任务恢复：Worker 崩溃后过期租约由 reaper 回收转 `retry_wait`，下一轮重新领取；进行中的 handler 由心跳续租保护。

#### 10.2.1 清理消费者（derivation-outbox）

删除/撤回的异步传播（投影失效 + 派生 search/compat 结果标 stale）由
`derivation_outbox` 消费者循环执行，调度入口在同一个 Worker 进程：

```powershell
# 单轮安全空转预览（不访问数据库、不写任何数据）
uv run python -m app.workers.ai_worker --consumers --once --dry-run

# 单轮真实运行（claim 未消费的 outbox 删除事件 → 分发已注册清理 handler）
uv run python -m app.workers.ai_worker --consumers --once

# 常驻消费循环（默认每 5 秒一轮，可 --idle-seconds 调整）
uv run python -m app.workers.ai_worker --consumers

# 与业务任务 Worker 并跑时建议各自独立进程（业务任务与清理消费者分开调度）
uv run python -m app.workers.ai_worker --consumers --idle-seconds 10
```

- 重复消费由 `derivation_consumer_receipt` 拦截；旧事件（版本落后）写
  `superseded` 收据，不覆盖新投影。
- 单轮输出 `claimed=... applied=... superseded=... duplicate=... skipped=...`。

### 10.3 Mock Provider 与测试

```powershell
$env:ENVIRONMENT = "testing"
uv run pytest tests/test_ai_release_gates.py -v
```

- `MockAIProvider` 实现 `structured_extract` / `parse_search_query` / `moderate_text`，并支持 `failures=["timeout","http_429","schema_invalid","policy_blocked"]` 注入失败。
- 生产环境启用 Mock Provider 会被 `Settings` 校验拒绝；测试库建表使用幂等 `CREATE TABLE IF NOT EXISTS`。

### 10.4 发布验证（不改变生产开关）

```powershell
uv run python scripts/verify_ai_release.py --environment testing --report artifacts/ai-release-evidence.json
```

脚本聚合配置门禁、数据库 16 AI + 3 derivation 表、OpenAPI 四路径、隐私矩阵、mock 失败注入、删除回放、shadow 报告和回滚演练证据；任何一项缺失输出稳定 blocker、`release_gate=disabled-until-approved` 且退出码 2，绝不误报通过，也不修改任何开关。

### 10.5 生产禁用运行

```powershell
$env:ENVIRONMENT = "production"
$env:DEBUG = "false"
$env:AUTO_INIT_DB = "false"
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

生产 `AUTO_INIT_DB` 必须为 `false`；未批准条件保持 `AI_FEATURE_DISABLED`。回滚：先关闭 `AI_MASTER_ENABLED` 和各模块开关，停止 Worker/消费者，旧 `/discovery/*` 接口与 `legacy-rule-v1` 字段保持可用。

## 11. G5 证据链脚手架（Task 10，2026-08-17 证据治理分支）

> 本节登记 `codex/ai-g5-g7-20260817` 分支的证据链脚手架状态。本轮硬约束：禁止运行 pytest/ruff/python 脚本来验证，禁止构建/容器/微信/稳定性观察。所有"已完成"仅指结构/代码已写，运行验证 NOT_RUN。

### 11.1 证据 schema 与 builder

- `artifacts/schemas/ai-evidence-v1.schema.json`：G0-G7 gates、commandEvidence、hashMap、reviewer、result enum 完整。
- `scripts/build_ai_evidence.py`：command allowlist、git state、redact、load_command_evidence、validate_evidence_shape、build_evidence、CLI 完整。
- `tests/test_build_ai_evidence.py`（Task10 Step1）：TDD 失败测试，覆盖 required fields、反例（空 JSON/旧 SHA/未知命令/非零 exit/result≠PASS）、validate_evidence_shape、redact、production+mock blocker、PENDING review blocker。写但不跑。

### 11.2 发布验证 verifier 改造（Task10 Step3）

```powershell
uv run python scripts/verify_ai_release.py --target internal --report artifacts/ai-internal-readiness.json
```

- `--target internal|production`（required）；`--environment` 保留为 deprecated 兼容期，`--target` 优先。
- `target=production` 强制 `environment=production` + provider≠mock + review_status=REVIEWED + result=PASS。
- `target=internal` 时 environment=development/testing，禁止 production。
- 新增 evidence bundle 聚合：读取 `artifacts/ai-evidence-bundle.json`（build_ai_evidence 产物），校验 schema、SHA、hash、时效（72h）、所有 Gate（G0-G7）、production approvals。
- 任何证据缺失 → exit 2 + `disabled-until-approved`，绝不误报 GO。
- `--environment` 旧用法保留兼容期：`--target production --environment testing` 会强制 production；`--target internal --environment production` 会降级到 testing。

### 11.3 质量集扩充（Task10 Step4）

- `artifacts/ai-profile-quality.json`、`ai-search-quality.json`、`ai-compatibility-shadow-quality.json` 替换为带 `provenance`、`use_limitation`、`reviewed`、`allow_expansion` 的版本化结构。
- compatibility 保持 `allow_expansion=false`（未过 expansion 阈值）。
- metrics 全部 null（NOT_RUN），结构占位，不填真实评测数据。

### 11.4 Ruff 修复状态（Task10 Step5）

- 2026-08-19 G5 清零：pyproject 显式钉住经典默认规则集 `select = ["E4", "E7", "E9", "F"]`（ruff 0.16 扩大了默认规则集，全仓出现 818 项新规则告警，其中 655 项 B008 是 FastAPI `Depends` 惯用法；历史契约是经典默认集，钉住以避免随 ruff 版本漂移，不采用 blanket ignore）。钉住集内修复：`profile.py` F402×3（`dataclasses.field` 别名 `dc_field`）、TRY004×2（类型校验改抛 `TypeError`）、E701/E702×22（admin 路由单行多语句拆分）、`test_ai_search_real_db.py` F841×1。
- `ruff check app tests scripts` → All checks passed。

### 11.5 migration rollback 演练（Task10 Step6 / Task11 Step7）

- `docs/ai/AI_MIGRATION_ROLLBACK.md`：快照/备份引用、每 DDL step 记录、迁移矩阵、中途失败补偿、down 丢列前数据损失范围说明。
- 2026-08-19 在 disposable DB `xuanshiai_ai_drill`（mysql 127.0.0.1:3307）执行真实演练：fresh up → verify current → repeated up（幂等）→ 注入数据依赖 DDL 故障（重复 `(snapshot_id, rank_position)` 行触发 1062 on `uk_ai_search_result_rank`，历史记 `rollback_failed`）→ 删除重复行恢复 down → verify previous（probe 数据可读）→ restore up → verify current（turn_id 回填 legacy-turn-<id>、generation 复位 1，与 down SQL 注释的损失范围一致）。
- 快照引用 `/tmp/ai-drill-snapshot-6f8f09c.sql`（sha256 `a5f69feb...`）与全步骤记录在 `artifacts/ai-rollback-drill.json`（result=PASS、blockers=[]、6 步全 PASS）。

### 11.6 G7 稳定性/回滚占位（Task 13）

- `artifacts/ai-stability-report.json`：NOT_RUN 占位，`result: NOT_RUN`，blockers 列出"3-5天观察未开始"等。
- `artifacts/ai-rollback-drill.json`：2026-08-19 已由真实演练填充（见 §11.5），不再是占位。
- 3-5 天稳定性观察未开始。production 恒 NO-GO。

### 11.7 Graphify 更新

- 本轮代码/文档变更后需从工作区根 `graphify update .`；本轮硬约束不执行。
- 登记在 `docs/待完成事项.md` §六。
