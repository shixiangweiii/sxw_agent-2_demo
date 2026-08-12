# Query 到 Answer 全链路代码阅读指南

本文按当前代码梳理一次 Query 从浏览器进入 Runtime、被 Worker 执行、调用模型/工具、通过 SSE 展示并最终写入 Conversation history 的完整路径。

当前公开 Engine 端口只有：

```python
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

三个引擎分为两条实现路径：

```text
plan_execute ─┐
              ├─ AdkEngineAdapter → ADK ReasoningEngine
agent_loop ───┘

native_loop ─── NativeLoopAdapter → Native Runtime-independent kernel
```

Native 不再经过旧的通用 Adapter、`RunContext.engine_outcome` 或后台 runner merge queue。文中按符号名定位代码，不依赖易漂移的行号。

## 1. 整体架构与事实源

```text
Browser / API client
  │
  ├─ POST /api/v1/artifacts
  ├─ POST /api/v1/runs
  └─ GET  /api/v1/runs/{run_id}/events
          │
          ▼
Runtime API (:8000)
  ├─ admission/status/cancel/signal/SSE
  └─ 不加载 LLM 和远程工具目录
          │
          ▼
runtime.db + Artifact CAS
          ▲
          │
Runtime Worker
  ├─ exact-release claim + lease/fencing
  ├─ RunCoordinator
  ├─ EngineAdapter
  ├─ Tool Broker / Artifact
  └─ ARAG / Skill Center / A2A
```

辅助服务：

- ARAG：`:8100`
- Skill Center：`:8200`
- A2A：`:8300`

当前 API 与 Worker 共享本机 SQLite/Artifact，因此实现的是本机多进程恢复，不是跨主机 HA。

### 1.1 权威状态

| 问题 | Authority | 不能作为 Authority 的东西 |
|---|---|---|
| CreateRun 幂等 | `run_requests` | HTTP 是否断开 |
| Run 状态/终态 | `runs` + `RUN_TERMINATED` | Engine 内部 event、SSE EOF |
| Activity 执行权 | lease/revision/fencing | 某个 Python task 仍在运行 |
| Conversation history | committed USER + 成功 ASSISTANT event | partial delta、attempt session |
| checkpoint | append-only checkpoints + revision CAS | Trace |
| Tool effect | `tool_executions` | timeout 推测、args hash |
| Artifact 字节 | SHA-256 CAS | preview、路径名 |
| Evidence | committed strict EvidenceSet | UI citation 序号 |
| release | immutable manifest + active pointer | 当前进程临时配置 |

## 2. Worker 启动：先冻结 current 世界

理解 Query 前，先看 `agent/runtime/worker/main.py::build_worker`。只有 Worker 会计算并原子切换 current release 的 active pointer；Admission 只是读取持久化 pointer，并不会校验是否存在新鲜 Worker heartbeat。因此重启间隙可能冻结到库中保留的 pointer，Run 会等待 exact-release Worker，最迟由绝对 deadline 收口。

### 2.1 current schema

Runtime 和 ARAG 各有一份唯一 current `schema.sql`。`common/sqlite_schema.py::ensure_current_schema` 执行：

```text
空库
→ BEGIN IMMEDIATE
→ 一次性创建完整 schema
→ schema_meta(id, schema_digest, created_at)
→ COMMIT

非空库
→ 计算完整 schema.sql 原始字节 SHA-256
→ 必须与 schema_meta.schema_digest 完全一致
→ 否则 CURRENT_SCHEMA_MISMATCH
```

没有 migration、`ALTER TABLE`、upgrader、shadow read/write 或旧 checkpoint codec。schema 变化后由用户显式删库重建。

### 2.2 strict ToolCatalog

Worker 加载 builtin、远程 Skill、本地 Claude Skill、A2A 与 `read_artifact` 后：

1. 收集 `native_loop` 和 `agent_loop` 的工具面；
2. 校验 name、description、normalized schema、effect/result policy parity；
3. 构造唯一 `ToolCatalog`；
4. 严格注册 `ToolBroker`；
5. catalog digest 进入 release identity。

重复名称、空 schema fallback、非法 Draft 2020-12 object schema、畸形成功目录、缺 executor/result adapter 都会阻止启动。远程目录连接失败可以 best-effort 为空；成功响应则必须全量有效。

### 2.3 三份 release 原子激活

`ReleaseManifest` 当前只包含 `engine + components`。components 覆盖 schema/source/catalog/provider/checkpoint/语义配置/资源上限和真实安装依赖版本。

`store.activate_current_releases(...)` 必须一次传入：

```text
plan_execute + agent_loop + native_loop
```

在一个 `BEGIN IMMEDIATE` 中写入或核对 immutable manifests、检查是否有异 fingerprint 非终态 Run、再同时切换三个 active pointer。中间任一失败会全部回滚。

## 3. 阶段一：HTTP CreateRun

入口：`agent/runtime/api/runs.py::create_run`

```http
POST /api/v1/runs
Idempotency-Key: <required>
Content-Type: application/json

{
  "client_request_id": "...uuid...",
  "conversation_id": null,
  "principal_id": "demo-user",
  "agent_id": "demo-agent",
  "engine": "native_loop",
  "input": {
    "text": "什么是 Tool Broker？",
    "attachment_refs": []
  }
}
```

API 只做 schema/身份/附件/admission，不加载 LLM。

### 3.1 Admission 事务顺序

`AdmissionService.create` 最终调用 Store admission 事务：

1. 按 `(principal_id, agent_id, idempotency_key)` 查 `run_requests`；
2. 已存在且 request digest 相同：返回原 Run；
3. 已存在但 digest 不同：`IDEMPOTENCY_KEY_REUSE`；
4. 读取所选 Engine 的 active release pointer；
5. 校验 conversation busy、附件 metadata；
6. 创建/推进 Conversation、Run、ENGINE_RUN Activity；
7. 写 `run_requests`、Artifact links；
8. 原子追加 USER/Run/Activity canonical events；
9. 返回 `202 Accepted`。

幂等重放故意先于 conversation busy 检查。因此客户端超时重发同一请求时，仍能拿回原 `run_id`。

Run 在 admission 时冻结：

- engine；
- release fingerprint；
- absolute deadline；
- input/attachment refs；
- principal/agent/conversation identity。

## 4. 阶段二：Worker exact-release claim

入口：`agent/runtime/worker/dispatcher.py::RuntimeWorker.run`

Worker 周期执行 maintenance，然后调用：

```python
store.claim_next(
    worker_id=...,
    lease_ms=...,
    now_ms=...,
    release_map={
        "plan_execute": "...",
        "agent_loop": "...",
        "native_loop": "...",
    },
)
```

Claim SQL 同时匹配 `(engine, release_fingerprint)`，并在写事务中：

- 选择一个可执行 PENDING Activity；
- `PENDING → CLAIMED`；
- attempt + 1；
- fencing token + 1；
- 设置 lease owner/expiry；
- 普通 Run 从 `DISPATCH_PENDING → RUNNING`；
- 追加状态事件。

错误 release 的 Worker 根本领取不到该 Run，也不存在单独的“release 不兼容”终态：Run 继续 pending，等待正确 Worker或 absolute deadline 收口。

### 4.1 lease renewal

每个 claim 同时启动 attempt task 与 renewal task。续租失败会立即取消 attempt，并以 `AttemptOwnershipLost` 语义收口；不会把 Run 错判为 Engine failure。

所有后续 Store 写都携带 fencing token。旧 Worker 即使在网络暂停后醒来，其 event/checkpoint/tool result/final 写入也会被拒绝。

## 5. 阶段三：RunCoordinator 建立统一边界

入口：`agent/runtime/application/coordinator.py::RunCoordinator.execute_claim`

Coordinator 的核心顺序：

1. `CLAIMED → RUNNING`，再次验证 fence；
2. 重读最新 Run，处理 cancel/deadline/reconcile-only 分支；
3. 从 `EngineRegistry` 选择 Adapter；
4. 防御性核对 adapter release；
5. 读取最新 checkpoint；
6. 从 committed events 编译 canonical conversation history；
7. 构造 `EngineRunRequest`；
8. 构造 `CommittedEventSink`，作为 `RuntimeIO`；
9. 调用统一端口 `adapter.execute(request, io)`；
10. 根据 `EngineOutcome` 与数据库权威状态裁决 retry/wait/final。

`CLAIM_RELEASE_MISMATCH` 是理论不可达的防御断言。若发生，只中止 attempt 并报警，不产生一个虚假的 release 不兼容 Run 终态。

### 5.1 RuntimeIO 提供什么

`RuntimeIO` 是 Adapter 写 Runtime authority 的唯一端口：

- `emit(...)`
- `force_flush()`
- `checkpoint(..., events=...)`
- `is_cancelled()`
- `remaining_ms()`
- mandatory `tool_broker`
- `set_final_assistant(text, message_id, generation_id)`

text delta 按约 100ms/2KiB 聚合；切换 generation、Tool、checkpoint、terminal 前会先 flush。所有公开输出都是先 commit、后对 SSE 可见。

## 6. 阶段四：两个 ADK Adapter

`plan_execute` 与 `agent_loop` 进入 `agent/runtime/adapters/adk_engines.py::AdkEngineAdapter`。

每次 attempt 都新建 ADK `InMemorySessionService` 和 `InMemoryArtifactService`，把 canonical history 重放进去；attempt 结束即丢弃。跨 attempt 不依赖进程内 session。

```text
AdkEngineAdapter.execute
→ 编译 history/current input/附件为 ADK Content
→ 构造 RunContext
→ build_engine(plan_execute | agent_loop)
→ async for engine.run_stream(...)
→ engine-owned event 交给 RuntimeIO
→ Broker-owned tool projection 只 force_flush，不重复提交
→ 必须读取显式 rc.engine_outcome
```

生成器自然 EOF 不等于成功；`engine_outcome` 缺失会以 `ENGINE_OUTCOME_MISSING` fail-closed。

两个 ADK 引擎保持原有内部循环和粗粒度恢复语义，不宣称 mid-turn deterministic replay。

## 7. 阶段五：NativeLoopAdapter 直连 RuntimeIO

入口：`agent/engine/native_loop/engine.py::NativeLoopAdapter`

### 7.1 输入与严格恢复

Adapter 负责：

- canonical history/current user input；
- 图片从验证后的 CAS 分片物化；
- 非图片附件只给有界 preview，并提示 `read_artifact`；
- 当前 strict checkpoint decode；
- 所有历史大 ToolResult ledger ref 重物化；
- mandatory Broker session；
- RuntimeIO event/checkpoint/final assistant。

只有 `checkpoint is None` 才是新运行。checkpoint 存在但 contract、字段、phase、message role、call/result 配对或尾部状态不合法时，直接 `NATIVE_CHECKPOINT_INVALID`，不 fallback 到 canonical history 重跑。

current phase 只有：

```text
MODEL_REQUEST
MODEL_RESPONSE_COMMITTED
TOOL_BATCH_COMMITTED
TOOL_RESULT_COMMITTED
NEXT_TURN
COMPLETED
```

### 7.2 一次无工具的模型轮次

```text
control/deadline/budget/compact
→ MODEL_REQUEST checkpoint + OUTPUT_GENERATION_STARTED
→ provider stream
→ 每个 delta await RuntimeIO.emit/flush 后才拉下一块
→ 明确 finish=stop
→ MODEL_RESPONSE_COMMITTED checkpoint
→ 校验最终正文非空且无 ToolCall
→ COMPLETED checkpoint
→ RuntimeIO.set_final_assistant
→ EngineOutcome(COMPLETED)
```

Provider 必须给出显式完整 finish marker。零 chunk、usage-only、silent EOF、缺 finish、finish 后继续 choice、多 choice、id/name 漂移、非连续 tool index 都不会被合成为成功。

- 不完整网络流：`MODEL_STREAM_INCOMPLETE`，可重试；
- 协议矛盾：`MODEL_PROTOCOL_INVALID`，终端失败；
- 空最终正文：`MODEL_EMPTY_FINAL_RESPONSE`；
- `length/content_filter/unknown finish`：fail-closed。

### 7.3 generation 与 partial output

每次 model slot 开始先原子提交 `OUTPUT_GENERATION_STARTED`，SSE 名为 `text_start`：

```json
{
  "message_id": "稳定 model slot",
  "generation_id": "本次 generation",
  "supersedes_generation_id": null,
  "reason": "initial|next_turn|retry|recovery|reactive_compact"
}
```

codec 接受上述五个 reason；当前 producer 实际发出 `initial`、`next_turn`、`recovery`、`reactive_compact`。Coordinator 级 retry 恢复到 `MODEL_REQUEST` 时归类为 `recovery`，当前没有单独发出 `retry` 的代码分支。

所有 Native text delta 带 message/generation id。重试或恢复创建新 generation；旧 partial event 保留审计，但 UI 收到 `text_start` 时只重置当前回答正文，不清空 Tool/Skill/Plan 卡片。最终 committed `assistant_message` 是权威覆盖。

### 7.4 工具轮次：默认 early dispatch 关闭

默认 `native_early_tool_dispatch=off`：

```text
完整 provider stream + finish=tool_calls
→ 严格验证整个 ToolCall batch
→ MODEL_RESPONSE_COMMITTED checkpoint
→ Broker 原子 PREPARE 整批 stable slots + ToolCall events
→ TOOL_BATCH_COMMITTED checkpoint
→ 安全 READ_ONLY 在上限内并发执行
→ durable settlement 按 call ordinal
→ 每个结果后 TOOL_RESULT_COMMITTED checkpoint
→ NEXT_TURN
→ 下一次 model request
```

模型 finish 前不会创建工具执行 task。关闭提前派发不影响：

- 模型正文流式输出；
- 完整 batch 后 READ_ONLY 受控并发；
- 工具运行期间 Skill/Claude Skill 进度实时提交；
- ToolResult 完成后一次性权威提交。

`experimental_heuristic` 仍可显式启用，但不是默认生产保证。它只允许安全 READ_ONLY，并使用有界队列/固定 worker；每个 early call 也必须先 Broker PREPARE。fragment 后续漂移会 `TOOL_REPLAY_MISMATCH`。

`provider_block_complete` 当前 provider 不支持，Worker 会在 release 激活前以 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE` 启动失败。

### 7.5 Native 资源上限

默认硬限：

| 项目 | 默认值 |
|---|---:|
| 工具并发 | 10 |
| ToolCall/turn | 64 |
| ToolCall/Run | 256 |
| 单调用 args | 64 KiB |
| 单 batch args | 256 KiB |
| model output/generation | 1 MiB |
| checkpoint | 2 MiB |
| ToolCatalog | 1 MiB |
| Skill UI frame | 64 KiB |
| Skill UI events/Run | 2000 |
| Skill UI bytes/Run | 8 MiB |

尺寸统一按 UTF-8 bytes。模型调用硬上限为 `max_loop_iters + 2`；`MODEL_REQUEST` 前预留调用次数，崩溃后不退款。

## 8. 阶段六：ToolBroker 接触外部世界

ToolBroker 的完整说明见专门指南。全链路只需抓住：

```text
ToolCall intent
→ stable logical slot(turn + call ordinal)
→ PREPARED + TOOL_CALL_COMMITTED
→ effect-aware external dispatch
→ strict ToolExecutionOutput
→ Artifact/Evidence when needed
→ ToolExecution settlement + TOOL_RESULT_COMMITTED
→ ToolResultEnvelope back to Engine
```

同一 slot 的 name/request/release/effect 漂移会 `TOOL_REPLAY_MISMATCH`。READ_ONLY 可安全重试；幂等副作用必须透传稳定 idempotency key；非幂等/未知副作用不透明重试，结果不明则 reconcile/manual。

普通大结果由 Broker 写入 Artifact，ledger 只保留有界 preview/ref；Native 恢复用 `materialize_committed_result` 恢复完整 envelope，不重复外部 effect。

## 9. 阶段七：Coordinator 裁决 EngineOutcome

Adapter 只返回候选 outcome，Coordinator 还会再次读取 cancel、deadline 和 unresolved tool effects。

| EngineOutcome | Coordinator 行为 |
|---|---|
| `COMPLETED` 且无 unresolved effect | 原子提交 final assistant、citation、SUCCEEDED terminal |
| `COMPLETED` 但 effect 不明 | `WAITING_INPUT`，要求 tool reconciliation |
| `WAITING_INPUT` | 持久化 pending input，释放 Worker slot |
| `RETRYABLE_FAILURE` 且未超次数 | `WAITING_RETRY` + durable timer/backoff |
| `CANCELLED` | 在取消/效应规则允许时提交 CANCELLED |
| terminal failure | FAILED；deadline 映射 TIMED_OUT |

Native 通过 `set_final_assistant` 指定最后一个完整、非空、无 ToolCall assistant turn及其 generation identity。ADK 没有 override 时，Coordinator 继续使用 sink 累积文本。

成功事务同时写：

```text
ASSISTANT_MESSAGE_COMMITTED
CITATION_SET_COMMITTED
RUN_TERMINATED(SUCCEEDED)
```

并推进 Run/Activity。Conversation history 只从过去 Run 的 committed USER 和成功 ASSISTANT event 编译；中间“我先查一下”、失败 partial、工具前正文都不会成为下一轮 assistant history。

若崩溃发生在 Native `COMPLETED` checkpoint 与成功终态之间，恢复会从 checkpoint 精确设置 final assistant，不再调用模型，也不重放 delta。

## 10. 阶段八：SSE replay/tail

入口：`agent/runtime/api/runs.py::stream_events`

客户端可以用：

```http
GET /api/v1/runs/{run_id}/events?after_seq=17
Last-Event-ID: 17
```

显式 `after_seq` 优先。服务端循环短查询 `run_events`，按 seq 输出；无事件时发送无 seq 的 heartbeat。连接在 `RUN_TERMINATED` 后结束。

主要映射：

| Canonical Event | SSE name |
|---|---|
| `OUTPUT_GENERATION_STARTED` | `text_start` |
| `OUTPUT_DELTA_COMMITTED` | `text` |
| `TOOL_CALL_COMMITTED` | `tool_call` |
| `TOOL_RESULT_COMMITTED` | `tool_result` |
| `MODEL_PLAN_UPDATED` | `plan_step` |
| `SKILL_UI_FRAME_COMMITTED` | `skill_event` |
| `CITATION_SET_COMMITTED` | `citation` |
| `ASSISTANT_MESSAGE_COMMITTED` | `assistant_message` |
| `RUN_TERMINATED` | `terminal` |

公开 visibility 过滤可能造成 seq 跳号，这是正常的。heartbeat 没有 seq。订阅断开不会取消 Run，cancel 必须走独立命令。

### 10.1 前端 generation 处理

`web/app.js` 的核心规则：

- `text_start`：清空当前回答正文；保留工具、Skill、Plan 过程展示；
- `text`：只追加当前 generation delta；
- `assistant_message`：以 committed final text 权威覆盖；
- `terminal`：根据 Runtime 终态结束 UI 状态。

`eval/harness/sse_client.py` 使用相同规则，所以 fresh replay、断线续传、retry/recovery 不会把多个 generation partial 拼成最终答案。

## 11. Cancel、deadline、retry 与恢复

### 11.1 absolute deadline

deadline 是 Run admission 时冻结的绝对 UTC 时间。Coordinator、Adapter、provider、Tool 都计算剩余预算，不在每层重开完整 timeout。

### 11.2 cancel 与 effect

cancel/complete 由提交顺序决定：

- cancel 先提交，迟到 success 不能覆盖；
- 已派发/未知外部 effect 时进入 `CANCEL_REQUESTED`，先 reconcile；
- 没有 unresolved effect 才能完成 CANCELLED；
- deadline 先赢则 TIMED_OUT。

### 11.3 Native 恢复

Native 从最后 committed current checkpoint 恢复：

- 半个 model stream：新 generation 重做 model slot；
- Tool batch：按 stable slot 查 ledger；
- 已 COMMITTED effect：复用结果/Artifact，不重派发；
- 历史任意位置的大 ToolResult ref：逐项重物化；
- COMPLETED：直接恢复 final assistant。

它保证 durable 边界的语义等价，不承诺 provider token 级 deterministic replay。

## 12. 端到端简化时序图

```text
Client          Runtime API       SQLite            Worker/Coordinator       Native/Broker
  │                  │               │                       │                     │
  │ POST /runs       │               │                       │                     │
  ├─────────────────>│ admit txn     │                       │                     │
  │                  ├──────────────>│ Run+Activity+Events   │                     │
  │<──── 202 run_id ─┤               │                       │                     │
  │                  │               │<── exact claim ───────┤                     │
  │                  │               │── Claim+fence ───────>│                     │
  │ GET events       │               │                       │                     │
  ├─────────────────>│ replay/tail   │                       │                     │
  │                  ├──────────────>│                       │                     │
  │                  │               │                       │ execute(request,io) │
  │                  │               │                       ├────────────────────>│
  │                  │               │<── text_start/delta checkpoint commits ────│
  │<── text_start/text via SSE ──────┤                       │                     │
  │                  │               │<── PREPARED+ToolCall ──────────────────────│
  │<── tool_call ────┤               │                       │                     │
  │                  │               │<── settle+ToolResult/Artifact ─────────────│
  │<── tool_result ──┤               │                       │                     │
  │                  │               │<── COMPLETED checkpoint ──────────────────│
  │                  │               │<── assistant+citation+terminal txn ─┤     │
  │<── assistant_message + terminal ─┤                       │                     │
```

## 13. 推荐阅读顺序

### 13.1 先看 Runtime 主干

```text
agent/runtime/api/runs.py
→ agent/runtime/application/admission.py
→ agent/runtime/adapters/sqlite/store.py
→ agent/runtime/worker/dispatcher.py
→ agent/runtime/application/coordinator.py
→ agent/runtime/ports/engine.py
→ agent/runtime/application/events.py
```

### 13.2 再分引擎

```text
ADK:
agent/runtime/adapters/adk_engines.py
→ agent/engine/base.py
→ plan_execute / agent_loop 实现

Native:
agent/engine/native_loop/engine.py
→ agent/engine/native_loop/checkpoint.py
→ agent/engine/native_loop/llm_client.py
→ agent/engine/native_loop/loop.py
→ agent/engine/native_loop/executor.py
```

### 13.3 最后看工具与交付

```text
agent/runtime/application/tool_catalog.py
→ agent/runtime/application/tool_outputs.py
→ agent/runtime/application/tool_broker.py
→ agent/runtime/adapters/brokered_tools.py
→ agent/runtime/api/runs.py::stream_events
→ web/app.js
→ eval/harness/sse_client.py
```

## 14. 阅读时最容易混淆的边界

1. `RuntimeIO` 是进入 Runtime authority 的唯一写路径，但 text 可先进入有界聚合 buffer；只有 Store commit 后才对 SSE 可见。Native 每个 delta 都会 awaited emit 后再 force-flush，形成逐块背压。
2. Native kernel 可被 Claude Skill 子 Runner 复用，但生产持久化语义在 `NativeLoopAdapter`，不能拿裸 kernel 结果推导 Runtime 终态。
3. ToolCall/ToolResult event 是公开投影，工具是否真正发生由 ToolExecution ledger 裁决。
4. `OUTPUT_DELTA_COMMITTED` 是过程输出；只有成功的 `ASSISTANT_MESSAGE_COMMITTED` 才进入后续 conversation history。
5. SSE EOF 不是业务终态；客户端必须看 `terminal` 或 GET Run status。
6. Trace 关闭后恢复逻辑必须完全不受影响，因为 Trace 从来不是 authority。

## 15. 当前诚实边界

- SQLite + 本机 Artifact CAS 只支持单机进程级恢复；
- 两个 ADK 引擎没有 mid-turn deterministic replay；
- Native 也不承诺 provider token 级重放；
- 当前没有 PostgreSQL/Temporal/Redis、跨节点 Artifact、服务端 delivery ACK；
- LocalSandbox 不是生产安全隔离；
- Runtime signal/Artifact 的存在不代表所有子 Runner 已自动支持 HITL 或跨 Skill Artifact。

这些限制不改变当前代码的核心不变量：单一事实源、exact release claim、fencing、commit-before-visible、Broker effect authority、strict current checkpoint，以及所有终态都必须由 Coordinator 的正常 EngineOutcome 路径或 Store 的权威命令事务提交。
