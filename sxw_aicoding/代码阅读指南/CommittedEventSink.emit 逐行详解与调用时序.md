# CommittedEventSink.emit 逐段详解与调用时序

本文以当前 `agent/runtime/application/events.py` 为准，解释 `CommittedEventSink.emit()` 的每个语义段。不使用固定行号，避免后续加注释或重排方法时文档失效。

## 1. 它的当前定位

`CommittedEventSink` 是 Coordinator 交给 EngineAdapter 的 `RuntimeIO` 实现，核心职责是：

- 把 text delta 聚合成已提交 `OUTPUT_DELTA_COMMITTED`；
- 在语义边界前 flush text；
- 提交 engine-owned Canonical Event；
- 提供带 revision CAS 的 checkpoint，并支持 checkpoint 与一组 engine-owned event 原子提交；
- 暴露强制 Tool Broker、cancel/deadline probe 和显式 final assistant。

正常工具的 `TOOL_CALL_COMMITTED` / `TOOL_RESULT_COMMITTED` 不由 `emit()` 重复写入，而是 Tool Broker/Store 账本事务的一部分。

## 2. 入口前置检查

```python
if self._closed:
    raise RuntimeError("event sink is closed")
self._raise_background_error()
self._trace_event(str(event_type), payload)
```

含义如下：

1. `close()` 或 `abort()` 后禁止继续写，避免 attempt 收口后出现迟到事件。
2. 100 ms timer 中的 flush 失败不能静默丢失；异常被保存在 `_background_error`，下一个显式边界继续抛出。
3. Trace 是旁路诊断；先记数不代表事件已持久化，Trace 也不是恢复事实源。

## 3. text 快速分支

```python
if event_type in {"text", EventType.OUTPUT_DELTA_COMMITTED}:
    await self.emit_text(
        str(payload.get("delta", "")),
        message_id=...,
        generation_id=...,
    )
    return
```

text 不走通用“每事件立即 append”路径，而是进入 `emit_text()`。当前 buffer 同时绑定 `message_id` 和 `generation_id`：

- 身份不变时，delta 可以按 100 ms / 2 KiB 聚合；
- 身份变化时，必须先 flush 旧 buffer，不得把两个 generation 拼成一条 Canonical Event。

`emit_text()` 还会在首个非空 delta 记录 TTFT，并把 delta 追加到 attempt-local `_full_text`。

Native 生产 Adapter 对每个 text 会 `await io.emit(...)`，但**不再紧接着 `force_flush()`**。这个 await 保证的是当前 delta 已按顺序被 Sink 接纳，而且 Adapter 还未允许 pump 拉下一个 provider item；它不保证未达阈值的小 delta 已单独写入 SQLite。

text 的 durable 时机仍由 Sink 决定：2 KiB 阈值、100 ms timer、message/generation 切换、非 text 事件、checkpoint、显式 `force_flush()` 或 `close()`。因此 Native 和 ADK 现在都遵守同一份 100 ms / 2 KiB 冻结聚合语义；Native 另外通过单 slot acknowledge 提供无界 queue 不存在的反压。

## 4. 非 text 分支：先拿锁，再 flush

```python
async with self._lock:
    await self._flush_locked()
    ...
```

这两步是一个临界区。目的不是用进程内锁“保证数据库事务”，而是防止 timer 或新 text 在“提交旧 text → 提交当前非 text 事件”之间插入。

### 4.1 Skill UI 配额

`skill_event` 在锁内先加载本 Run 已提交的 Skill UI 用量，再按 UTF-8 JSON bytes 检查：

- 单条默认最大 64 KiB；
- 每 Run 默认最多 2000 条；
- 每 Run 默认累计最大 8 MiB。

恢复后会从 durable events 重建计数，不会因 Worker 重启重置配额。超限报 `SKILL_UI_LIMIT_EXCEEDED`。

### 4.2 error

`error` 被保存为 INTERNAL `MODEL_MESSAGE_COMMITTED`，同时写入 `engine_error`。它只是诊断信号：attempt 的失败必须转成 `EngineOutcome` 交给 Coordinator 裁决，不能根据某个 error event 或 stream EOF 推导 Run 终态。Store 的 cancel、deadline、recovery/reconciliation 命令事务是另一类权威终态入口。

### 4.3 citation

`citation` 分支只收集 attempt-local 内容。成功收口时，Store 会从已提交 `knowledge_search` Evidence 派生引用，并把 final assistant、citation set 和 success terminal 置于同一事务。

### 4.4 通用 Canonical Event

`_EVENT_MAP` 将引擎名称映射为 Canonical EventType，例如：

| Runtime 入口名 | Canonical Event |
|---|---|
| `output_generation_started` | `OUTPUT_GENERATION_STARTED` |
| `tool_call` | `TOOL_CALL_COMMITTED` |
| `tool_result` | `TOOL_RESULT_COMMITTED` |
| `plan_step` | `MODEL_PLAN_UPDATED` |
| `skill_event` | `SKILL_UI_FRAME_COMMITTED` |
| `retrieval` | `RETRIEVAL_COMMITTED` |

然后通过 `store.append_events()` 写入，每个 draft 带 Activity、producer 和时间；Store 使用 fencing token 拒绝 stale attempt。

表中的 tool 映射只适用于真正由 Engine 负责投影的合成事件。例如默认 `off` 模式中，模型参数不合法或工具未找到时会生成整批零 dispatch 的错误对。正常 Broker 工具账本事实不能通过此映射再写一遍；实验性提前派发模式可能已执行安全只读前缀，不能把这句话外推为实验模式也绝对零执行。

## 5. `_flush_locked()` 的完整语义

`_flush_locked()` 只能在持有 `_lock` 时调用。它按以下顺序工作：

1. buffer 为空则直接返回；
2. join text，清空 buffer 和 byte 计数；
3. 取出并清空 message/generation 身份；
4. 取消尚未执行的 timer；
5. 追加一条 `OUTPUT_DELTA_COMMITTED`，payload 带 delta，Native 路径还带 message/generation identity。

先清内存、后 await Store 是安全的，因为锁尚未释放；如果 Store 失败，异常直接中断 attempt，不会把未提交 text 当成成功继续执行。

## 6. checkpoint 与事件的两段提交

`RuntimeIO.checkpoint()` 的时序是：

```text
force_flush()
  → store.save_checkpoint(expected_revision, engine_state, events)
```

需要区分两个原子边界：

- 之前缓冲的 text 由 `force_flush()` 先单独提交；
- 新 checkpoint row、调用方传入的 engine-owned events、`CHECKPOINT_COMMITTED` 以及必要的 plan 投影，在 `save_checkpoint()` 的同一 SQLite 事务中提交。

Native 在 `MODEL_REQUEST` checkpoint 中一并提交 `OUTPUT_GENERATION_STARTED`。因此客户端不会看到“已开始新 generation，但恢复点仍停在旧一代”的半提交状态。

## 7. 两条 Adapter 调用链

### 7.1 Native：直接 awaited sink

```text
NativeLoop kernel yield StreamEvent
  → attempt-level native-stream-pump 放入单个 envelope slot
  → await io.emit(event, data)
  → emit 返回后 acknowledge slot
  → pump 才获取下一个 kernel/provider 事件
```

单 slot 与 acknowledge 是 pull/顺序屏障，不是每 delta 的 durable 屏障。如果当前 `emit` 因达到字节阈值而正在写库，或是一个必须直接提交的非 text 事件，写库阻塞会自然阻止下一次 pull；如果 text 只进入 buffer，则等后续 timer/阈值/语义边界批量提交。

Adapter 同时维持一个 attempt-level `native-cancel-watch`，定期查取消而不是每个 delta 查 SQLite；整个消费循环受绝对 deadline timeout 监督。退出时 pump/watcher 都被取消并 await，然后 `aclose()` kernel/provider stream。Skill UI 的 awaited sink 也直接 `await io.emit()`。Native 没有 ADK merge queue，没有后台无界 Queue，也没有“无 RuntimeIO”生产 fallback。

### 7.2 ADK：只保留给两个引擎

```text
plan_execute / agent_loop
  → ADK 内部 run_stream + queue/merge
  → AdkEngineAdapter
  → Broker 已提交的 tool 投影：force_flush 后跳过
  → 其他事件：await io.emit()
```

Adapter stream 结束不等于成功；`RunContext.engine_outcome` 必须显式存在，否则返回 `ENGINE_OUTCOME_MISSING`。Native 的最终结果不走这条 RunContext 兼容面。

## 8. generation 与最终 Assistant

Native delta 带稳定 `message_id` 和 attempt generation identity。重试或恢复开始新 generation 时，先提交 `OUTPUT_GENERATION_STARTED`；API 投影为 `text_start`。

`CommittedEventSink.assistant_text` 仍可作为 ADK 的累积文本。Native 成功时必须调用：

```python
io.set_final_assistant(final_text, final_message_id, final_generation_id)
```

Coordinator 使用该 override 提交唯一最终 `ASSISTANT_MESSAGE_COMMITTED`。这条完整消息是最终语义权威，不等价于把所有历史 delta 简单拼接。

## 9. 建议的源码阅读顺序

1. `agent/runtime/ports/engine.py`：`RuntimeIO` 对外契约。
2. `agent/runtime/application/events.py`：Sink 的 emit/flush/checkpoint/final 实现。
3. `agent/runtime/adapters/sqlite/store.py`：`append_events()` 和 `save_checkpoint()` 事务。
4. `agent/runtime/adapters/adk_engines.py`：ADK-only Adapter。
5. `agent/engine/native_loop/engine.py`：Native 直接 RuntimeIO 适配。
6. `agent/runtime/application/tool_broker.py`：Broker-owned ToolCall/ToolResult 的账本边界。
