# 事件持久化、SSE 可见性与 Trace 记录的先后关系

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

## 1. 核心规则

```text
Engine/Tool/Skill 产生候选事件
        -> RuntimeIO 或 Broker 写 SQLite 事务
        -> Canonical Event committed
        -> API 短查询读到事件
        -> SSE 可见

Trace：诊断旁路，不参与上述事务或恢复裁决
```

SSE 从不直接消费模型流、ADK transport、Native pump 或 Trace。`run_events` 是公开 event 的唯一事实源；Trace 不是 event、checkpoint、ToolEffect、Run terminal 或 conversation history authority。

## 2. RuntimeIO 的文本分层

`agent/runtime/application/events.py::CommittedEventSink` 是三个 EngineAdapter 写 Runtime authority 的共同出口。

- `emit(text)` 把 delta 放入按 `message_id`/`generation_id` 隔离的内存 buffer；
- 同一身份文本到 100ms 或 2KiB 时，写 `OUTPUT_DELTA_COMMITTED`；
- message/generation 切换、非文本 `emit`、Tool、checkpoint、`close()` 与终态相关边界会先 flush；
- `append_events`/`save_checkpoint` 成功后，API 才可能读到它。

NativeLoopAdapter 对 stream pump 的约束是：每一个 provider/kernel event 都 `await io.emit` 完成才拉下一项。这是 admission 顺序与背压，不是“每个 delta 都立即耐久提交”。ADK 内部事件传输只属于两个 ADK 引擎；Native 不走该路径。

## 3. 原子边界

| 业务动作 | 同一耐久事务的关键事实 |
|---|---|
| admission | Run/Activity/idempotency/Artifact links + USER/状态 events |
| Native model slot 开始 | `MODEL_REQUEST` checkpoint + `OUTPUT_GENERATION_STARTED` |
| checkpoint | revision CAS + checkpoint + 所带 engine-owned events |
| Broker prepare | ToolExecution/Tool Activity + `TOOL_CALL_COMMITTED` |
| Broker settlement | effect/result/ref、Activity、必要 Artifact link + `TOOL_RESULT_COMMITTED` |
| success finalization | final assistant、citations、Run/Activity 成功状态 + `RUN_TERMINATED` |

所有 SQLite 写是短 `BEGIN IMMEDIATE`；事务中不做 LLM、Tool、RAG、Skill、文件系统或人工等待。seq 与 `runs.next_seq` 同事务推进，回滚不留 seq 洞。

`OUTPUT_GENERATION_STARTED` 的 SSE 名称为 `text_start`，后续 `OUTPUT_DELTA_COMMITTED` 是 `text`。客户端可因 visibility 过滤看到 seq 跳号；不能用连续 seq 推导丢数据。

## 4. 工具控制故障与 pending terminal

Broker 的 `DISPATCHED` 之后，`AttemptOwnershipLost` 及 ownership-coded `RuntimeFault` 原样穿透，旧 attempt 不产生 ToolResult/terminal。其他 executor `RuntimeFault` 则先 effect-aware settlement：READ_ONLY 记失败，可能副作用的 class 记不确定，再保留错误让 Coordinator 决策。

普通 FAILED 若还有 unresolved ToolEffect，Store fail-closed：把原失败写入 `pending_input.pending_terminal`，Run/父 Activity 转至严格 reconciliation 等待。operator signal 逐一推进 ToolExecution；只有最后一个确定后才写原 FAILED，且不再执行 Engine。`TIMED_OUT` 是唯一可保留 unresolved effect 的终态。

因此 ToolCall/ToolResult 的业务权威是 ToolExecution ledger，不是模型 draft、SSE 到达、HTTP timeout 或 Trace。

## 5. Trace 与事实写入的关系

`agent/main.py` 的 `TraceMiddleware` 在 API 请求上提供/生成 trace id；admission 把它保存到 Run，但不放入 idempotency digest。Worker 的 `RunCoordinator.execute_claim()` 从 durable Run 恢复 trace id（缺失时回退 run id），再创建 engine attempt span。`CommittedEventSink.attach_trace_span()` 记录 TTFT、事件计数及有限的工具/引用诊断字段。

Trace 是旁路：它可早于或晚于某次 SQLite commit 被写入，也可关闭、采样、截断、跨 API/Worker 文件分散或在重试中有多个文件。不能据此断言 SSE 已可见、Tool 已提交或 Run 已完成。反之，Trace 关闭后，commit/recovery/终态行为必须不变。

## 6. SSE 可见性与结束

`stream_events()` 按 cursor 从 Store 读取 committed public events，编码为带 `id: seq` 的 SSE block。显式 `after_seq` 优先于 `Last-Event-ID`。`RUN_TERMINATED` 输出后连接结束；heartbeat 只是无 seq、不入库的 API comment。SSE EOF 不是 Run terminal authority。

## 7. current-only 基础

这一切依赖唯一 current schema。`common/sqlite_schema.py` 仅允许空库完整创建或非空库 exact schema digest 验证；`CURRENT_SCHEMA_MISMATCH` 必须显式重建，绝不在 event/recovery 路径补迁移或兼容旧数据。

## 8. 阅读索引

1. `agent/runtime/application/events.py`
2. `agent/runtime/adapters/sqlite/store.py`
3. `agent/runtime/application/tool_broker.py`
4. `agent/runtime/application/coordinator.py`
5. `agent/runtime/api/runs.py`
6. `common/trace.py`、`common/obs.py`
