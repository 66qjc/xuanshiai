# 墨相师六维实时整理 WebSocket（2026-09-02）

## 变更记录

- **2026-09-02，破坏性变更：** `/api/v1/voice/moxiang-master` 不再接受
  `mode=profile_build`，统一改为 `mode=moxiang_journey`。旧 `progress`、
  `session_ready` 和聊天内即时确认候选已删除；客户端改处理 `journey_ready`、
  `extraction_status`、`journey_progress`。
- 新进度是六维理解覆盖度，不是已确认或已发布资料数。正式建档、确认和发布仍复用
  原邀请、确认卡和发布能力。

## 1. 连接与鉴权

**基本信息**：用于墨相师自然对话、最终文本/ASR 的异步候选抽取和六维进度推送。
完整 URL：`GET wss://<host>/api/v1/voice/moxiang-master?token=<access_token>`；协议为
WebSocket（无 HTTP 请求体）；需要登录，仅当前用户可访问自己的会话；成功握手
`101 Switching Protocols`；消息是 UTF-8 JSON。

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法/非法示例 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `token` | query | string | 是 | 无 | 有效 access JWT | 当前用户身份 | 合法：`?token=<jwt>`；非法：空值/过期 JWT |

连接示例：`wss://api.example.com/api/v1/voice/moxiang-master?token=<access_token>`。
无 HTTP body；缺失或失效 token 时以 `1008` 关闭。客户端不得把 token 写入日志。

## 2. 客户端消息

### `session_start`

**基本信息**：建立或重连主体会话；第一条业务消息必须是它。

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法/非法示例 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `type` | JSON body | string | 是 | 无 | 固定 `session_start` | 消息类型 | 合法：`session_start`；非法：`start` |
| `mode` | JSON body | string | 是 | 无 | 固定 `moxiang_journey` | 新旅程协议标记 | 合法：`moxiang_journey`；非法：`profile_build` |
| `subject` | JSON body | string | 否 | `personal` | `personal` / `ideal_partner` | 当前画像主体 | 合法：`personal`；非法：`other` |
| `consentVersion` | JSON body | string | 否 | `profile-text-v1` | 存在的授权版本 | 授权快照版本 | 合法：`profile-text-v1`；非法：未知版本 |

请求体示例：

```json
{"type":"session_start","mode":"moxiang_journey","subject":"personal","consentVersion":"profile-text-v1"}
```

非法示例：`{"type":"session_start","mode":"profile_build"}`，返回
`AI_INPUT_INVALID`。

### 对话与控制消息

| `type` | 额外字段 | 校验与业务含义 |
| --- | --- | --- |
| `text_message` | `text`、可选 `clientTurnId` | 去空白后 1–2000 字；最终文字先持久化、独立入队；相同 `clientTurnId` 重传回放同一任务。 |
| `revise_text` | `text`、可选 `clientTurnId` | 同样作为最终文字入队，再让墨相师回复。 |
| `audio_start` / `audio_chunk` / `audio_end` | `audio_chunk.data` 为 base64 PCM | 仅 `audio_end` 的最终 ASR 转写入队；`partial_transcript` 永不入队或计进度。 |
| `subject_switch` | `subject` | 只允许两个合法主体；会话、进度、任务严格隔离。 |
| `build_invite_accept` | `subject`、`invite_id` | 只接受本人待处理邀请；同一事务锁邀请和候选，生成/补充草稿。 |
| `build_invite_snooze` | `subject`、`invite_id` | 只处理本人待处理邀请，不生成草稿。 |
| `listen` / `cancel` | 无 | 沿用播报/录音控制；`cancel` 不取消已入队候选任务。 |

文字示例：

```json
{"type":"text_message","clientTurnId":"turn-20260902-01","text":"我周末喜欢看展，也需要安静地独处。"}
```

## 3. 服务端消息

### `journey_ready`

重连或切换主体都会返回此消息，随后必推一份 `journey_progress`。

| 字段 | 类型 | 必返 | 枚举/空值 | 业务含义 | 示例 |
| --- | --- | --- | --- | --- | --- |
| `type` | string | 是 | 固定 `journey_ready` | 消息类型 | `journey_ready` |
| `session_id` | string | 是 | 非空 | 当前主体会话 ID | `ab12...` |
| `subject` | string | 是 | 两个主体之一 | 当前主体 | `personal` |
| `journey_stage` | string | 是 | `chatting/building/ready/published` | 当前旅程阶段 | `chatting` |
| `resumed` | boolean | 是 | — | 是否复用已有会话 | `true` |

### `extraction_status`

只含任务和主体标识，**绝不含候选正文、证据原文或 provider 输出**。

| 字段 | 类型 | 必返 | 枚举 | 业务含义 | 示例 |
| --- | --- | --- | --- | --- | --- |
| `type` | string | 是 | 固定 `extraction_status` | 消息类型 | `extraction_status` |
| `subject` | string | 是 | 两个主体之一 | 任务归属主体 | `personal` |
| `task_id` | string | 是 | 非空 | 可观测任务 ID | `task-...` |
| `status` | string | 是 | `queued/processing/completed/failed` | 仅代表抽取任务状态 | `processing` |

### `journey_progress`

| 字段 | 类型 | 必返 | 枚举/范围 | 业务含义 | 示例 |
| --- | --- | --- | --- | --- | --- |
| `type` | string | 是 | 固定 `journey_progress` | 消息类型 | `journey_progress` |
| `subject` | string | 是 | 两个主体之一 | 进度归属主体 | `personal` |
| `overall_percent` | number | 是 | 0–100 | 六维百分比平均；不是发布状态 | `25` |
| `dimensions` | object | 是 | 固定六键 | 每维进度，见下方 | — |
| `dimensions.<dimension>.percent` | number | 是 | `0/50/100` | 0、1、2+ 个有效高置信候选对应的百分比 | `50` |
| `dimensions.<dimension>.evidence_count` | integer | 是 | `>=0` | 去重后的有效高置信候选数 | `1` |

六个固定维度：`personality_social`、`intimacy_pattern`、`lifestyle`、
`emotional_expression`、`relationship_boundaries`、`future_expectations`。

成功返回示例：

```json
{"type":"journey_progress","subject":"personal","overall_percent":8.3333,"dimensions":{"personality_social":{"percent":0,"evidence_count":0},"intimacy_pattern":{"percent":0,"evidence_count":0},"lifestyle":{"percent":50,"evidence_count":1},"emotional_expression":{"percent":0,"evidence_count":0},"relationship_boundaries":{"percent":0,"evidence_count":0},"future_expectations":{"percent":0,"evidence_count":0}}}
```

`build_invite`、`build_invite_resolved`、`confirm_card`、`publish_ready`、`ai_reply`、语音
转写和错误消息沿用已有形状。邀请摘要只出现在正式邀请卡，不出现在聊天流。

## 4. 使用方法与业务规则

1. 连接后先发 `session_start(mode=moxiang_journey)`；收到 `journey_ready` 再发对话或
   切换主体。断线重连重复此步骤，并以 `journey_progress` 覆盖本地临时状态。
2. `queued/processing` 展示「正在理解…」；`completed` 后消费进度消息；`failed` 仅给
   无打扰的可恢复提示，已存在进度不回退。
3. 进度只统计 confidence `>=0.75` 的候选，以 `(session_id, content_hash)` 去重；重复
   表述合并来源轮次，不虚涨进度。个人与理想对象绝不交叉写入。
4. 各轮任务独立观察；后续消息、`cancel` 或断线不取消已落库任务。重连恢复持久化快照。
5. 邀请门槛复用既有候选/轮次规则；每会话最多一个 `pending` 邀请，单会话自动整理邀请
   上限 3 次（2026-09-03 由 2 提升）。接受邀请才生成
   `suggested` 草稿，仍需原确认和发布流程；稍后、越权、过期或重复接受均不建档。
6. 服务端必须显式启用 `ai_moxiang_journey_enabled`，且通过 `AiFeature.PROFILE` 的生产
   合规/provider/保留期门禁；禁用时不可回退到旧协议。
7. 知遇每轮回复前按当前会话的整理进度、尚未确认的基础硬字段与已沉淀内容感知提问，
   围绕空白处自然追问、不复述已确认信息（`build_context` 注入，纯聊模式不注入）。

## 5. 错误与关闭

| 传输层/业务码 | 触发条件 | 前端处理建议 |
| --- | --- | --- |
| close `1008` | token 缺失、无效或过期 | 重新登录后建立新连接 |
| `AI_INPUT_INVALID` | 旧 mode、非法主体、空/过长文本、邀请参数错、重复接受 | 修正输入；旧客户端升级到 `moxiang_journey` |
| `AI_CONSENT_REQUIRED` | 授权缺失/撤回 | 引导重新授权 |
| `AI_FEATURE_DISABLED` | 实时旅程或生产门禁未启用 | 展示不可用，不回退旧协议 |
| `AI_TEMPORARILY_UNAVAILABLE` | 任务、数据库、ASR 或 provider 临时不可用 | 保留已显示进度，稍后重连；不要伪造进度 |

错误示例：`{"type":"error","code":"AI_INPUT_INVALID","message":"请使用最新墨相师旅程"}`。

## 6. 兼容性与上线迁移

这是前后端同步发布的破坏性变更。部署顺序：执行旅程表迁移及
`20260902_01_retire_legacy_moxiang_profile_extract_up.sql` → 注册/启动 Worker 的
`moxiang_candidate_extract` → 显式打开生产开关 → 发布新前端。退役迁移以
`AI_LEGACY_MOXIANG_RETIRED` 审计取消存量旧 master `profile_extract`，不删除对话、
候选、邀请或草稿。旧版 `profile_build` 会收到 `AI_INPUT_INVALID`，无兼容层。
