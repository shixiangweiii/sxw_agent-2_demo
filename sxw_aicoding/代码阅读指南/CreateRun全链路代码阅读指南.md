# CreateRun 全链路代码阅读指南

本文从 `POST /api/v1/runs` 开始，一直跟到 Worker 领取 Activity、Engine Adapter 执行，以及正常成功路径由 Coordinator 提交唯一终态。内容以当前 current-only schema 和 exact release 逻辑为准。

## 1. 端到端架构

```text
Client
  -> POST /api/v1/runs
  -> Runtime API:8000
       -> AdmissionService
       -> SqliteRuntimeStore.admit()
       -> runtime.db 持久化接受

Runtime Worker（无 HTTP）
  -> claim_next(release_map)
  -> RunCoordinator.execute_claim()
  -> EngineAdapter.execute(request, RuntimeIO)
       plan_execute -> AdkEngineAdapter
       agent_loop   -> AdkEngineAdapter
       native_loop  -> NativeLoopAdapter
  -> Tool Broker / Artifact / ARAG / Skill / A2A
  -> final assistant + citation + terminal 原子提交

Client
  <- committed run_events 的 SSE replay/tail
```

API 进程只负责 admission、status、cancel、signal、Artifact 和 SSE，不加载 LLM 或远程工具目录。Worker 才负责最终 ToolCatalog、release 和 Engine Adapter 装配。

## 2. 在 CreateRun 之前：两个进程的启动边界

### 2.1 API 启动

`agent/main.py` 的 lifespan 构造：

```text
RuntimeDatabase
-> SqliteRuntimeStore
-> store.initialize()
-> app.state.settings
-> app.state.runtime_store
-> app.state.artifact_store
```

`store.initialize()` 只会在空库安装完整 current schema，或对非空库校验 `schema_meta.schema_digest`。digest 是完整 `schema.sql` 字节的 SHA-256。不匹配时以 `CURRENT_SCHEMA_MISMATCH` 终止启动，不会改写已有库。

API 不注册 release。`GET /healthz` 只读 `active_releases`，因此 API 启动成功不代表 Worker 已 ready。

### 2.2 Worker 启动

`agent/runtime/worker/main.py` 保持严格顺序：

```text
初始化 current schema
-> 加载 LLM 和工具源
-> 构造 agent_loop/native_loop 最终工具面
-> 校验同名、同描述、同 schema、同 effect policy
-> 构造严格 ToolCatalog 并注册到 Broker
-> 计算三份 ReleaseManifest
-> 构造 2 个 AdkEngineAdapter + 1 个 NativeLoopAdapter
-> activate_current_releases(三份 manifest)
-> 启动 RuntimeWorker
```

`ReleaseManifest` 只由 `engine + components` 组成，components 包含 current schema digest、源码 digest、ToolCatalog digest、provider/model/checkpoint codec、语义配置、资源上限和实际安装依赖版本。

`activate_current_releases()` 要求恰好是 `plan_execute`、`agent_loop`、`native_loop` 三份 manifest。它在一个 `BEGIN IMMEDIATE` 中：

1. 写入或核对不可变 manifest。
2. 拒绝在存在另一 fingerprint 非终态 Run 时切换相应 engine。
3. 原子切换三个 active pointer。

任一步失败都整体回滚。API admission 与 release activation 都在 SQLite 写事务中读/写 pointer，避免入场与切换竞态。

## 3. HTTP 接入

入口在 `agent/runtime/api/runs.py`。请求体主要包括：

```json
{
  "client_request_id": "UUID",
  "conversation_id": null,
  "principal_id": "demo-user",
  "agent_id": "demo-agent",
  "engine": "native_loop",
  "input": {
    "text": "什么是混合召回？",
    "attachment_refs": []
  },
  "deadline_at": null
}
```

边界约束：

- `Idempotency-Key` header 必填。
- `engine` 只能是三种引擎之一。
- `deadline_at` 若提供必须是带 UTC offset 的绝对时间；未提供时由服务端使用默认 deadline。
- `attachment_refs` 必须已经在 Artifact metadata 中存在。
- TraceMiddleware 提供的 trace id 会持久化到 Run，但不参与幂等 digest。

handler 从 `request.app.state` 取出 settings/store，当次构造 `AdmissionService`，再调用 `create()`。HTTP 返回 `202 Accepted` 只表示请求已经持久化，不表示 Engine 执行完成。

## 4. AdmissionService：组装命令

源码：`agent/runtime/application/admission.py`。

### 4.1 请求 digest

digest 包含归一化的 client request：

```text
client_request_id, conversation_id, principal_id, agent_id,
engine, input.text, input.attachment_refs, deadline_at
```

附件顺序和用户文本内的空白都有语义，不会被洗掉。服务端生成的默认 deadline 不参与 digest，避免同一请求重放时因“当前时间”不同而冲突。

### 4.2 AdmissionCommand

Service 生成 `req_`、`run_`、`turn_`、`cancel_`、`evt_` 等 UUIDv4 身份，并组装 `AdmissionCommand`。这一层不读 active release；release 必须在 Store 写事务里冻结，否则会与 Worker 切换指针产生 TOCTOU 竞态。

## 5. SqliteRuntimeStore.admit：持久化入场

源码：`agent/runtime/adapters/sqlite/store.py`。整个 admission 是一个短 `BEGIN IMMEDIATE`。

### 5.1 先判幂等重放

Store 先查：

```text
(principal_id, agent_id, idempotency_key)
```

- key 与 request digest 都相同：返回原 Run，`reused=true`。
- key 相同但 digest 不同：`IDEMPOTENCY_KEY_REUSE` / HTTP 409。

这一步必须早于 conversation busy 检查，否则同一请求的合法重放会被错误拒绝。

### 5.2 冻结 exact release

Store 在同一写事务中查 `active_releases WHERE engine=?`。未找到时返回 `NO_ACTIVE_RELEASE`；找到后将 fingerprint 直接写入 `runs.release_fingerprint`。

Run 一旦入场，它的 release 就不再跟随 active pointer 变化。

### 5.3 校验 Artifact 和 Conversation

- 任一 attachment metadata 不存在：`ARTIFACT_NOT_FOUND`。
- 未传 `conversation_id`：创建 Conversation，第一个 `turn_seq=1`。
- 指定了 Conversation：要求 principal/agent 归属一致，并原子增加 `next_turn_seq`。
- 同一 Conversation 只允许一个非终态 Run，由 partial unique index 保护，冲突投影为 `CONVERSATION_BUSY`。

### 5.4 写入 Run、Activity 和事件

新 Run 的持久化结果：

```text
runs.state = DISPATCH_PENDING
activities:
  type = ENGINE_RUN
  logical_key = engine:0
  state = PENDING
  activity_id = UUIDv5(run_id, logical_key)
```

同一事务还写入：

- `run_requests` 幂等记录。
- 去重后的 input Artifact links，而 Envelope 仍保留原始顺序和重复项。
- `USER_MESSAGE_COMMITTED`。
- Run `ACCEPTED -> DISPATCH_PENDING` 状态事件。
- Activity `None -> PENDING` 状态事件。

事件 seq 与 `runs.next_seq` 同事务更新，回滚不会留下 seq 洞。

## 6. Worker claim：只领取自己能解释的 Run

`RuntimeWorker` 持有 `release_map: {engine: fingerprint}`。每轮调度先 maintenance，然后在并发槽位允许时调用：

```text
claim_next(worker_id, lease_ms, now_ms, release_map)
```

claim SQL 除了要求 Activity 可用、Run 未超时和状态匹配，还将每一对 `(engine, fingerprint)` 编入 SQL predicate。领取成功后：

```text
Activity: PENDING -> CLAIMED
attempt += 1
fencing_token += 1
lease_owner / lease_expires_at 被设置

Run: DISPATCH_PENDING -> RUNNING（普通路径）
```

不同 fingerprint 的 Worker 不会领到该 Run，更不会为它写一个伪终态。如果始终没有正确 Worker，Run 由绝对 deadline 收口。

## 7. RunCoordinator：统一裁决层

源码：`agent/runtime/application/coordinator.py`。

### 7.1 先建立当前 attempt 所有权

Coordinator 先用 `worker_id + fencing_token` 把 Activity `CLAIMED -> RUNNING`，再重读 Run。这样能看到 claim 之后到达的 cancel，并且在任何 Engine 副作用之前拒绝陈旧 Worker。

reconcile-only Activity 在查 adapter/checkpoint/history 之前分流，只能调查既有 ToolEffect，不会重放 Engine 或原工具。

### 7.2 exact release 防御断言

SQL 已保证 exact claim。Coordinator 仍会对比：

```text
adapter.release_fingerprint == run.envelope.release_fingerprint
```

若不等，`CLAIM_RELEASE_MISMATCH` 作为不可达的所有权防御异常冒泡给 Worker，只中止 attempt，不改写 Run 终态。

### 7.3 编译权威输入

Coordinator 读取：

- Run 的最新 checkpoint（可为 `None`）。
- Conversation 内 committed user messages 和成功 assistant messages。
- Activity 的 attempt/fencing/resume payload。

然后构造 `EngineRunRequest` 与当次 `CommittedEventSink`/`RuntimeIO`。失败 partial delta、上一个 attempt 的进程内 session 都不会被当作 Conversation history。

## 8. 三引擎 Adapter 分流

### 8.1 plan_execute / agent_loop

两者走 `agent/runtime/adapters/adk_engines.py` 的 `AdkEngineAdapter`。它在每个 attempt 里创建 ADK `InMemorySessionService` 和 `InMemoryArtifactService`，将 canonical history 重放进去，attempt 结束后丢弃。

ADK Engine 内部仍走 `ReasoningEngine`/stream event 兼容面，但对 Runtime 的公开端口仍是 `execute(request, io)`。

### 8.2 native_loop

`agent/engine/native_loop/engine.py` 的 `NativeLoopAdapter` 直接实现公开端口，自己负责：

- canonical history/current input/附件编译。
- 唯一 current typed checkpoint 的严格恢复。
- provider stream 驱动与 awaited RuntimeIO event 提交。
- mandatory Tool Broker 调度。
- generation identity 和显式 final assistant。

Native 不经过 ADK `ReasoningEngine`、merge queue 或 authority route。默认 `native_early_tool_dispatch=off`：必须等完整 model finish 和 ToolCall batch 校验后才 PREPARE/派发工具，但模型正文和 Skill 进度仍流式提交。

## 9. RuntimeIO、最终消息与终态

RuntimeIO 提供：

- committed events，text delta 按时间/字节聚合，但必须先 commit 才能被 SSE 看到。
- checkpoint + engine-owned events 同事务提交。
- cancel/deadline probe。
- mandatory Tool Broker。
- `set_final_assistant(text, message_id, generation_id)`。

Native 的中间正文可能出现在“文本 -> 工具 -> 最终文本”链路中，因此累加 delta 不等于最终 Assistant。Native Adapter 只在最后完整、非空、无 ToolCall 的 turn 调用 final override。Coordinator 成功分支优先使用它；ADK 没有 override 时使用 Sink 累计文本。

Store 将以下内容在一个短事务里提交：

```text
ASSISTANT_MESSAGE_COMMITTED
+ 由 committed EvidenceSet 派生的 CITATION_SET_COMMITTED
+ Activity/Run 成功状态
+ RUN_TERMINATED
```

Conversation history 只会纳入这个成功 Assistant，不纳入失败 partial output。

## 10. 所有权丢失、取消和重试

stale fence、lease loss、checkpoint CAS 冲突等会统一转为 `AttemptOwnershipLost`。该异常不会变成 ToolResult 或 Run terminal；Worker 中止本地 attempt，由 durable recovery 安排下一个所有者。

Worker 同时运行 lease renewal task。续租返回 false 或续租异常时，它会立即 cancel 对应 attempt，不再让旧 Worker 继续调用 provider/工具。

Coordinator 最终还会按 DB cancel authority、绝对 deadline、未决 ToolEffect 覆盖 EngineOutcome；Engine/Adapter 自身无权提交 Run terminal。正常 EngineOutcome 由 Coordinator 收口，但 cancel API、deadline maintenance、lease recovery/reconciliation 等命令路径可以由 Store 在权威事务中直接提交 terminal。

## 11. SSE 与查询结果

`GET /api/v1/runs/{run_id}` 读 Run/Activity 当前权威状态。

`GET /api/v1/runs/{run_id}/events` 从 committed `run_events` replay/tail：

- 显式 `after_seq` 优先于 `Last-Event-ID`。
- `text_start` 开启一个 generation，UI 只清空回答正文，不清工具/Skill/计划过程卡片。
- `text` 是 generation-scoped delta。
- `assistant_message` 是最终回答权威覆盖。
- `terminal` 才是 SSE 终态事实；连接 EOF 不代表 Run 完成。

## 12. 建议的源码阅读顺序

1. `agent/main.py`：API lifespan 与 app.state。
2. `agent/runtime/api/runs.py`：HTTP DTO、CreateRun、SSE 投影。
3. `agent/runtime/application/admission.py`：digest 与 AdmissionCommand。
4. `agent/runtime/adapters/sqlite/store.py`：`admit()` 和 `claim_next()`。
5. `agent/runtime/worker/main.py`：ToolCatalog、release、Adapter 启动装配。
6. `agent/runtime/worker/dispatcher.py`：maintenance、claim、renewal、drain。
7. `agent/runtime/application/coordinator.py`：所有权、Adapter 调用和终态裁决。
8. `agent/runtime/adapters/adk_engines.py` 与 `agent/engine/native_loop/engine.py`：两条引擎适配路径。
9. `agent/runtime/application/events.py`：RuntimeIO、generation 和 final override。
10. `agent/runtime/adapters/sqlite/schema.sql`：完整 current schema 不变量。
