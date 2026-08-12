# SSE 流式输出端到端全链路指南

## 1. 一句话模型

```text
CreateRun durable admission -> exact-release Worker -> Adapter/RuntimeIO/Broker
-> committed Canonical Events -> API replay/tail SSE -> Web 或 eval 投影
```

SSE 不是 Worker 到浏览器的内存推送。Worker 先提交 `run_events`，API 再查询；断线、API 重启和 Worker 重启都不依赖进程内推送状态。

## 2. CreateRun、执行和订阅相互独立

`POST /api/v1/runs` 以 `Idempotency-Key` 做 durable admission，返回 `202` 不等待执行。该事务读取 active release 并冻结 fingerprint；Worker 只会凭完全相同的 `(engine, release_fingerprint)` claim。随后客户端可订阅：

```http
GET /api/v1/runs/{run_id}/events?after_seq=0
Accept: text/event-stream
```

取消是独立命令，SSE 断开不会取消 Run。

## 3. 两条引擎事件路径

`plan_execute` 和 `agent_loop` 使用 `AdkEngineAdapter`。每个 attempt 创建独立的 ADK session/artifact service，canonical history 由 committed events 重放；ADK 内部 transport 只属于这两个引擎。Broker 已拥有的工具投影只会先清空 text buffer，不会重复写 ToolCall/ToolResult；stream EOF 也必须有显式 `EngineOutcome` 才能成功。

`NativeLoopAdapter` 不使用 ADK 内部事件传输，直接调用 RuntimeIO。它的 pump 一次交付一项：

```text
pull provider/kernel event
-> await io.emit(event)
-> 才允许 pull 下一项
```

这保证 Runtime admission 的顺序和背压，但对 text 不等同每帧 DB commit。`CommittedEventSink` 按 100ms 或 2KiB 合并相同 message/generation 的 text；在身份切换、Tool、checkpoint、close 或终态边界前才清空缓冲并提交。Skill UI event 也走 awaited RuntimeIO，因此没有 Native 私有无界队列。

## 4. Native generation 与 final assistant

每个 model slot 开始时，Native 把 `MODEL_REQUEST` checkpoint 和 `OUTPUT_GENERATION_STARTED` 原子提交；SSE 名称为 `text_start`。payload 带 stable `message_id`、attempt `generation_id`、可选 supersedes generation 和 reason。

后续 `OUTPUT_DELTA_COMMITTED` 映射为 `text`，每个 payload 也带 message/generation identity。新 generation 不删除旧 event，而由客户端用 `text_start` 仅清空回答正文。最终一个完整、非空、没有 ToolCall 的 assistant turn 由 Native 的 `set_final_assistant()` 显式指定。

Coordinator 的成功事务原子写入：

```text
ASSISTANT_MESSAGE_COMMITTED
+ CITATION_SET_COMMITTED
+ success Run/Activity state
+ RUN_TERMINATED
```

`assistant_message` 以完整文本覆盖累积 delta，是最终语义权威；`RUN_TERMINATED` 是终态权威。

## 5. 工具事实和控制故障

Tool Broker 在 PREPARE transaction 中创建 ToolExecution/Tool Activity 和 `TOOL_CALL_COMMITTED`，在 settlement transaction 中提交 effect/result/ref、Activity 和 `TOOL_RESULT_COMMITTED`。Adapter 不得为同一 Broker 事实再写一次。

已 durable `DISPATCHED` 后，`AttemptOwnershipLost` 以及 ownership-coded `RuntimeFault` 必须原样向上穿透：它们不是模型可见 ToolResult，也不由旧 Worker 结算。其他 executor `RuntimeFault` 先按 effect class 结算（READ_ONLY `FAILED`，effectful `UNKNOWN`），再保留原错误码抛回 Coordinator。

普通失败若留下 unresolved effect，Store 固定 `pending_input.pending_terminal` 并进入严格 tool reconciliation；最后一个 effect 被 signal 处置后才提交原 `FAILED`，不重跑 Engine。只有 `TIMED_OUT` 可成为仍带 unresolved ToolEffect 的终态。

## 6. SSE 编码与消费

`agent/runtime/api/runs.py` 的主要映射：

| Canonical Event | SSE event |
|---|---|
| `OUTPUT_GENERATION_STARTED` | `text_start` |
| `OUTPUT_DELTA_COMMITTED` | `text` |
| `TOOL_CALL_COMMITTED` / `TOOL_RESULT_COMMITTED` | `tool_call` / `tool_result` |
| `SKILL_UI_FRAME_COMMITTED` | `skill_event` |
| `CITATION_SET_COMMITTED` | `citation` |
| `ASSISTANT_MESSAGE_COMMITTED` | `assistant_message` |
| `RUN_TERMINATED` | `terminal` |

每块包含 `id: <seq>`、event 名和 Canonical Event envelope。query `after_seq` 优先于 `Last-Event-ID`；visibility 过滤可以造成 seq 跳号。API 从 committed events 按 cursor 短查询，读到 `RUN_TERMINATED` 后结束；终态但 cursor 后无事件也结束。

`web/app.js` 和 `eval/harness/sse_client.py` 都按同一规则：`text_start` 清回答正文、`text` 追加、`assistant_message` 覆盖、`terminal` 才结束。fresh replay 与断线续传因此不会把多个 generation 的 partial 文本拼成最终回答。

## 7. 阅读索引

1. `agent/runtime/api/runs.py`
2. `agent/runtime/application/events.py`
3. `agent/runtime/adapters/adk_engines.py`
4. `agent/engine/native_loop/engine.py`
5. `agent/runtime/application/tool_broker.py`
6. `agent/runtime/application/coordinator.py`
7. `web/app.js`、`eval/harness/sse_client.py`
