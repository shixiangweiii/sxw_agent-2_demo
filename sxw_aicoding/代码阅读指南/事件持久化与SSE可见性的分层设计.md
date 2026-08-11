# 事件持久化与 SSE 可见性的分层设计

本文档回答一个常见疑问：为什么 `native_loop` 的循环体里直接 `yield StreamEvent(...)`，却看不到持久化逻辑？答案是——**“先持久化、再对外可见”的语义被刻意上移到 Runtime 适配层**，而不是散落在引擎循环内部。本文档从架构意图、调用链路、源码定位三个维度展开说明。

---

## 目录

- [1. 核心问题与总体思路](#1-核心问题与总体思路)
- [2. 分层架构图](#2-分层架构图)
- [3. 各层职责与关键代码](#3-各层职责与关键代码)
- [4. 端到端数据流时序图](#4-端到端数据流时序图)
- [5. 关键源码位置索引](#5-关键源码位置索引)
- [6. 关键不变量](#6-关键不变量)
- [7. 常见疑问](#7-常见疑问)
- [8. 阅读建议](#8-阅读建议)

---

## 1. 核心问题与总体思路

### 1.1 问题场景

阅读 `agent/engine/native_loop/loop.py` 时容易产生的困惑：

```python
# loop.py:199
yield StreamEvent("text", {"delta": item.text})

# loop.py:209-210
for ev in self._call_events(item.call):
    yield ev

# loop.py:306-307
for ev in self._result_events(outcome):
    yield ev
```

这些 `yield` 看起来只是把事件抛给上游，**没有 `INSERT`、没有 `commit`、没有数据库操作**。如果按“先持久化、再对外可见”的原则，持久化逻辑在哪里？

### 1.2 总体思路

`native_loop` 被设计为**纯粹的推理循环**，只负责：

1. 与 LLM 流式交互；
2. 产出统一的事件草稿（`StreamEvent`）；
3. 在关键阶段调用 `checkpoint` hook，保存引擎恢复状态。

而“事件是否落盘、如何落盘、何时对外可见”属于 **Runtime 编排语义**，由更上层的 `LegacyEngineAdapter` + `CommittedEventSink` 统一保证。这样三代引擎（`plan_execute`、`agent_loop`、`native_loop`）可以共享同一套持久化与 SSE 可见性语义，避免每个引擎各自实现一半、行为不一致。

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         引擎层：只产出事件草稿                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  NativeLoop.run()                                                   │   │
│  │  - yield StreamEvent("text", ...)                                   │   │
│  │  - yield StreamEvent("tool_call", ...)                              │   │
│  │  - await self._checkpoint(state, phase)  # 恢复状态                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ yield StreamEvent
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      引擎适配层：透传 + 注入恢复 hook                         │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  NativeLoopEngine.run_stream()                                      │   │
│  │  - 把 loop.run() 的事件透传出去                                      │   │
│  │  - 提供 persist(state, phase) 作为 checkpoint hook                   │   │
│  │  - persist() 内部调用 rc.runtime_io.checkpoint(...)                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ yield StreamEvent
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Runtime 适配层：决定哪些事件需要 emit                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  LegacyEngineAdapter.execute()                                      │   │
│  │  - Broker 已拥有的 tool_call/tool_result → force_flush() + continue  │   │
│  │  - 其他事件（text/error/engine 自有）→ io.emit(event.event, event.data)│   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ io.emit / force_flush
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   事实写入层：先 commit，再让 SSE 可见                        │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  CommittedEventSink                                                 │   │
│  │  - emit()：拿到锁 → flush text buffer → store.append_events()        │   │
│  │  - emit_text()：累积 delta，满足阈值后 _flush_locked() 写库          │   │
│  │  - checkpoint()：force_flush() → store.save_checkpoint()             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ append_events / save_checkpoint
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SQLite：runtime.db                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  run_events 表：append-only，seq 单调递增                            │   │
│  │  checkpoints 表：恢复状态 + revision CAS                             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ SELECT seq > cursor
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Runtime API 进程 (:8000)                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  GET /runs/{id}/events                                              │   │
│  │  - 只读取已提交事件                                                  │   │
│  │  - 250ms 轮询 / 15s heartbeat                                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ SSE
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              浏览器 (前端)                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 分层架构图

### 2.1 纵向分层

```text
┌────────────────────────────────────┐
│  前端 (SSE 订阅者)                  │
│  只能看到已提交事件                  │
└──────────────┬─────────────────────┘
               │ SSE
┌──────────────▼─────────────────────┐
│  Runtime API (:8000)               │
│  stream_events() → list_events()   │
└──────────────┬─────────────────────┘
               │ SELECT committed
┌──────────────▼─────────────────────┐
│  SQLite (runtime.db)               │
│  run_events / checkpoints          │
└──────────────┬─────────────────────┘
               │ INSERT / UPDATE
┌──────────────▼─────────────────────┐
│  CommittedEventSink                │
│  先 commit，再对外可见              │
└──────────────┬─────────────────────┘
               │ io.emit / checkpoint
┌──────────────▼─────────────────────┐
│  LegacyEngineAdapter               │
│  路由事件：emit 或 force_flush      │
└──────────────┬─────────────────────┘
               │ yield StreamEvent
┌──────────────▼─────────────────────┐
│  NativeLoopEngine                  │
│  透传事件 + 注入 checkpoint hook    │
└──────────────┬─────────────────────┘
               │ yield StreamEvent
┌──────────────▼─────────────────────┐
│  NativeLoop                        │
│  产出事件草稿 + 触发 checkpoint     │
└────────────────────────────────────┘
```

### 2.2 为什么引擎层不直接落盘

| 如果让引擎层落盘 | 实际上层负责 | 收益 |
|---|---|---|
| 每个引擎都要自己实现 batch、visibility、cancel 探测 | `CommittedEventSink` 统一实现 | 行为一致，避免重复 |
| `native_loop` 要知道 `run_id`、`activity_id`、`fencing_token` | 由 `RunContext` / `RuntimeIO` 传入 | 引擎只关注循环逻辑 |
| tool_call/tool_result 的事实来源可能重复 | `LegacyEngineAdapter` 判断 `authority` | Broker 权威事件不重复 emit |
| 三代引擎产出的事件格式可能不一致 | 统一 `StreamEvent` + `EventDraft` 转换 | SSE 订阅者无感知 |

---

## 3. 各层职责与关键代码

### 3.1 引擎层：`NativeLoop.run()`

**文件**：`agent/engine/native_loop/loop.py`

职责：
- 驱动自研 Tool-Use 循环；
- 产出文本增量、工具调用、工具结果等事件草稿；
- 在 `MODEL_REQUEST` / `TOOL_BATCH_COMMITTED` / `TOOL_RESULT_COMMITTED` / `NEXT_TURN` / `COMPLETED` 等阶段触发 `_checkpoint()`。

关键代码：

```python
# loop.py:199
yield StreamEvent("text", {"delta": item.text})

# loop.py:209-210
for ev in self._call_events(item.call):
    yield ev

# loop.py:305-309
async for outcome in executor.run_calls(...):
    for ev in self._result_events(outcome):
        yield ev
    state.messages.append(outcome.message)
    await self._checkpoint(state, "TOOL_RESULT_COMMITTED")
```

这里的 `yield` 只是**草稿 transport**。真正的落盘在上层。

### 3.2 引擎适配层：`NativeLoopEngine.run_stream()`

**文件**：`agent/engine/native_loop/engine.py`

职责：
- 把 `NativeLoop` 接到统一的 `ReasoningEngine` 端口；
- 透传事件；
- 实现 `persist(state, phase)` 作为 `checkpoint` hook，内部调用 `rc.runtime_io.checkpoint()`。

关键代码：

```python
# engine.py:103-129
async def persist(state: LoopState, phase: str) -> None:
    nonlocal checkpoint_revision
    if rc.runtime_io is None:
        return
    plan = state.tool_state.get(TASK_PLAN_KEY)
    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    current = plan.get("current", 1) if isinstance(plan, dict) else 1
    model_plan = [...]
    saved = await rc.runtime_io.checkpoint(
        WorkingState(...),
        expected_revision=checkpoint_revision,
        engine_state=_serialize_state(state, phase),
    )
    checkpoint_revision = saved.revision

# engine.py:131-148
loop = NativeLoop(
    ...,
    checkpoint=persist,
    config=LoopConfig(...),
)

# engine.py:156-159
async for event in merge_runner_events(
    loop.run(messages, initial_state=initial_state), lambda e: [e],
):
    yield event
```

注意：`persist()` 保存的是 **WorkingState + engine_state（恢复状态）**，不是 SSE 事件本身。SSE 事件的持久化由 `CommittedEventSink.emit()` 负责。

### 3.3 Runtime 适配层：`LegacyEngineAdapter.execute()`

**文件**：`agent/runtime/adapters/legacy_engines.py`

职责：
- 消费 `engine.run_stream(rc)`；
- 判断事件是否已被 Tool Broker 持久化；
- 对需要持久化的事件调用 `io.emit()`。

关键代码：

```python
# legacy_engines.py:154-170
async for event in engine.run_stream(rc):
    # Tool Broker 已经 committed 权威 call/result 事件。
    # Native unknown-tool 和参数解析失败没有外部 ToolExecution，
    # 所以它们的 authority=engine 投影必须成为 durable facts。
    # ADK 投影没有 hint，默认归属 Broker authority。
    if _broker_owns_tool_projection(event, self.tool_broker):
        # 保持模型流顺序：子 2KiB text buffer 必须先 durable，
        # 然后执行层才能 commit 随后的 ToolCall/ToolResult fact。
        await io.force_flush()
        if await io.is_cancelled():
            return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
        continue
    await io.emit(event.event, event.data)
    if await io.is_cancelled():
        return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
```

判断规则：

```python
# legacy_engines.py:31-43
def _broker_owns_tool_projection(event: Any, tool_broker: Any) -> bool:
    return (
        tool_broker is not None
        and event.event in {"tool_call", "tool_result"}
        and event.authority != "engine"
    )
```

### 3.4 事实写入层：`CommittedEventSink`

**文件**：`agent/runtime/application/events.py`

职责：
- 统一实现 text 聚合（100ms / 2KiB）；
- 在 flush 时调用 `store.append_events()` 写入 `run_events`；
- `checkpoint()` 时先 `force_flush()` 再 `save_checkpoint()`。

关键代码：

```python
# events.py:130-175
async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
    ...
    self._trace_event(str(event_type), payload)
    if event_type in {"text", EventType.OUTPUT_DELTA_COMMITTED}:
        await self.emit_text(str(payload.get("delta", "")))
        return
    async with self._lock:
        await self._flush_locked()
        ...
        await self.store.append_events(
            self.run_id,
            [EventDraft(canonical, dict(payload), ...)],
            activity_id=self.activity_id,
            fencing_token=self.fencing_token,
            now_ms=self.clock.now_ms(),
        )

# events.py:177-189
async def emit_text(self, delta: str) -> None:
    if not delta:
        return
    ...
    async with self._lock:
        self._buffer.append(delta)
        self._full_text.append(delta)
        self._buffer_bytes += len(delta.encode("utf-8"))
        if self._buffer_bytes >= self.flush_bytes:
            await self._flush_locked()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_after_delay())

# events.py:219-241
async def _flush_locked(self) -> None:
    if not self._buffer:
        return
    text = "".join(self._buffer)
    self._buffer.clear()
    self._buffer_bytes = 0
    ...
    await self.store.append_events(
        self.run_id,
        [EventDraft(
            EventType.OUTPUT_DELTA_COMMITTED,
            {"delta": text},
            ...,
        )],
        ...,
    )

# events.py:249-267
async def checkpoint(...):
    await self.force_flush()
    return await self.store.save_checkpoint(...)
```

### 3.5 持久化存储：`RuntimeStore.append_events()`

`CommittedEventSink` 依赖 `RuntimeStore` 端口。实际写入 SQLite 的实现通常位于 `agent/runtime/persistence/repository.py` 或 `service.py`。该层保证：

- `BEGIN IMMEDIATE` 短事务；
- `runs.next_seq` 与 event batch 同事务更新；
- 回滚不留 seq 洞；
- 启用 WAL、`synchronous=FULL`、外键、busy timeout。

SSE 端点只读取已提交的事件，因此**数据库 commit 是前端可见的唯一前提**。

---

## 4. 端到端数据流时序图

### 4.1 一个 text delta 的完整链路

```text
NativeLoop                NativeLoopEngine          LegacyEngineAdapter       CommittedEventSink        SQLite              SSE API           前端
   │                           │                           │                         │                    │                  │              │
   │──yield StreamEvent(text)──>│                           │                         │                    │                  │              │
   │                           │──yield StreamEvent(text)──>│                         │                    │                  │              │
   │                           │                           │──io.emit("text", ...)──>│                    │                  │              │
   │                           │                           │                         │──emit_text(delta)  │                  │              │
   │                           │                           │                         │   (累积 buffer)     │                  │              │
   │                           │                           │                         │                    │                  │              │
   │──yield StreamEvent(text)──>│                           │                         │                    │                  │              │
   │                           │──yield StreamEvent(text)──>│                         │                    │                  │              │
   │                           │                           │──io.emit("text", ...)──>│                    │                  │              │
   │                           │                           │                         │   (buffer 满 2KiB)  │                  │              │
   │                           │                           │                         │──_flush_locked()   │                  │              │
   │                           │                           │                         │──append_events()──>│                  │              │
   │                           │                           │                         │                    │──COMMIT─────────>│              │
   │                           │                           │                         │                    │                  │──list_events─>│
   │                           │                           │                         │                    │                  │<─seq=N────────│
   │                           │                           │                         │                    │                  │──SSE id:N───>│
```

### 4.2 一个 tool_call 的完整链路

```text
NativeLoop                NativeLoopEngine          LegacyEngineAdapter       ToolBroker               SQLite              SSE API           前端
   │                           │                           │                        │                     │                  │              │
   │──yield StreamEvent(       │                           │                        │                     │                  │
   │    tool_call,             │                           │                        │                     │                  │
   │    authority=broker)─────>│                           │                        │                     │                  │
   │                           │──yield StreamEvent(       │                        │                     │                  │
   │                           │    tool_call)────────────>│                        │                     │                  │
   │                           │                           │──_broker_owns_tool_    │                     │                  │
   │                           │                           │   projection=True       │                     │                  │
   │                           │                           │──io.force_flush()─────>│                     │                  │
   │                           │                           │   (把前面 text buffer   │                     │                  │
   │                           │                           │    先 commit)           │                     │                  │
   │                           │                           │                        │                     │                  │
   │                           │                           │                        │──commit ToolExecution│                  │
   │                           │                           │                        │  (tool_executions)  │                  │
   │                           │                           │                        │                     │                  │
   │                           │                           │                        │──emit broker events? │                  │
   │                           │                           │                        │  (取决于 Broker 实现) │                  │
   │                           │                           │   continue              │                     │                  │
```

注意：Broker 拥有的 tool_call/tool_result **不重复 emit**，因为 Tool Broker 已经通过 `tool_executions` 等表保存了权威事实。`force_flush()` 只是为了保持“text 在前、tool 在后”的模型流顺序。

### 4.3 checkpoint 的完整链路

```text
NativeLoop                NativeLoopEngine          CommittedEventSink        SQLite
   │                           │                           │                    │
   │──_checkpoint(state,       │                           │                    │
   │  "TOOL_BATCH_COMMITTED")  │                           │                    │
   │                           │                           │                    │
   │                           │──persist(state, phase)    │                    │
   │                           │  (作为 checkpoint hook)   │                    │
   │                           │                           │                    │
   │                           │──rc.runtime_io.checkpoint─>│                    │
   │                           │  (WorkingState +          │                    │
   │                           │   engine_state)           │                    │
   │                           │                           │                    │
   │                           │                           │──force_flush()    │
   │                           │                           │  (text buffer 落盘)│
   │                           │                           │                    │
   │                           │                           │──save_checkpoint()│
   │                           │                           │  (checkpoints 表) │
   │                           │                           │──>│
   │                           │                           │   COMMIT           │
```

---

## 5. 关键源码位置索引

| 层级 | 文件路径 | 关键符号 | 作用 |
|---|---|---|---|
| 引擎循环 | `agent/engine/native_loop/loop.py` | `NativeLoop.run`, `_checkpoint`, `_call_events`, `_result_events`, `T_COMPLETED` | 产出事件草稿，触发恢复 checkpoint |
| 引擎适配 | `agent/engine/native_loop/engine.py` | `NativeLoopEngine.run_stream`, `persist`, `_serialize_state` | 透传事件，注入 checkpoint hook |
| Runtime 适配 | `agent/runtime/adapters/legacy_engines.py` | `LegacyEngineAdapter.execute`, `_broker_owns_tool_projection` | 路由事件到 emit 或 force_flush |
| 事实写入 | `agent/runtime/application/events.py` | `CommittedEventSink`, `emit`, `emit_text`, `_flush_locked`, `checkpoint`, `force_flush` | 先 commit 再对外可见 |
| RuntimeIO 协议 | `agent/runtime/ports/engine.py` | `RuntimeIO` Protocol | 定义 emit / checkpoint / force_flush 等接口 |
| 数据模型 | `agent/runtime/domain/models.py` | `EventType`, `WorkingState`, `CheckpointRecord`, `Visibility` | 事件类型与领域模型 |
| SSE 读取 | `agent/runtime/api/runs.py` | `stream_events` / `list_events` | 只读已提交事件 |
| 存储实现 | `agent/runtime/persistence/repository.py` / `service.py` | `RuntimeStore.append_events`, `save_checkpoint` | SQLite 事务写入 |

---

## 6. 关键不变量

1. **先 commit，后 SSE 可见**
   - `CommittedEventSink` 必须在 `store.append_events()` 事务提交成功后，事件才能被 SSE 读取。

2. **引擎层只产草稿，不拥有持久化语义**
   - `NativeLoop` 的 `yield StreamEvent(...)` 只是事件草稿 transport。
   - 是否落盘、如何落盘由上层 `LegacyEngineAdapter` + `CommittedEventSink` 决定。

3. **Broker 权威事件不重复 emit**
   - 对于 `authority != "engine"` 的 `tool_call` / `tool_result`，`LegacyEngineAdapter` 只 `force_flush()` 前面的 text buffer，然后 `continue`，不调用 `io.emit()`。
   - Tool Broker 的 `tool_executions` 表是这些事件的唯一事实来源。

4. **text delta 聚合后统一落盘**
   - 单条 text delta 不立即写库，而是按 100ms / 2KiB 聚合，在 `_flush_locked()` 中统一 `append_events()`。

5. **checkpoint 必须先 flush text buffer**
   - `CommittedEventSink.checkpoint()` 第一行就是 `await self.force_flush()`，确保恢复点之前的所有 text 都已落盘。

6. **取消检查在每次 emit 边界**
   - `LegacyEngineAdapter` 在每次 `force_flush()` / `emit()` 后检查 `io.is_cancelled()`，保证取消能尽快生效。

---

## 7. 常见疑问

### Q1：`loop.py` 里直接 `yield` 事件，如果进程崩溃，事件会丢失吗？

不会。`yield` 只是给上层消费，真正写库发生在 `CommittedEventSink.emit()` / `_flush_locked()` / `checkpoint()`。只要上层成功消费并 commit，事件就是 durable 的。如果在上层 commit 之前进程崩溃，那段草稿本就不该被前端看到。

### Q2：Tool Broker 已经持久化了 tool_call，为什么还要有 `tool_call` 这个 SSE 事件？

SSE 事件是**面向 UI 的投影**，而 `tool_executions` 是**面向调度和恢复的事实**。UI 需要知道“模型现在调用了什么工具”，所以 `LegacyEngineAdapter` 会消费 `NativeLoop` 产出的 `tool_call` 草稿并交给 `CommittedEventSink` 写入 `run_events`（如果 authority=engine），或者由 Broker 自己的机制写入。最终 SSE 读取的是同一张 `run_events` 表。

### Q3：`emit_text()` 为什么不每条 delta 都写库？

为了性能。LLM 流式输出可能每秒几十到几百个 delta，逐条写 SQLite 会产生大量小事务。按 100ms / 2KiB 聚合后批量提交，既保证前端实时性，又避免事务风暴。

### Q4：`NativeLoop` 的 `_checkpoint()` 保存的是事件吗？

不是。`_checkpoint()` 保存的是 **WorkingState + 引擎私有状态**（`engine_state`），用于恢复。SSE 事件由 `CommittedEventSink` 通过 `io.emit()` 单独保存。两者是并行的持久化路径：
- `checkpoint`：恢复状态；
- `emit`：对外可见的事件历史。

---

## 8. 阅读建议

### 8.1 推荐阅读顺序

1. **先理解 `StreamEvent` 和 `RuntimeIO` 协议**
   - `agent/stream/event_converters.py`
   - `agent/runtime/ports/engine.py`

2. **从入口反向追踪**
   - `agent/runtime/adapters/legacy_engines.py:154-170`：事件路由；
   - `agent/runtime/application/events.py:130-175`：emit 落盘；
   - `agent/engine/native_loop/engine.py:156-159`：透传 + checkpoint hook；
   - `agent/engine/native_loop/loop.py:139-328`：循环体事件产出。

3. **对照 SSE 读取端**
   - `agent/runtime/api/runs.py`：SSE 端点只读已提交事件。

4. **结合可靠性文档**
   - `docs/reliability/README.md`
   - `docs/reliability/state-ownership-registry.md`

### 8.2 调试技巧

- 想看事件是否落盘：在 `CommittedEventSink.emit()` 和 `_flush_locked()` 打断点；
- 想看 SSE 是否读到：在 `stream_events()` 的 `list_events()` 处打断点；
- 想看 checkpoint：在 `CommittedEventSink.checkpoint()` 和 `NativeLoop._checkpoint()` 打断点；
- 想看 Broker 事件是否重复：在 `LegacyEngineAdapter.execute()` 的 `_broker_owns_tool_projection()` 判断处打断点。

---

## 9. 一句话总结

> `native_loop` 的 `yield StreamEvent(...)` 只是引擎内部事件草稿；“先持久化、再对外可见”的语义由 `LegacyEngineAdapter` 路由、`CommittedEventSink` 统一 commit、SQLite 作为唯一事实来源、SSE 只读已提交事件共同保证。引擎层不直接落盘，是刻意为之的分层设计。
