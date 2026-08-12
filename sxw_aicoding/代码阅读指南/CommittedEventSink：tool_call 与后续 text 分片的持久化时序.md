# CommittedEventSink：ToolCall 与后续 text 分片的持久化时序

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

本文专门回答一个时序问题：已有 text 在 buffer 中，中间出现 ToolCall，随后又来了 text，新旧分片会怎么入库？

## 1. 最短结论

ToolCall 是一个不可跨越的提交边界：

```text
ToolCall 前已交给 Runtime 的 text
  → 先 flush 为 OUTPUT_DELTA_COMMITTED
  → 再提交 ToolCall 事实

ToolCall 后的 text
  → 进入新 buffer/generation
  → 在后续阈值、timer、checkpoint 或 close 边界提交
```

这不会丢失 text，也不会把 ToolCall 的 JSON 拼进助手文本。它只可能让 text 聚合粒度变小。

## 2. 需要先区分两类 ToolCall

### 2.1 Broker-owned 正常工具调用

这是生产主路径。`ToolBroker.prepare_batch()` 会让 Store 在一个事务内：

- 预检整个稳定 slot batch；
- 新建 Tool Activity 和 ToolExecution；
- 写入 `TOOL_CALL_COMMITTED`；
- 如任一旧 slot 与新的 tool/request/release/effect 不一致，整批回滚。

工具完成后，Store 在另一个结算事务内更新 effect/activity/result/artifact link，并写 `TOOL_RESULT_COMMITTED`。

这些事件不进入 Sink 重复写入。text 与 ToolCall 的顺序由 Tool Broker 之前的 flush/checkpoint 屏障保证。

Native kernel 中曾经可用来生成普通 ToolCall 投影的 `_call_events` 已删除。生产主路不再留一个与 Broker 并行的 call 写入口。

### 2.2 engine-owned 合成工具事件

默认 `native_early_tool_dispatch=off` 时，如果模型给出不合法参数或不存在的工具，整批必须零 dispatch。此时不创建真实 ToolExecution，但仍需要向模型和 UI 提交成对 call/result。Native 把这组合成事件作为 `NEXT_TURN` checkpoint 的 `events` 一起原子提交。实验模式可能在完整 batch 校验前已逐 slot 执行安全 READ_ONLY 前缀；随后会停止派发并 fail-closed，已完成执行只会被浪费。

这是 engine-owned tool event 的主要现实用途，不能与已派发工具的 Broker 账本混为一谈。

## 3. Native 默认 `off` 模式的真实时序

Native 现在直接实现 `EngineAdapter.execute(request, RuntimeIO)`，不经过 ADK queue/merge。默认 `native_early_tool_dispatch=off` 时，一个有工具的 model turn 时序是：

```text
1. save MODEL_REQUEST checkpoint
   + 同事务提交 OUTPUT_GENERATION_STARTED

2. provider 输出 text delta
   → attempt-level pump 放入单 slot
   → NativeLoopAdapter await io.emit(text)
   → emit 返回后 acknowledge，才能拉下一帧
   → text 按 100 ms / 2 KiB 或语义边界聚合持久化

3. provider 显式 finish=tool_calls
   → 验证完整 batch
   → save MODEL_RESPONSE_COMMITTED checkpoint
   → checkpoint 内部先 flush 所有仍缓冲的 text

4. Broker 单事务 PREPARE 全部 slots
   → ToolExecution / Tool Activity / TOOL_CALL_COMMITTED 一起提交

5. save TOOL_BATCH_COMMITTED checkpoint
   → 到这之后才允许 dispatch

6. 外部执行可按 effect policy 受控并发
   → Broker 按 call ordinal 结算 TOOL_RESULT_COMMITTED
   → save TOOL_RESULT_COMMITTED checkpoint

7. 全批配对后 save NEXT_TURN
   → 下一次 model generation
```

因此，当前 Native 默认路径中，ToolCall 不会在 provider 还在产生可变 fragment 时偷跑到 text 前面。

注意：`await io.emit(text)` 是 admission/顺序与 no-next-pull 屏障，不是“每帧单独已 durable”。对未达阈值的小帧，Sink 可先放入有界 buffer；但 kernel 要进入 `MODEL_RESPONSE_COMMITTED` 必须调用 checkpoint，而 checkpoint 会先 `force_flush()`。所以 Broker PREPARE 看到的序列仍一定是“所有模型 text 已持久 → ToolCall 事实提交”，同时又不牺牲 delta 聚合。

## 4. “后续 text”其实属于哪个 generation

常见例子：

```text
中间正文 → ToolCall → ToolResult → 最终正文
```

在 Native loop 中，ToolResult 之后的“最终正文”是下一次 model turn，会先提交新的 `OUTPUT_GENERATION_STARTED`，并使用对应 generation identity。它不会继续拼入工具前那个 generation 的 buffer。

客户端收到 `text_start` 时会清空当前回答正文，但保留 Tool/Skill/plan 过程卡片；后续 delta 重建新 generation 的正文。

最终 `ASSISTANT_MESSAGE_COMMITTED` 再以完整 text 权威覆盖客户端拼接结果。所以“中间正文”可以保留在审计事件中，但不会错误进入 Conversation history 的最终 Assistant。

## 5. Sink 内部的竞争时序

对任何真正进入 `emit()` 的非 text 事件，可以用下图理解：

```text
主流程      emit_text("A") ─────────▶ emit(non-text) ───▶ emit_text("B")
                     │                       │
Sink lock           ├─ append buffer         ├─ acquire
                     │                       ├─ flush "A"
100ms timer          └─ scheduled             ├─ cancel timer
                                             ├─ append non-text
                                             └─ release

最终 DB seq    OUTPUT_DELTA("A") → non-text event → OUTPUT_DELTA("B")
```

如果 timer 先拿锁，它先 flush A，非 text 事件随后看到空 buffer 并入库；如果非 text 事件先拿锁，它自己 flush A 再入库。两种 interleaving 得到相同因果顺序。

## 6. 不能从 SSE 反推的事情

SSE 只是 `run_events` 的已提交公开投影：

- 它不是 Engine 到 Runtime 的写入通道；
- heartbeat 没有 seq，不代表业务状态改变；
- INTERNAL 事件会导致公开 seq 看起来跳号，不代表丢事件；
- SSE 断开不取消 Run；重连按 `after_seq` / `Last-Event-ID` 读取后续已提交事件。

## 7. 源码阅读索引

- `agent/runtime/application/events.py`：text buffer、锁、flush、checkpoint。
- `agent/engine/native_loop/loop.py`：Native checkpoint phase 与 generation 时序。
- `agent/engine/native_loop/engine.py`：直接 awaited RuntimeIO 和 final assistant override。
- `agent/runtime/application/tool_broker.py`：`prepare_batch()` / `execute_prepared()`。
- `agent/runtime/adapters/sqlite/store.py`：ToolExecution batch PREPARE 与结算事务。
- `agent/runtime/api/runs.py`：Canonical Event 到 SSE event name 的投影。
