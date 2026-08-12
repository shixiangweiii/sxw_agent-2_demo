# Runtime 核心数据模型详解

本文以当前源码为准，介绍 Runtime 的领域模型、SQLite 持久化模型以及它们之间的权威边界。核心不是“把一次 LLM 调用存下来”，而是用 Run、Activity、Canonical Event、Checkpoint、ToolExecution 和 Release 构造一个可裁决、可恢复的执行运行时。

## 1. 先建立全景图

```text
Conversation
  └─ Run                     一轮用户请求及其唯一终态
       ├─ Activity            可领取、租约化的执行单元
       ├─ Canonical Event     append-only 业务事实
       ├─ Checkpoint          append-only + revision CAS 恢复点
       ├─ ToolExecution       工具效果及幂等权威
       ├─ ArtifactLink        指向 SHA-256 CAS 内容
       ├─ Signal / Timer      外部输入与持久化 retry 时间驱动
       └─ Release fingerprint 入场时冻结的不可变执行语义
```

Runtime 的唯一事实源不是 Engine 内存里的 message list，也不是 SSE 连接或 Trace，而是 `runtime.db` 内以短事务提交的表和 Artifact CAS。

## 2. Current-only schema 身份

Runtime 只接受一份当前 schema：

- 定义文件：`agent/runtime/adapters/sqlite/schema.sql`
- 通用校验：`common/sqlite_schema.py`
- Runtime 接入：`agent/runtime/adapters/sqlite/database.py`

`schema_meta` 只有三个字段：

```sql
CREATE TABLE schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_digest TEXT NOT NULL CHECK (length(schema_digest) = 64),
    created_at INTEGER NOT NULL
) STRICT;
```

`schema_digest` 是完整 `schema.sql` **原始字节**的 SHA-256，不是表名列表或人工版本号。`ensure_current_schema()` 在一个 `BEGIN IMMEDIATE` 中完成判定：

1. 空库：执行完整 schema，再写入 digest。
2. 非空库：只校验 `schema_meta.id=1` 的 digest 是否完全相等。
3. 缺少 meta、digest 不等或 schema 文件非法：抛出 `SchemaIdentityError`，稳定错误码为 `CURRENT_SCHEMA_MISMATCH`。

这个项目不会修改陌生库，也不会自动删库。换 schema 时由操作者显式删除本地 DB 并重建。

### 两类“版本”不要混淆

- `schema_meta.schema_digest` 是本地 SQLite 存储布局身份。
- `RuntimeEnvelope`、`CanonicalEvent`、Artifact 契约、`EvidenceSet` 等对外 DTO 保留 `schema_version: Literal["1"]`，表示当前传输契约。`ToolResultEnvelope` 是另一个严格的当前 DTO，但其模型本身没有 `schema_version` 字段。

SQLite 的 `runs`、`run_events`、`checkpoints`、`release_manifests` 不再存各自的 `schema_version`。传输契约字段不等于库表兼容机制。

## 3. 核心领域模型

源码入口：`agent/runtime/domain/models.py`。

### 3.1 RuntimeEnvelope

`RuntimeEnvelope` 是 Run 入场时冻结的执行信封，主要包括：

- 请求与幂等：`request_id`、`client_request_id`、`idempotency_key`
- 对话与轮次：`conversation_id`、`turn_id`、`run_id`
- 租户与引擎：`principal_id`、`agent_id`、`engine`
- 控制：`deadline_at`、`cancel_token_id`
- 执行语义：`release_fingerprint`
- 输入溯源：`input_event_id`、`attachment_refs`、`created_at`

`release_fingerprint` 由 admission 从当时的 `active_releases` 读取并冻结。它不会因之后 Worker 重启而改变。

### 3.2 RunRecord

`RunRecord` 是信封之上的可变运行状态：

- `status` / `terminal_status` / `terminal_payload`
- `revision`：Run 状态 CAS 版本
- `next_seq`：下一个 Canonical Event 序号
- `current_activity_id`
- `pending_input`
- `input_text`、`updated_at`
- `trace_id`：只用于 API 进程与 Worker 进程的诊断关联，不参与业务裁决

### 3.3 ActivityRecord 与 Claim

Activity 是可领取的持久工作单元。关键字段是：

- 稳定身份：`activity_id`、`run_id`、`logical_key`、`type`
- 调度：`status`、`attempt`、`available_at`
- 所有权：`lease_owner`、`lease_expires_at`、`fencing_token`
- CAS：`revision`
- 结果/恢复：`result`、`error`、`resume_payload`

`Claim` 是 `RunRecord + ActivityRecord` 的不可变快照，不是新的数据库实体。Worker 必须带着 Claim 里的 `fencing_token` 执行后续写入。

### 3.4 CanonicalEvent

Canonical Event 是 append-only 的业务事实，包含 `run_id`、`turn_id`、单 Run 递增 `seq`、event type、payload/ref、visibility/sensitivity 和可选 Activity/ToolExecution 关联。

`run_events` 表不重复保存 release fingerprint。读取事件时 Store 与 `runs` join，用 Run 冻结的 release 派生 `CanonicalEvent.release_fingerprint`。

典型事件包括：

- `USER_MESSAGE_COMMITTED`
- `OUTPUT_GENERATION_STARTED` / `OUTPUT_DELTA_COMMITTED`
- `TOOL_CALL_COMMITTED` / `TOOL_RESULT_COMMITTED`
- `CHECKPOINT_COMMITTED`
- `ASSISTANT_MESSAGE_COMMITTED` / `CITATION_SET_COMMITTED`
- `RUN_TERMINATED`

`OUTPUT_GENERATION_STARTED` 投影成 SSE `text_start`，带稳定 `message_id`、当次 `generation_id` 与 supersede 信息。重试和恢复不覆盖旧 delta，而是开启新 generation。

### 3.5 WorkingState 与 CheckpointRecord

`WorkingState` 记录 goal、constraints、plan、facts、pending input、budget、Artifact/Evidence refs。它不重复保存 release fingerprint。

`CheckpointRecord` 只有：

```text
checkpoint_id, run_id, activity_id, revision,
working_state, engine_state, created_at
```

注意已经不存在的字段：

- checkpoint 不存 `schema_version`
- checkpoint 不存 `release_fingerprint`
- checkpoint 不存额外引擎状态外部指针；当前 typed state 直接由 `engine_state` 承载

`save_checkpoint()` 同时校验 Activity fence 和 `expected_revision`，并能把 engine-owned events 与 checkpoint 放在同一短事务中提交。Native 只接受当前唯一 typed codec；存在 checkpoint 但无法严格解析时是 `NATIVE_CHECKPOINT_INVALID`，不猜测、不回退到 history。

### 3.6 EngineOutcome 与 RuntimeIO

Engine Adapter 统一端口是：

```text
execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

`EngineOutcomeKind` 只描述 Engine 为什么结束：`COMPLETED`、`RETRYABLE_FAILURE`、`TERMINAL_FAILURE`、`WAITING_INPUT`、`CANCELLED`。它是 Coordinator 的输入，不能直接写 Run 终态。

`RuntimeIO` 提供 committed event sink、checkpoint CAS、Tool Broker、cancel/deadline probe 和：

```python
set_final_assistant(text, message_id, generation_id)
```

Native 使用这个显式 final override 指定最后一个完整、非空、无 ToolCall 的 Assistant turn。Coordinator 成功收口时优先取它；ADK Adapter 未设置时，才使用 Sink 的累计文本。

### 3.7 ReleaseManifest

`ReleaseManifest` 只有：

```python
engine: EngineName
components: dict[str, str]
```

fingerprint 是 manifest 规范 JSON 的 SHA-256。`components` 覆盖 current schema digest、engine/runtime/shared source digest、最终 ToolCatalog digest、provider/model/checkpoint codec、语义配置、资源上限和实际安装依赖版本。

`release_manifests` 由 SQLite trigger 禁止 UPDATE/DELETE，`active_releases` 是三个 engine 的当前指针。Worker 启动使用唯一公开写入路径：

```text
activate_current_releases(plan_execute, agent_loop, native_loop)
```

三份 manifest 的写入/核对、活跃旧 Run 检查和三个 active pointer 切换全部在一个 `BEGIN IMMEDIATE` 内，不会暴露“一半新 release”。

## 4. 状态机

### 4.1 RunStatus

```text
非终态：ACCEPTED, DISPATCH_PENDING, RUNNING,
          WAITING_RETRY, WAITING_INPUT, CANCEL_REQUESTED

终态：  SUCCEEDED, FAILED, CANCELLED, TIMED_OUT, REJECTED
```

错误 release 的 Worker 根本不能 claim Run，因此没有“release 不兼容终态”。Run 保持待调度，由符合 exact release 的 Worker 领取，或最终由绝对 deadline 收口。

### 4.2 ActivityStatus

```text
PENDING -> CLAIMED -> RUNNING
RUNNING -> SUCCEEDED | FAILED | CANCELLED
RUNNING -> WAITING_RETRY | WAITING_INPUT
RUNNING -> RECONCILE | MANUAL
```

租约过期恢复不是简单无条件回到 `PENDING`：Store 先检查 deadline、cancel 和未决 ToolEffect。只有可安全重放的边界才重派，不确定副作用会进入 reconcile/manual。

## 5. SQLite 表与权威职责

| 表 | 权威职责 |
|---|---|
| `schema_meta` | current schema 字节 digest |
| `conversations` | 对话归属、下一 turn seq |
| `release_manifests` | 不可变 release 内容 |
| `active_releases` | 每个 engine 的当前入场指针 |
| `runs` | Run 状态、终态、deadline、冻结 release |
| `run_requests` | `(principal_id, agent_id, idempotency_key)` 幂等事实 |
| `activities` | 工作单元、lease、fencing、attempt |
| `run_events` | append-only Canonical Event |
| `checkpoints` | append-only 恢复点及 revision CAS |
| `tool_executions` | ToolEffect 状态、稳定 slot、结果/ref |
| `artifact_metadata` / `artifact_links` | CAS 元数据与业务引用 |
| `signals` / `cancellation_commands` | 幂等外部命令 |
| `timers` | 当前已装配的持久化 retry timer；绝对 deadline 由 Store 另行扫描 |
| `runtime_workers` | Worker 心跳、state 与 release map |

表中不重复存储可从 Run 权威派生的 release fingerprint。例如 Event 读模型中的 fingerprint 来自 Run join，Checkpoint/WorkingState 则根本不带该字段。

## 6. 关键事务边界

### 6.1 Admission

一个写事务内按顺序完成：

```text
幂等重放判定
-> 读取 engine 的 active release
-> 校验 Artifact
-> 创建/更新 Conversation
-> 写 Run + ENGINE_RUN Activity + run_request
-> 链接输入 Artifact
-> 追加用户消息和状态事件
```

幂等重放先于 conversation busy 检查。activation 和 admission 都使用写事务读/写 active pointer，因此不会冻结到一个半切换 release。

### 6.2 Claim

`claim_next()` 使用一条 `UPDATE ... RETURNING` 在短写事务中领取 Activity，并且 SQL 同时精确匹配：

```text
(runs.engine, runs.release_fingerprint) in worker.release_map
```

普通 claim、恢复后 claim 和 reconcile claim 共用这个条件。

### 6.3 Event / Checkpoint / Terminal

- Event batch 与 `runs.next_seq` 同事务，回滚不留 seq 洞。
- Checkpoint CAS 和引擎事件可同事务提交。
- 成功时 final assistant、由 committed Evidence 派生的 citations 和 `RUN_TERMINATED` 同事务。
- Store 自有事件不允许 Engine 伪造。

## 7. AttemptOwnershipLost：所有权不是业务失败

`agent/runtime/domain/errors.py` 定义 `AttemptOwnershipLost`。以下情形会被转成该控制异常：

- stale fencing token / lease expired
- Activity 没有合法运行租约
- checkpoint revision CAS 冲突
- Coordinator 不可达防御中的 claim/release mismatch

它必须穿过 Engine 和 Coordinator 到 Worker，不能被包装成 ToolResult，也不能使 Run 终态化。Worker 停止当地 attempt，让持久化 lease recovery 决定下一个所有者。

## 8. 建议阅读顺序

1. `agent/runtime/domain/models.py`：领域词汇和状态机。
2. `agent/runtime/ports/store.py` 与 `agent/runtime/ports/engine.py`：存储和 Engine 端口。
3. `agent/runtime/adapters/sqlite/schema.sql`：唯一 current schema。
4. `common/sqlite_schema.py` 与 `agent/runtime/adapters/sqlite/database.py`：schema digest 启动边界。
5. `agent/runtime/adapters/sqlite/store.py`：admission、claim、checkpoint、terminal 事务。
6. `agent/runtime/application/events.py` 与 `agent/runtime/application/coordinator.py`：RuntimeIO 和终态所有权。
7. `agent/runtime/worker/main.py` 与 `agent/runtime/worker/dispatcher.py`：release 装配、领取和 lease 续租。

> 本项目当前的 authority 是单机多进程共享 SQLite + Artifact CAS。租约/fencing 解决的是本机进程级故障恢复，不等于跨主机 HA。
