# SSE 流式输出端到端全链路指南

本文从 CreateRun 一直跟到 Web UI/eval 收到终态，重点说明当前 Native direct RuntimeIO、ADK-only queue、Tool Broker 事件权威、generation 替换与 committed replay。

## 1. 一句话架构

```text
Client CreateRun
  → Runtime durable admission
  → Worker exact-release claim
  → EngineAdapter + RuntimeIO / Tool Broker
  → SQLite committed Canonical Events
  → API replay/tail SSE
  → Web UI / eval 按 seq 投影
```

SSE 不是 Worker 向浏览器的直连推送队列。API 和 Worker 通过 `runtime.db` 解耦；事件必须先 commit，然后才能被 SSE 查询看到。

## 2. 端到端时序

```text
Web/Eval          Runtime API        Runtime DB          Worker/Adapter       Provider/Tool
  │ POST /runs      │                  │                    │                  │
  ├─────────────▶│ admission tx     │                    │                  │
  │                 ├─────────────────▶│ Run/Activity/events│                  │
  │ 202 + run_id    │                  │                    │                  │
  │◀──────────────────│                  │                    │                  │
  │ GET events      │                  │ exact claim tx     │                  │
  ├─────────────▶│ list_events      │◀──────────────────┤                  │
  │◀─ committed SSE ─┤◀─────────────────┤                    │                  │
  │                 │                  │                    ├─ request/stream ─▶│
  │                 │                  │◀─ events/checkpoint ┤◀─ chunks ────────┤
  │◀─ text_start/text─┤◀─ polling sees commit                 │                  │
  │                 │                  │◀─ Broker ledger/events ─┤─ tool execution ───▶│
  │◀─ tool/process ──┤◀─ polling sees commit                 │                  │
  │◀─ assistant_message + citation + terminal (committed order)                  │
```

## 3. CreateRun 与订阅是两条独立命令

`POST /api/v1/runs` 完成 durable accepted 后返回 Run，不等待模型。请求使用 `Idempotency-Key`，相同范围与请求 digest 可重放；digest 不同则冲突。

之后客户端订阅：

```http
GET /api/v1/runs/{run_id}/events?after_seq=0
Accept: text/event-stream
```

SSE 断开不会取消 Run。取消是独立 `POST /runs/{run_id}/cancel` 命令。

## 4. Worker 内部的两种 Adapter

### 4.1 ADK 两引擎

`plan_execute` 和 `agent_loop` 使用 `AdkEngineAdapter`。这条路径保留 ADK 内部 queue/merge，但每个 attempt 使用独立的临时 ADK session/artifact service。跨 attempt history 只从 Canonical Events + Checkpoint 编译。

Adapter 消费合并后的事件：

```text
普通 engine event → await RuntimeIO.emit
Broker 已提交的 tool projection → force_flush 后跳过
```

不允许根据 generator EOF 猜成功；必须有显式 EngineOutcome。

### 4.2 Native 引擎

`NativeLoopAdapter` 直接实现 `execute(request, RuntimeIO)`，不使用 ADK queue/merge。每个 kernel event 都逐个 await 提交；text 后又立即 `force_flush()`。

```text
provider/kernel event
  → await io.emit
  → text: await io.force_flush
  → cancel/deadline probe
  → 才拉下一个 event
```

这是 Native 的背压与提交屏障。Skill UI 帧也使用直接 awaited sink，所以进度可实时提交，却不会在 Runtime 慢时无界堆积。

## 5. Native generation 的完整流程

默认 `native_early_tool_dispatch=off`，一轮模型的 durable 顺序是：

```text
MODEL_REQUEST checkpoint + OUTPUT_GENERATION_STARTED
  → provider stream + committed OUTPUT_DELTA
  → explicit finish marker
  → MODEL_RESPONSE_COMMITTED checkpoint
  → 无工具：COMPLETED checkpoint + final override
  → 有工具：Broker PREPARE batch
  → TOOL_BATCH_COMMITTED checkpoint
  → dispatch / ordered Broker settlement
  → TOOL_RESULT_COMMITTED checkpoint(s)
  → NEXT_TURN checkpoint
```

Provider 必须给出单 choice 且显式 `stop` 或 `tool_calls`。零 chunk、usage-only、silent EOF、finish 矛盾等不能合成一个成功 TurnEnd。

### 5.1 `OUTPUT_GENERATION_STARTED` / `text_start`

Native 在发起 provider 请求前，把 generation 开始事件和 `MODEL_REQUEST` checkpoint 原子提交。payload 包含：

```json
{
  "message_id": "stable-model-slot",
  "generation_id": "attempt-generation",
  "supersedes_generation_id": null,
  "reason": "initial|next_turn|retry|recovery|reactive_compact"
}
```

current checkpoint codec 接受上表五个值；当前生产 producer 实际产生 `initial`、`next_turn`、`recovery`、`reactive_compact`。Coordinator 重试恢复 `MODEL_REQUEST` 时使用 `recovery`，目前没有单独发出 `retry` 的分支。

API 把它映射为 `text_start`。新 generation 不删除旧 events，而是在投影端替换当前回答正文。

### 5.2 `OUTPUT_DELTA_COMMITTED` / `text`

Native text payload 带 `delta + message_id + generation_id`。`CommittedEventSink` 确保 buffer 不跨身份聚合；Native Adapter 的显式 flush 使当前帧先 commit，然后 provider 才能继续。

### 5.3 `ASSISTANT_MESSAGE_COMMITTED` / `assistant_message`

Native 只有最后一个完整、非空、无 ToolCall 的 Assistant turn 通过 `set_final_assistant()` 成为 final override。Coordinator 成功收口时，Store 在一个事务中提交：

- `ASSISTANT_MESSAGE_COMMITTED`；
- 从 committed Evidence 派生的 `CITATION_SET_COMMITTED`；
- success terminal 状态及 `RUN_TERMINATED`。

`assistant_message` 是最终语义权威，客户端应用其完整 text 覆盖已拼接 delta，不是追加。

## 6. ToolCall/ToolResult 为什么不由 Adapter 重复 emit

正常工具事实由 Tool Broker/Store 拥有：

```text
prepare_batch transaction
  → ToolExecution(PREPARED)
  → Tool Activity(PENDING)
  → TOOL_CALL_COMMITTED

settlement transaction
  → ToolExecution effect/result/ref
  → Tool Activity state
  → Artifact metadata/link (如需)
  → TOOL_RESULT_COMMITTED
```

Adapter 只需确保工具事实前的 text 已 flush。Native 的 MODEL_RESPONSE checkpoint 和逐帧 flush 提供该屏障；ADK Broker wrapper 在批次 PREPARE 前显式 flush。

只有没有外部 dispatch/ToolExecution 的模型可修正错误，才由 Engine 提交 synthetic call/result 对。

## 7. API SSE 编码

`agent/runtime/api/runs.py` 将 Canonical EventType 映射为客户端 event name：

| Canonical EventType | SSE event |
|---|---|
| `USER_MESSAGE_COMMITTED` | `user_message` |
| `OUTPUT_GENERATION_STARTED` | `text_start` |
| `OUTPUT_DELTA_COMMITTED` | `text` |
| `TOOL_CALL_COMMITTED` | `tool_call` |
| `TOOL_RESULT_COMMITTED` | `tool_result` |
| `MODEL_PLAN_UPDATED` | `plan_step` |
| `SKILL_UI_FRAME_COMMITTED` | `skill_event` |
| `CITATION_SET_COMMITTED` | `citation` |
| `ASSISTANT_MESSAGE_COMMITTED` | `assistant_message` |
| `RUN_STATUS_CHANGED` | `run_status` |
| `ACTIVITY_STATUS_CHANGED` | `activity_status` |
| `RUN_TERMINATED` | `terminal` |

每个业务 event block 包含：

```text
id: <run seq>
event: <projection name>
data: <Canonical Event envelope JSON>
```

envelope 保留 event_id/run_id/activity/tool_execution/seq/payload/terminal/release 等字段。

## 8. replay、tail 与 heartbeat

SSE generator 以 cursor 循环：

```text
list_events(after_seq=cursor, limit=500)
  → 按 seq yield
  → 看到 RUN_TERMINATED 则 return
  → Run 已终态且无新事件则 return
  → 无事件超过 heartbeat 间隔则 yield comment
  → 等待 poll interval 后重复
```

cursor 选择规则：query `after_seq` 优先，否则使用 `Last-Event-ID`，都没有则从 0 开始。

heartbeat 是 `: heartbeat\n\n` SSE comment，无 id、无 seq、不入库，不改变 Run 状态。

## 9. Web UI 如何消费

`web/app.js` 使用 `fetch()` + `ReadableStream` 手工解析 SSE，以便精确控制 cursor 和重连。

`handleSseEvent()` 的重要规则：

- 先用 event `id` 单调更新 `lastSeq`；
- `text_start`：只清空 assistant body，保留过程卡片；
- `text`：追加 delta；
- `assistant_message`：用完整 text 覆盖 body；
- tool/result/plan/skill/status：追加 process item；
- `terminal`：停止 watch loop，显示权威 terminal status。

同一页面内断线使用 `lastSeq` 续传。但刷新后 DOM 不是 durable projection，所以 `resumeStoredRun()` 会把投影 cursor 设为 0，从所有 committed public events 重建。

## 10. Eval harness 的对等语义

`eval/harness/sse_client.py` 在连接失败后以 `after_seq=last_seq` 和 `Last-Event-ID=last_seq` 重连。它与 Web UI 保持同样的 generation 规则：

- `text_start` 清空 `run.text`，不清 tool/skill/plan 集合；
- `text` 记录 TTFT 并追加；
- `assistant_message` 权威覆盖 `run.text`；
- `terminal` 才标记完成并读 terminal status。

因此 fresh replay、断线续传和实时 tail 的最终回答语义一致。

## 11. 关键不变量

1. 未 commit 的事件不得对 SSE 可见。
2. text 不得跨 message/generation 聚合。
3. Native 的前一个 RuntimeIO 提交未完成时，不得拉取下一 provider 事件。
4. Broker-owned tool facts 不得由 Adapter 再写一次。
5. `assistant_message` 是最终文本权威；`RUN_TERMINATED` 是终态权威。
6. heartbeat 不是 Canonical Event，不进度 cursor。
7. SSE 断开不得取消 Run。

## 12. 建议的源码阅读顺序

1. `agent/runtime/api/runs.py`：SSE 名称投影、cursor、tail、heartbeat。
2. `agent/runtime/application/events.py`：RuntimeIO 事件提交和 text 缓冲。
3. `agent/runtime/adapters/adk_engines.py`：ADK-only 事件消费。
4. `agent/engine/native_loop/engine.py`：Native direct awaited sink。
5. `agent/runtime/application/tool_broker.py` 与 `agent/runtime/adapters/sqlite/store.py`：Tool Broker 权威事务。
6. `web/app.js`：浏览器投影与 fresh replay。
7. `eval/harness/sse_client.py`：评测客户端的对等语义。
