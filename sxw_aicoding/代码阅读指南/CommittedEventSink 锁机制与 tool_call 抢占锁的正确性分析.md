# CommittedEventSink 锁机制与 ToolCall 边界的正确性分析

本文对照当前 `agent/runtime/application/events.py`、`agent/runtime/adapters/adk_engines.py`、`agent/engine/native_loop/engine.py` 与 Tool Broker/Store 实现，说明 `CommittedEventSink` 的锁到底保护什么，以及 ToolCall 为什么会形成 text 的提交边界。

## 1. 先记住两个不同的写入权威

当前实现不是“所有事件都进 Sink”：

- engine-owned 事件：如 text、`OUTPUT_GENERATION_STARTED`、plan、Skill UI 帧和模型未进入 Broker 的合成工具错误，由 `RuntimeIO.emit()` 或 `RuntimeIO.checkpoint(..., events=...)` 提交。
- Broker-owned 工具事实：正常 ToolCall 由 Store 的 batch PREPARE 事务同时写入 ToolExecution、Tool Activity 和 `TOOL_CALL_COMMITTED`；ToolResult 由结算事务同时更新账本并写入 `TOOL_RESULT_COMMITTED`。

因此，Sink 锁不是 ToolExecution 的并发锁，也不能代替 SQLite 事务、fencing 或 checkpoint CAS。它只保护单个 attempt 内 Sink 自己的 text buffer 及 engine-owned 事件顺序。

## 2. 为什么单条模型流仍然需要锁

provider chunk 本身有顺序，但 Sink 内部至少有两个可能竞争的协程：

1. 主流程在 `emit_text()` 中追加 delta，达到 2 KiB 时立即 flush；
2. 首个未达阈值的 delta 会启动延迟任务，默认 100 ms 后 flush。

同时，主流程还可以进入 `emit()`、`force_flush()`、`checkpoint()` 或 `close()`。如果不共用一把 `asyncio.Lock`，就可能出现：

- timer 与主流程同时 join/clear buffer；
- message/generation 标识与 text 被错配；
- 非 text 事件已入库，它之前的 text 还留在内存。

锁保护的共享状态是：

```text
_buffer / _buffer_bytes
_buffer_message_id / _buffer_generation_id
_timer
以及“flush 旧 text → 写入新的 engine-owned 事件”的临界区
```

`_full_text` 是 attempt 内的累计投影；Native 的最终语义不再依赖这个累加值，而由 `set_final_assistant(text, message_id, generation_id)` 显式指定。

## 3. `emit()` 的临界区

当事件不是 text 时，核心顺序是：

```python
async with self._lock:
    await self._flush_locked()
    # 然后处理 skill_event / error / citation / canonical event
```

这保证：一旦一个非 text 事件在 Sink 中提交，所有在它之前已交给 Sink 的 text 必定已先提交。

不同分支的行为不同：

- `skill_event` 先核对持久化配额，再写 `SKILL_UI_FRAME_COMMITTED`；
- `error` 只写 INTERNAL 诊断事件，不决定 Run 终态；
- `citation` 只收集 attempt-local 投影，最终 citation 由 Store 从已提交 Evidence 派生；
- 其他 engine-owned 事件映射为 Canonical Event 并调用 `append_events()`。

## 4. ToolCall “抢到锁”的准确理解

假设逻辑顺序是：

```text
text("A") → ToolCall(search) → text("B")
```

如果这个 ToolCall 是真正进入 Sink 的 engine-owned 事件，则锁内顺序为：

```text
buffer("A")
  → ToolCall 获取锁
  → flush OUTPUT_DELTA_COMMITTED("A")
  → 提交 ToolCall 事件
  → 释放锁
  → text("B") 进入新 buffer
```

不会丢 text，只会让原本可能合并的一段 text 在语义边界处提前结束。新 delta 不能在“flush A → 写 ToolCall”之间插入，因为 `emit_text()` 也必须获取同一把锁。

但生产路径中的正常工具事件属于 Broker：

- ADK 两引擎在 Broker PREPARE 之前显式 `force_flush()`，之后 Adapter 不会重复 emit Broker 投影；
- Native 每个 text 帧都等待 `io.emit()` 并紧接着 `force_flush()`，模型完整结束后又先提交 `MODEL_RESPONSE_COMMITTED` checkpoint，再进入 Broker batch PREPARE。

所以对 Broker-owned ToolCall，正确的因果屏障是“先 flush/checkpoint，再由 Broker 事务提交工具事实”，而不是让 ToolCall 与 text 去争 Sink 锁。

## 5. Native 与 ADK 的并发差异

### Native

`NativeLoopAdapter` 直接循环消费 Native kernel 的 `StreamEvent`，每次都 `await io.emit(...)` 完成后才能拉取下一个事件。Skill UI sink 也直接 await 同一 RuntimeIO。这是自然背压：Runtime 提交被阻塞时，provider 拉流、checkpoint 和工具派发都不能绕过。Native 不经过后台无界队列。

### ADK

`AdkEngineAdapter` 只服务 `plan_execute` 和 `agent_loop`。这两个引擎保留 ADK 内部的 event queue/merge 路径；Adapter 在消费合并后事件时仍然逐个 await RuntimeIO，并在 Broker-owned 工具投影处只做 flush。这个队列路径不再被 Native 共用。

## 6. 锁无法提供的保证

`asyncio.Lock` 只在当前进程、当前 Sink 对象内有效。下列保证来自其他层：

- 多 Worker 或 stale attempt 写入拒绝：Activity lease + fencing token；
- checkpoint 不被旧 attempt 覆盖：revision CAS；
- ToolCall 批次全有或全无：Broker/Store 的 SQLite 写事务；
- Event seq 无回滚空洞：`runs.next_seq` 与 event batch 同事务；
- 丢失 attempt ownership 后不再写库：`abort()` 取消 timer 并丢弃未提交 buffer。

## 7. 必须保持的不变量

1. text buffer 不能跨 message_id 或 generation_id 聚合。
2. 任何进入 Sink 的非 text 事件提交前，必须先 flush 已接收 text。
3. 正常工具账本事实只能由 Broker/Store 提交一次。
4. Native 必须等待前一个 RuntimeIO 提交，才能拉取下一个 provider 事件。
5. SSE 只读已提交 `run_events`，Sink 锁不参与 SSE 订阅者协调。

## 8. 建议的源码阅读顺序

1. `agent/runtime/application/events.py`：`emit_text`、`_flush_after_delay`、`_flush_locked`、`emit`。
2. `agent/runtime/adapters/adk_engines.py`：两个 ADK 引擎的事件消费与 Broker 投影去重。
3. `agent/engine/native_loop/engine.py`：直接 awaited RuntimeIO 与 Skill UI sink。
4. `agent/runtime/application/tool_broker.py` 及 `agent/runtime/adapters/sqlite/store.py`：ToolCall PREPARE 和 ToolResult 结算事务。
