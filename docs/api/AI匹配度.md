# AI 匹配度接口（M06 双向规则、证据解释与 shadow 快照）

接口前缀：`/api/v1/ai`。本文件是 M06「资料合拍参考」（统一方案 §9，执行计划 Task 11）对外的完整契约。

对外文案固定为「资料合拍参考」，并附免责声明「仅根据双方当前可见且已确认资料整理，供了解和破冰参考」。内部算法名为 `compatibility-rule-v1`；旧 `match_score`/`match_reason` 的算法版本恒为 `legacy-rule-v1`。

### 变更记录

- 2026-08-08：新增 2 个 `/api/v1/ai/compatibility/*` 路径（GET 读取 + POST recompute）。旧推荐流的 `match_score/match_reason` 保持 `legacy-rule-v1` 语义并在卡片上标注 `algorithm_version`/`match_score_source=legacy-rule-v1`；新兼容度只写 shadow，不影响首页推荐排序、不触发喜欢/申请/聊天。

通用请求头（所有接口）：

```http
Authorization: Bearer <access_token>   # 必需
Content-Type: application/json          # 写接口必需
Idempotency-Key: <8-128 位 ASCII>       # 写接口必需；重复 key + 相同请求摘要回放第一次结果
X-Request-ID: req_01J...                # 可选，1-128 位 [A-Za-z0-9._:-]，用于日志与错误关联
```

通用说明：

- 前置条件：已登录；已同意授权 `compatibility_shadow`；功能开关开启（`ai_master_enabled` + `ai_compatibility_shadow_enabled`，生产环境还需三批准门禁）。开关关闭统一返回 `503 AI_FEATURE_DISABLED`。
- 快照状态固定为 `ready / stale / blocked / coverage_insufficient`（统一方案 §9.4，执行计划 §3.1）。`blocked` 不展示候选；`coverage_insufficient` 不伪造完整分；`stale` 不能当最新解释。
- **硬门禁先于规则**：每次读取/重算都重新过 `CandidateVisibilityService` 门禁（账号、审核、隐私 `who_can_see_me`、双向拉黑、资料完整、媒体审核等）；denied 的 pair 统一 `404 CANDIDATE_NOT_VISIBLE`，不返回具体拒绝原因，不泄露用户存在、认证或拉黑关系。
- **双向规则**（§9.2）：`direction_score(A→B)` 是 A 的已确认偏好对 B 的已确认资料在可用维度上的加权平均；`pair_score(A,B)` 是两个方向分数的调和平均；`coverage` 是两方向可用权重占比的较小值。双方方向 coverage 均达 `0.50` 才生成可比较 shadow 分数；低于阈值保存 `coverage_insufficient`，缺失维度记 `DIMENSION_UNKNOWN`，不补负面事实。方向交换必须可回放（forward.directions == reverse.directions 反向）。
- **维度权重冻结**（§9.2，非科学概率）：年龄 20、城市/异地 15、婚姻 10、学历 10、身高 10、收入 10、兴趣标签 15、关系期待 10。MBTI、认证、活跃、会员、置顶不进入兼容度。
- **证据与解释**（§9.3）：结果使用稳定原因码（`AGE_MUTUAL_WITHIN_RANGE`、`CITY_MUTUAL_ACCEPTED`、`MARRIAGE_MUTUAL_ACCEPTED`、`EDUCATION_MUTUAL_WITHIN_RANGE`、`HEIGHT_MUTUAL_WITHIN_RANGE`、`INCOME_MUTUAL_WITHIN_RANGE`、`INTEREST_OVERLAP`、`RELATIONSHIP_GOAL_SHARED`、`DIMENSION_UNKNOWN`、`COVERAGE_INSUFFICIENT`、`CANDIDATE_NOT_VISIBLE`）。每条原因码绑定 `evidence`（字段 key、source revisions、可展示标记、限制说明），不存对方敏感原文；模板解释优先。
- **Shadow 纪律**（§9.5/§10.4）：新快照只写 `ai_compatibility_snapshot`，`display_eligible` 固定为 `false`、`experiment_bucket=shadow`，不影响排序和用户动作；旧 `match_score/match_reason` 语义恒为 `legacy-rule-v1`，新客户端才读取可选的 `compatibility` 对象。C-06 质量反馈只做离线评估（Task 12），学习排序仍受 Phase 5 门禁。
- 结果读取以 MySQL 为事实源；Redis 断开不影响本接口。
- 错误响应统一为：

```json
{
  "detail": {
    "code": "CANDIDATE_NOT_VISIBLE",
    "message": "目标用户当前不可见",
    "request_id": "req_01JXc5...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

`retryable` 是后台任务的重试语义，客户端不得据此推断资源是否存在。所有 4xx/5xx 都带 `request_id`；普通响应不含原文、Provider trace 或密钥。

### 错误码速查

| HTTP | code | 触发 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 401 | — | 未登录/令牌失效 | false | 重新登录 |
| 404 | `CANDIDATE_NOT_VISIBLE` | 目标不可见（隐私/拉黑/审核/认证/账号门禁失败）；不返回具体原因 | false | 提示「该用户当前不可见」，不展示合拍参考 |
| 403 | `AI_CONSENT_REQUIRED` | 未同意 `compatibility_shadow` 授权或已撤回 | false | 引导重新授权 |
| 400 | `AI_INPUT_INVALID` | Idempotency-Key 非法 | false | 修正入参后重试 |
| 409 | `RESULT_STALE` | `expected_*_profile_revision` 与当前版本不符 | true | 刷新资料版本后重试 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 但请求摘要不同 | false | 更换幂等键 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关关闭或生产门禁未满足 | false | 提示功能不可用 |
| 503 | `AI_TEMPORARILY_UNAVAILABLE` | 任务基础设施临时失败 | true | 稍后重试 |

---

## 1. 查询与目标用户的资料合拍参考

**基本信息**：返回当前用户对目标用户的 shadow 资料合拍参考；完整 URL `GET /api/v1/ai/compatibility/{target_user_id}`；HTTP Method `GET`；需要登录；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `target_user_id` | path | integer | 是 | 无 | ≥ 1 | 目标用户 ID；不可与自己相同（自我引用按不可见处理） |
| `Authorization` | header | string | 是 | 无 | Bearer token | 当前用户访问令牌 |

### 请求体示例

无请求体（GET）。

非法示例：`GET /api/v1/ai/compatibility/0` → `400`（path 校验 `ge=1`）。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `snapshot_id` | string | 是 | 空字符串=尚无快照 | — | 快照 ID（`cp_` 前缀 hex） | `cp_01JXc5...` |
| `status` | string | 是 | — | `ready/stale/blocked/coverage_insufficient` | 快照状态（§3.1） | `ready` |
| `algorithm_version` | string | 是 | — | 固定 `compatibility-rule-v1` | 内部算法版本 | `compatibility-rule-v1` |
| `score_semantics` | string | 是 | — | 固定 `rule_based_reference_shadow` | 分数语义（参考 shadow） | `rule_based_reference_shadow` |
| `compatibility_index` | number | 否 | `null`：无分数（stale/blocked/coverage_insufficient） | 0..100 | 资料合拍参考分 | `78.0` |
| `coverage` | number | 否 | `null`：无快照 | 0..1 | 双向可用维度覆盖率 | `0.74` |
| `directions` | object | 否 | `null`：无分数 | — | 双向方向分（见下表） | `{"viewer_to_target":82.0,"target_to_viewer":74.0}` |
| `directions.viewer_to_target` | number | 否 | — | 0..100 | 当前用户偏好 → 目标资料 | `82.0` |
| `directions.target_to_viewer` | number | 否 | — | 0..100 | 目标偏好 → 当前用户资料 | `74.0` |
| `reason_codes` | array[string] | 是 | `[]` | 稳定原因码（§9.3） | 原因码列表 | `["INTEREST_OVERLAP","CITY_MUTUAL_ACCEPTED"]` |
| `profile_revision_pair` | object | 否 | `{}` | — | 快照基于的 profile revision pair | `{"viewer":12,"target":18}` |
| `privacy_revision_pair` | object | 否 | `{}` | — | 快照基于的 privacy revision pair | `{"viewer":4,"target":7}` |
| `experiment_bucket` | string | 是 | — | 固定 `shadow` | 实验桶（shadow，不影响排序/动作） | `shadow` |
| `display_eligible` | boolean | 是 | — | 固定 `false` | 是否可用于对外展示/排序（一期恒 false） | `false` |
| `disclaimer` | string | 是 | — | 固定文案 | 免责声明 | `仅根据双方当前可见且已确认资料整理，供了解和破冰参考` |
| `calculated_at` | string(datetime) | 是 | — | — | 快照计算时间（UTC） | `2026-08-07T08:00:00Z` |
| `expires_at` | string(datetime) | 是 | — | — | 快照过期时间（默认 10 分钟），过期读取返回 `stale` | `2026-08-07T08:10:00Z` |
| `evidence` | array[object] | 是 | `[]` | — | 原因码证据引用（见下表） | — |

`evidence[]` 每一项：

| 字段 | 类型 | 必返 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- |
| `reason_code` | string | 是 | 对应原因码 | `AGE_MUTUAL_WITHIN_RANGE` |
| `field_keys` | array[string] | 是 | 涉及的字段 key（不存对方敏感原文） | `["age"]` |
| `displayable` | boolean | 是 | 该证据是否可对外展示（unknown/coverage 类为 false） | `true` |
| `limitation` | string | 是 | 限制说明（模板解释） | `双方年龄均落在对方已确认的年龄偏好区间内` |
| `source_revisions` | object | 否 | 该证据基于的双方五维 revision | `{"viewer":{"profile":12,...},"target":{...}}` |

### 返回示例

```json
{
  "snapshot_id": "cp_01JXc5...",
  "status": "ready",
  "algorithm_version": "compatibility-rule-v1",
  "score_semantics": "rule_based_reference_shadow",
  "compatibility_index": 78.0,
  "coverage": 0.74,
  "directions": {"viewer_to_target": 82.0, "target_to_viewer": 74.0},
  "reason_codes": ["AGE_MUTUAL_WITHIN_RANGE", "CITY_MUTUAL_ACCEPTED", "INTEREST_OVERLAP"],
  "profile_revision_pair": {"viewer": 12, "target": 18},
  "privacy_revision_pair": {"viewer": 4, "target": 7},
  "experiment_bucket": "shadow",
  "display_eligible": false,
  "disclaimer": "仅根据双方当前可见且已确认资料整理，供了解和破冰参考",
  "calculated_at": "2026-08-07T08:00:00Z",
  "expires_at": "2026-08-07T08:10:00Z",
  "evidence": [
    {
      "reason_code": "AGE_MUTUAL_WITHIN_RANGE",
      "field_keys": ["age"],
      "displayable": true,
      "limitation": "双方年龄均落在对方已确认的年龄偏好区间内",
      "source_revisions": {"viewer": {"profile": 12}, "target": {"profile": 18}}
    }
  ]
}
```

无快照/覆盖度不足（不伪造完整分）：

```json
{
  "snapshot_id": "",
  "status": "coverage_insufficient",
  "algorithm_version": "compatibility-rule-v1",
  "score_semantics": "rule_based_reference_shadow",
  "compatibility_index": null,
  "coverage": null,
  "directions": null,
  "reason_codes": ["COVERAGE_INSUFFICIENT"],
  "profile_revision_pair": {},
  "privacy_revision_pair": {},
  "experiment_bucket": "shadow",
  "display_eligible": false,
  "disclaimer": "仅根据双方当前可见且已确认资料整理，供了解和破冰参考",
  "calculated_at": "2026-08-07T08:00:00Z",
  "expires_at": "2026-08-07T08:00:00Z",
  "evidence": []
}
```

### 使用方法与业务规则

- 前置条件：已登录；目标用户存在且当前可见；本人已同意 `compatibility_shadow` 授权（读取已生成快照时未强制二次校验授权，但重算路径必须有效授权）。
- 每次读取重新过可见性门禁与 revision 校验：
  - 目标被拉黑/隐藏/账号失效 → `404 CANDIDATE_NOT_VISIBLE`（不返回具体原因）。
  - 双方 profile 或 privacy revision 与快照记录不一致，或快照过期 → 返回 `stale`（不能当最新解释），同时将该快照落库标记为 `stale`。
  - 快照为 `blocked` → 返回 `blocked`，`compatibility_index`/`directions` 为 `null`（不展示候选分数）。
  - 无快照或覆盖度不足 → 返回 `coverage_insufficient`，`compatibility_index` 为 `null`（不伪造完整分）。
- 本接口为只读，不消耗次数额度，不产生用户动作。
- 幂等：GET 天然幂等。
- 边界场景：无快照、目标注销/拉黑、双方资料变化、快照过期后前端应提示「资料已更新，请重新获取」。

---

## 2. 重算与目标用户的资料合拍参考

**基本信息**：请求重算 shadow 快照并创建 `compatibility` 任务；完整 URL `POST /api/v1/ai/compatibility/{target_user_id}/recompute`；HTTP Method `POST`；需要登录；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `target_user_id` | path | integer | 是 | 无 | ≥ 1 | 目标用户 ID；不可与自己相同 |
| `expected_viewer_profile_revision` | body | integer | 是 | 无 | ≥ 0 | 客户端持有的当前用户 profile revision（乐观锁） |
| `expected_target_profile_revision` | body | integer | 是 | 无 | ≥ 0 | 客户端持有的目标 profile revision（乐观锁） |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII `[A-Za-z0-9._:-]` | 幂等键；重复请求回放第一次结果 |

### 请求体示例

合法：

```http
POST /api/v1/ai/compatibility/42/recompute HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: compat-42-20260807-01
Content-Type: application/json

{"expected_viewer_profile_revision":12,"expected_target_profile_revision":18}
```

非法示例（Idempotency-Key 过短）：

```http
POST /api/v1/ai/compatibility/42/recompute HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: short
Content-Type: application/json

{"expected_viewer_profile_revision":12,"expected_target_profile_revision":18}
```

响应：`400 AI_INPUT_INVALID`。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `snapshot_id` | string | 是 | — | 本次重算对应的快照 ID（`cp_` 前缀 hex）；任务完成后由 GET 读取 | `cp_01JXc5...` |
| `task_id` | string | 是 | — | `compatibility` 任务 ID，通过 `GET /api/v1/ai/tasks/{task_id}` 轮询 | `at_01JXc5...` |
| `status` | string | 是 | — | 快照预测状态；任务尚未执行完成，统一为 `coverage_insufficient`（实际结果以 GET 为准） | `coverage_insufficient` |
| `poll_after_ms` | integer | 是 | — | 建议轮询间隔（毫秒） | `1000` |
| `expires_at` | string(datetime) | 否 | `null` | 建议重试/过期时间 | `2026-08-07T08:10:00Z` |

### 返回示例

```json
{
  "snapshot_id": "cp_01JXc5...",
  "task_id": "at_01JXc5...",
  "status": "coverage_insufficient",
  "poll_after_ms": 1000,
  "expires_at": "2026-08-07T08:10:00Z"
}
```

### 使用方法与业务规则

- 前置条件：已登录；目标当前可见；已同意 `compatibility_shadow` 授权；功能开关开启。
- 调用顺序：先通过 `GET /api/v1/ai/profile-revisions` 或现有资料版本接口取得双方 `profile_revision`，再提交 recompute；任务完成后用 `GET /api/v1/ai/compatibility/{target_user_id}` 读取结果（结果由 `GET /api/v1/ai/tasks/{task_id}` 反映任务状态）。
- **硬门禁先于版本校验**：目标不可见 → `404 CANDIDATE_NOT_VISIBLE`；随后 `expected_*_profile_revision` 与当前版本不符 → `409 RESULT_STALE`（版本已变化，需刷新后重试）。
- **幂等与防重**：同 `user_id + 接口 + Idempotency-Key + 请求摘要` 回放第一次结果；请求摘要不同返回 `409 TASK_IDEMPOTENCY_CONFLICT`。相同 key 下 snapshot_id 复用。
- 频率/额度：本任务不消耗次数额度；每用户可用同一幂等键反复回放。
- 状态流转：`ai_task` 通用状态机（queued→leased→running→succeeded；版本变化→superseded）。Worker 完成前再次过可见性门禁；任务期间双方版本变化 → 任务 `superseded`，旧结果不覆盖新状态。
- 边界场景：目标在任务执行期间被拉黑/注销 → 任务按 `RESULT_STALE` 失败；授权撤回 → 任务不创建（403）。

---

## 3. 旧推荐流兼容说明

- 旧推荐流（`/api/v1/discovery/*`）仍返回 `match_score/match_reason`，卡片上标注 `algorithm_version=legacy-rule-v1`、`match_score_source=legacy-rule-v1`；评分逻辑与排序完全不变。
- 新兼容度（`compatibility-rule-v1`）只写 `ai_compatibility_snapshot`，`display_eligible=false`、`experiment_bucket=shadow`，不影响推荐排序、喜欢/申请/聊天等任何用户动作。
- 旧 `user_match_recommend`/`user_match_score_history` 继续承载旧推荐来源与历史，`match_score` 语义恒为 `legacy-rule-v1`；新双向快照不回写旧行。
- C-06 质量反馈只做离线评估（Task 12），学习排序仍受 Phase 5 门禁，本期不实现。
