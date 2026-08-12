# CommittedEventSink：tool_call 与后续 text 分片的持久化时序

本文记录一次围绕 `CommittedEventSink` 锁、`tool_call` 和后续 text 分片的讨论。重点不是 SSE 展示顺序，而是：当 text 正在聚合、`tool_call` 先获得锁时，新到达的 text 会如何保存，以及为什么这种顺序是正确的。

相关实现位于：

- `agent/runtime/application/events.py`：`CommittedEventSink.emit()`、`emit_text()` 与 `_flush_locked()`；
- `agent/runtime/adapters/legacy_engines.py`：Broker-owned 工具事件前的 `io.force_flush()`；
- `agent/runtime/adapters/brokered_tools.py`：Broker 准备 ToolExecution 前的 `runtime_io.force_flush()`。

> 范围：本文讨论进入 `CommittedEventSink` 的 engine-owned 事件。Broker-owned 的 `tool_call/tool_result` 由 ToolBroker/Store 的事务写入，但它在写入前同样会先 `force_flush()`，因此 text 的持久化边界结论一致。

---

## 1. 原始提问

问题是：

> 假如 `tool_call` 先抢到锁，先让 text 落盘；但此次 text 还没有累积完，入库后又有新的 text 分片，怎么办？

这里的“还没有累积完”指 text 按 `100ms / 2KiB` 聚合，原本可能还会继续拼进同一条 `OUTPUT_DELTA_COMMITTED`，但中间出现了 `tool_call`。

---

## 2. 先给结论

不会丢 text，也不会把新旧 text 混在一起。`tool_call` 会成为一次明确的持久化边界：

```text
tool_call 之前已进入 buffer 的 text
  → 强制 flush 并提交 OUTPUT_DELTA_COMMITTED
  → 提交 TOOL_CALL_COMMITTED
  → 释放锁
tool_call 之后到达的 text
  → 进入一个新的 buffer
  → 在后续阈值、定时器、下一语义边界、checkpoint 或 Run 收尾时提交
```

因此，`tool_call` 的出现最多会让一段 text 的聚合粒度变小；它不会截断、覆盖或丢失 text。

---

## 3. 例子：事件日志与最终回答是两件事

假设上游模型流的语义顺序为：

```text
"你好，我" → tool_call("search") → tool_result(...) → "查到结果了"
```

那么 `run_events` 中的规范事件应按该因果顺序保存：

```text
OUTPUT_DELTA_COMMITTED: "你好，我"
TOOL_CALL_COMMITTED:    { name: "search", ... }
TOOL_RESULT_COMMITTED:  { ... }
OUTPUT_DELTA_COMMITTED: "查到结果了"
```

这并不表示最终助手文本会把 `tool_call` 的 JSON 拼进去。`CommittedEventSink._full_text` 只累积 text delta，最终助手文本仍然是：

```text
你好，我查到结果了
```

工具调用和工具结果是独立的结构化事实事件：SSE/UI 可以将它们投影为工具调用卡片；恢复、审计和排障也需要保留它们在 text 中间出现的真实位置。

---

## 4. 为什么不是“旧 text → 新 text → tool_call”？

讨论中一个自然的直觉是：既然新 text 还没有“攒够”，能否先继续攒完：

```text
"你好，我" → "查到结果了" → tool_call
```

答案取决于**上游模型流的真实顺序**，而不是 buffer 是否已满。

### 情况 A：模型流确实先给出 tool_call

若上游顺序是：

```text
"你好，我" → tool_call → "查到结果了"
```

则必须保存为：

```text
"你好，我" → tool_call → "查到结果了"
```

特别是在典型工具调用流程中，后续文本往往依赖工具结果：

```text
"你好，我" → tool_call → tool_result → "查到结果了"
```

若把后面的 text 提前到 `tool_call` 前，事件回放会声称模型在调用工具之前已经得到结果。这会破坏因果顺序，也会让故障恢复和审计失真。

### 情况 B：模型流实际先给出全部 text

若上游顺序原本就是：

```text
"你好，我" → "查到结果了" → tool_call
```

第二个 text 分片会先通过 `emit_text()` 进入 buffer。随后 `tool_call` 获得锁，调用 `_flush_locked()` 时会将二者合并提交：

```text
OUTPUT_DELTA_COMMITTED: "你好，我查到结果了"
TOOL_CALL_COMMITTED:    { ... }
```

也就是说，锁不会自行改变模型流顺序；它只在非 text 语义边界前，将**已经收到的** text 变为 durable event。

---

## 5. 对“tool_call 抢到锁”的准确理解

本次讨论最终形成的理解是：

> 当 `tool_call` 抢到锁时，它代表当前已按模型流顺序交给 Runtime 的下一个语义边界。

它的含义不是“无论调用方怎样并发调度，抢锁者天然就是真实的最新 LLM 内容”；锁本身不能从乱序并发任务中恢复因果关系。它依赖上游流消费者遵守下面的契约：

```python
for event in provider_stream:
    await io.emit(event.type, event.payload)
```

即按 provider 给出的顺序逐个 `await` 投递，而不是对多个 `emit_text()` / `emit("tool_call")` 随意 `create_task()` 并发投递。

在这个前提下：

1. `tool_call` 之前的 text 已经进入 buffer，或已单独提交；
2. `tool_call` 持锁后先 `_flush_locked()`，将 buffer 中的旧 text 提交；
3. 再提交 `TOOL_CALL_COMMITTED`；
4. 在锁持有期间到达的后续 text 只能等待；
5. 锁释放后，后续 text 进入新的 buffer，等待下一次 flush。

所以可将其概括为：

```text
旧 text → tool_call → 新 text
```

这里的“旧/新”按**模型流的语义位置**区分，而非按“是否凑满 2KiB”区分。

---

## 6. 与源码的对应关系

`CommittedEventSink.emit()` 对非 text 事件使用同一把 `asyncio.Lock`：

```python
async with self._lock:
    await self._flush_locked()
    await self.store.append_events(...)
```

因此，进入 Sink 的 `tool_call` 会在写入自己之前，先写出所有已有 text buffer。

`emit_text()` 也使用这把锁：

```python
async with self._lock:
    self._buffer.append(delta)
    self._full_text.append(delta)
    ...
```

所以在 `tool_call` 的“flush 旧 text → 写 tool_call”临界区内，新的 text 不可能插入旧 buffer。它只能等锁释放后写入新的 buffer。

新 buffer 的后续提交触发条件为：

- 累计达到 `flush_bytes`（默认 2KiB）；
- `flush_ms` 定时器到期（默认 100ms）；
- 后续非 text 事件、checkpoint 调用 `force_flush()`；
- Sink `close()` 时的最终 flush。

---

## 7. 时序图

```text
模型流：     text("你好，我") ─────→ tool_call(search) ─────→ text("查到结果了")
                                      │
text buffer： ["你好，我"]            │
                                      ▼
tool_call：                         获取同一把锁
                                      │
                                      ├─ flush → OUTPUT_DELTA_COMMITTED("你好，我")
                                      ├─ append → TOOL_CALL_COMMITTED(search)
                                      └─ 释放锁
                                                                         │
新 text：                                                               ▼
                                                               进入新 buffer
                                                               ["查到结果了"]
                                                                         │
                                              timer / 阈值 / 下一边界 / close
                                                                         ▼
                                           OUTPUT_DELTA_COMMITTED("查到结果了")
```

---

## 8. 最终结论

`tool_call` 抢锁不是为了“抢在 text 完整输出之前写自己”，而是为了将其作为一个不可跨越的语义边界：先持久化边界前所有已收到的 text，再持久化 `tool_call`，边界后的 text 则进入下一段聚合。

因此，正确性来自两个条件的组合：

1. 上游按模型流顺序将事件交给 Runtime；
2. Sink 用同一把锁串行化 buffer 操作和“flush text → 写非 text 事件”的提交。

最终得到的不是“所有 text 必须先攒完整再写 tool_call”，而是“text 的聚合不能跨越 `tool_call` 这类语义边界”。
