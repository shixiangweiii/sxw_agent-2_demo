# CommittedEventSink 锁机制与 tool_call 抢占锁的正确性分析

本文档专门回答关于 `CommittedEventSink` 事件写入锁的三个核心问题：

1. 事件不是按 SSE 流顺序走的吗，为什么会有并发？
2. 既然 OpenAI / DashScope 返回的流本身有顺序，为什么还要加 `asyncio.Lock`？
3. 如果 `tool_call` 先抢到锁，前面正在缓冲的 text delta 会不会丢、顺序会不会乱？

结论先行：**`tool_call` 先抢到锁不仅不会出错，反而正是设计想要的行为**。下面从源码出发逐层解释。

---

## 目录

- [1. 常见困惑的澄清](#1-常见困惑的澄清)
- [2. 并发从哪来：单条 Run 内部的三个写入源](#2-并发从哪来单条-run-内部的三个写入源)
- [3. 锁到底在保护什么](#3-锁到底在保护什么)
- [4. tool_call 先抢到锁的完整分析](#4-tool_call-先抢到锁的完整分析)
- [5. 三种竞争情形的时序图](#5-三种竞争情形的时序图)
- [6. 关键不变量](#6-关键不变量)
- [7. 常见疑问](#7-常见疑问)
- [8. 相关源码位置](#8-相关源码位置)

---

## 1. 常见困惑的澄清

### 1.1 “SSE 流”是只读投影，不是写入通道

很多开发者第一反应是：前端订阅的 SSE 流是有顺序的，所以写入端也应该天然顺序执行。但仓库里 SSE 的逻辑只是从 `run_events` 表做 replay/tail（`after_seq` / `Last-Event-ID`）。

`CommittedEventSink` 是**写入端**，它决定事件以什么顺序、什么粒度写进 `runtime.db`；SSE 只是读取已经落库的事件。因此并发问题不在 SSE，而在 engine 写入侧。

### 1.2 OpenAI 返回的流有顺序，但 Runtime 内部做了“异步聚合”

模型 provider 的流顺序是正确的：

```text
delta1 → delta2 → tool_call → delta3 → tool_result → ...
```

但 Runtime 为了性能，对 `text` 做了特殊处理：

- **text delta**：先攒到 buffer，按 `100ms / 2KiB` 聚合后再写库；
- **非 text 事件（tool_call、tool_result、plan_step、error 等）**：立即写库。

这就引入了一个时间差：`delta1` 和 `delta2` 可能被合并成一条 `OUTPUT_DELTA_COMMITTED`，而 `tool_call` 必须在它们之后落库。**聚合策略 + 立即提交策略之间需要同步**，这就是锁存在的原因。

---

## 2. 并发从哪来：单条 Run 内部的三个写入源

`CommittedEventSink` 内部有三个可能同时尝试写事件的协程入口，它们都共用同一个 `asyncio.Lock`。

### 2.1 主事件流：模型源源不断地产生事件

位置：`agent/runtime/application/events.py:130`

```python
async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
    ...
    if event_type in {"text", EventType.OUTPUT_DELTA_COMMITTED}:
        await self.emit_text(str(payload.get("delta", "")))
        return
    async with self._lock:
        await self._flush_locked()
        ...
```

引擎收到 `tool_call`、`tool_result`、`error`、`plan_step` 等非 text 事件时，会进入 `emit()` 的锁保护分支。

### 2.2 text 聚合后台定时器：`_flush_after_delay`

位置：`agent/runtime/application/events.py:211-221`

```python
async def _flush_after_delay(self) -> None:
    try:
        await asyncio.sleep(self.flush_ms / 1000)
        async with self._lock:
            self._timer = None
            await self._flush_locked()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        self._background_error = exc
```

`emit_text()` 在 text 未达 `flush_bytes` 时会创建一个后台 Task，100ms 后自动触发 flush。这个 Task 与主事件流是**并行**的。

### 2.3 text 达到阈值时的立即 flush

位置：`agent/runtime/application/events.py:180-192`

```python
async def emit_text(self, delta: str) -> None:
    ...
    async with self._lock:
        self._buffer.append(delta)
        self._buffer_bytes += len(delta.encode("utf-8"))
        if self._buffer_bytes >= self.flush_bytes:
            await self._flush_locked()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_after_delay())
```

当单个 text delta 使 buffer 超过 2KiB 时，主流程会立即 flush，不需要等 timer。

---

## 3. 锁到底在保护什么

`asyncio.Lock` 保护的是两类东西：

### 3.1 可变状态的一致性

- `_buffer`：待聚合的 text delta 列表
- `_buffer_bytes`：当前 buffer 的字节数
- `_timer`：后台 flush Task 的引用
- `_full_text`：完整回答的累积（用于最终拼接）

如果没有锁，下面这种情况就可能发生：

```text
主协程正在 _flush_locked() 里执行 "".join(self._buffer)
后台 timer 也刚好醒来执行 _flush_locked()
两者同时看到 buffer=["你好", "，我"]
两者都生成 OUTPUT_DELTA_COMMITTED "你好，我"
两者都 clear buffer
结果：同一段文本被提交两次，seq 推进混乱。
```

### 3.2 事件落库顺序

更重要的是，锁保证了**语义顺序**：

> **`tool_call` 落库之前，所有已经产生的 text delta 必须先落库。**

看 `emit()` 的锁内逻辑：

```python
async with self._lock:
    await self._flush_locked()          # 先把 text buffer 落库
    await self.store.append_events(...) # 再写 tool_call
```

这是“切换 message/Tool/checkpoint/terminal 前 flush”原则的具体实现。不加锁的话，`tool_call` 可能在积攒的 text 还没落库时就先写进去了，导致 SSE 客户端看到：

```text
TOOL_CALL_COMMITTED search
OUTPUT_DELTA_COMMITTED "你好，我"
```

这违反了模型输出的因果顺序。

---

## 4. tool_call 先抢到锁的完整分析

### 4.1 抢到锁后会发生什么

位置：`agent/runtime/application/events.py:142-178`

```python
async with self._lock:                       # tool_call 拿到锁
    await self._flush_locked()               # 先把 buffer 里已有的 text 刷到 DB
    ...
    await self.store.append_events(          # 再写 tool_call
        self.run_id,
        [EventDraft(EventType.TOOL_CALL_COMMITTED, dict(payload), ...)],
        ...
    )
```

`tool_call` 抢到锁后做的第一件事就是**强制 flush text buffer**，然后才写 `TOOL_CALL_COMMITTED`。

### 4.2 这会不会导致 text 丢失？

**不会。** `_flush_locked()` 只是把当前 buffer 里已有的 text 提交到 DB；buffer 被清空后，后续到达的 text delta 会 append 到新的 buffer，等待下一次 flush。

同时，`emit_text()` 内部的 `async with self._lock` 会让晚到的 text delta 在锁外等待。等 `tool_call` 写完释放锁后，它们才能进入 buffer。因此：

- `tool_call` 之前的 text → 被 `_flush_locked()` 带走；
- `tool_call` 之后的 text → 在锁外等待，进入新 buffer。

分界线就是 `tool_call` 这个事件本身，天然正确。

### 4.3 这会不会导致顺序错乱？

**不会，反而保证顺序正确。**

假设模型实际输出顺序是：

```text
"你好" → "，我" → tool_call("search") → "结果" → "如下"
```

如果 `tool_call` 先抢到锁，DB 里的事件顺序变成：

```text
OUTPUT_DELTA_COMMITTED "你好，我"      ← 被 tool_call 强制 flush
TOOL_CALL_COMMITTED search
OUTPUT_DELTA_COMMITTED "结果如下"      ← 后续 text 继续聚合后 flush
```

完全符合模型语义。

### 4.4 唯一的“副作用”

`tool_call` 可能会让一次 text 聚合**提前结束**。例如：

- 原本 100ms 内能攒够一段 3KiB 的文本，合并成一次 `OUTPUT_DELTA_COMMITTED`；
- 中间插进来一个 `tool_call`，只能先把 tool_call 之前的 0.5KiB flush 出去。

这只是**聚合粒度变小**，不影响语义顺序和事件完整性。

---

## 5. 三种竞争情形的时序图

### 情形 A：text buffer 达到阈值，主流程自己 flush

```text
时间轴 ──────────────────────────────────────────────▶

主事件流：emit_text("delta1") → emit_text("delta2") → emit("tool_call")
                │                     │                      │
                ▼                     ▼                      ▼
            append to           append to, bytes>=2KiB   async with _lock:
            buffer                 _flush_locked()         _flush_locked()
                                                          （发现 buffer 已空）
                                                          append_events(TOOL_CALL)
```

结果：`tool_call` 进去时发现 buffer 已空，直接写 `TOOL_CALL_COMMITTED`。**正确**。

### 情形 B：后台 `_timer` 先抢到锁

```text
时间轴 ──────────────────────────────────────────────▶

主事件流：emit_text("delta1") → emit_text("delta2") ─┬─▶ emit("tool_call")
                │                     │              │        │
                ▼                     ▼              │        ▼
            start timer            timer 还在跑      │    async with _lock:
                                                     │    （挂起等待）
后台 timer：                                         ▼
                                          100ms 后醒来
                                          async with _lock:
                                          _flush_locked()
                                          写 OUTPUT_DELTA_COMMITTED
                                          释放锁
                                                     │
                                                     ▼
                                              主事件流获得锁
                                              _flush_locked() 看到 buffer 空
                                              写 TOOL_CALL_COMMITTED
```

结果：timer 先 flush text，然后 `tool_call` 再落库。**正确**。

### 情形 C：`tool_call` 先抢到锁

```text
时间轴 ──────────────────────────────────────────────▶

主事件流：emit_text("delta1") → emit_text("delta2") ─┬─▶ emit("tool_call")
                │                     │              │        │
                ▼                     ▼              │        ▼
            start timer            timer 还在跑      │    async with _lock:
                                                     │    _flush_locked()
                                                     │    写 OUTPUT_DELTA_COMMITTED "delta1+delta2"
                                                     │    写 TOOL_CALL_COMMITTED
                                                     │    释放锁
后台 timer：                                         │
                                          100ms 后醒来   │
                                          async with _lock:│
                                          （buffer 已空，无事可做）
                                                     │
                                                     ▼
                                              后续 emit_text("delta3")
                                              append 到新 buffer
```

结果：`tool_call` 强制把前面 text flush 后再写自己。**正确**。

---

## 6. 关键不变量

| 不变量 | 说明 |
|---|---|
| **先 commit，后 SSE 可见** | 所有事件先写入 `run_events` 表（同一事务更新 `runs.next_seq`），SSE 只读已提交事件。 |
| **text 聚合，非 text 即时** | text 按 100ms/2KiB 聚合；tool_call 等事实事件立即落库。 |
| **切换事件类型前必须 flush** | 写 tool_call/tool_result/plan_step/terminal 前，必须先 `_flush_locked()` 清空 text buffer。 |
| **单 Run 内写入串行化** | 所有对 `_buffer` / `_timer` / `store.append_events` 的访问必须通过同一把 `asyncio.Lock`。 |
| **fencing token 保证所有权** | `store.append_events` 带 `fencing_token`，旧 Worker 的结果即使抢到锁也会被拒绝。 |

---

## 7. 常见疑问

### Q1：那把锁会阻塞模型流很久吗？

不会。`_flush_locked()` 做的是内存拼接 + 一次 SQLite `append_events`，通常在毫秒级。text 聚合阈值（2KiB）和 timer（100ms）也保证了 buffer 不会特别大。

### Q2：如果 `tool_call` 来的时候 buffer 本来就是空的，`_flush_locked()` 不是白执行吗？

是的，但这是正确的“白执行”。看 `_flush_locked()` 开头：

```python
async def _flush_locked(self) -> None:
    if not self._buffer:
        return
    ...
```

空 buffer 直接返回，成本极低。这种设计的好处是调用方不需要判断 buffer 是否为空，逻辑统一。

### Q3：`emit_text` 里也加了同一把锁，那 text delta 之间也会互相阻塞吗？

会短暂串行，但这是必要的。每个 text delta 都要修改 `_buffer`、`_buffer_bytes`、可能创建 `_timer`，这些操作不能交错。不过由于每个 delta 的处理极快，实际不会形成瓶颈。

### Q4：这把锁能防多个 Worker 同时写一个 Run 吗？

**不能，也不负责这个。** 多 Worker 写同一 Run 的防护在 `store.append_events` 的 `fencing_token` 里。`CommittedEventSink` 的锁只解决**单 attempt 内部多个协程**的并发问题。

---

## 8. 相关源码位置

- `agent/runtime/application/events.py:55` —— `CommittedEventSink` 类定义
- `agent/runtime/application/events.py:130` —— `emit()` 主入口
- `agent/runtime/application/events.py:142` —— 非 text 事件加锁分支
- `agent/runtime/application/events.py:180` —— `emit_text()` text 聚合入口
- `agent/runtime/application/events.py:211` —— `_flush_after_delay()` 后台 timer
- `agent/runtime/application/events.py:222` —— `_flush_locked()` 实际 flush 逻辑
- `agent/runtime/application/coordinator.py:241` —— `CommittedEventSink` 创建位置
- `agent/runtime/application/events.py#L246` —— `_raise_background_error()` 后台错误传播

---

## 总结

> `tool_call` 先抢到锁不会导致 text 丢失或顺序错乱，反而会把已有 text 强制落库后再写 `tool_call`——这正是“切换事件类型前必须 flush”的设计目的。
>
> 锁的存在不是因为 SSE 或多用户并发，而是因为单条 Run 内部有**多个 asyncio 协程**（主事件流、100ms 后台 timer、阈值 flush）在同时操作共享 buffer 和 DB。锁保证了 buffer 状态一致，并强制 text 事件在 tool_call 等事实事件之前落库。
>
> 谁先抢到锁并不重要，重要的是：任何非 text 事件落库前，都必须先完成 `_flush_locked()`。
