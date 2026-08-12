# NativeLoop LLM 流式调用全链路详解

> 本文以当前代码为准，说明 `native_loop` 从 Runtime 领取一次 Activity，到模型流、工具批次、checkpoint 和最终 Assistant 提交的完整链路。重点不是 OpenAI SDK 的调用语法，而是“哪些事实何时成为可恢复的权威状态”。

## 1. 先建立正确的分层图

`native_loop` 生产路径分为三层：

```text
RunCoordinator
  └─ NativeLoopAdapter.execute(EngineRunRequest, RuntimeIO)
       ├─ 编译 canonical history / current input / 附件
       ├─ 解码唯一 current checkpoint，重物化大 ToolResult
       ├─ 绑定强制 Tool Broker 和 RuntimeIO 回调
       └─ NativeLoop.run(initial_state=state)
            └─ NativeLlmClient.stream(...)
                 └─ OpenAI-compatible provider stream
```

三层职责不能混淆：

- `NativeLoopAdapter` 是生产级 EngineAdapter，直接接收 `RuntimeIO`。它负责持久化、fencing、绝对 deadline、cancel、Broker、Artifact 和最终消息指定。
- `NativeLoop` 是 Runtime-independent kernel，持有扁平 `messages` 和 `LoopState`，负责模型—工具循环的语义顺序。它通过窄回调接入 checkpoint、Broker 和控制探针，因而仍可供 Claude Skill 子 Runner 复用。
- `NativeLlmClient` 是唯一直面 provider 线协议的组件，它将 chunk 解析为 `TextDelta | ToolCallReady | TurnEnd`，不决定 Runtime 终态。

kernel 的入口也已收紧为“恰好一个状态来源”：普通可复用调用传 `messages=...`，生产 Adapter/恢复路径传 `initial_state=...`；两者都不传或同时传都立即 `ValueError`。这避免“messages 与 checkpoint state 到底谁覆盖谁”的隐式选择。

Native 不经过 ADK 的 `ReasoningEngine`、后台无界队列或事件 merge 路径。`plan_execute` 和 `agent_loop` 仍走 ADK Adapter，两条路径是隔离的。

## 2. 主要源码导航

| 职责 | 文件 / 入口 |
|---|---|
| 生产 Adapter | `agent/engine/native_loop/engine.py` 的 `NativeLoopAdapter.execute` |
| 自研 while 循环 | `agent/engine/native_loop/loop.py` 的 `NativeLoop.run` |
| provider 流协议 | `agent/engine/native_loop/llm_client.py` 的 `NativeLlmClient.stream/_consume` |
| 消息与原子单元 | `agent/engine/native_loop/messages.py` |
| current checkpoint | `agent/engine/native_loop/checkpoint.py` |
| 工具执行与并发分组 | `agent/engine/native_loop/executor.py` |
| Native 工具目录 | `agent/engine/native_loop/tools.py` |
| Native—Broker 绑定 | `agent/runtime/adapters/brokered_tools.py` |
| RuntimeIO 端口 | `agent/runtime/ports/engine.py` |
| committed event sink | `agent/runtime/application/events.py` |

本文不写固定行号：这些不变量比行号更值得记住。

## 3. Adapter 进入循环前做了什么

### 3.1 编译本次模型输入

`_compile_input` 从 `EngineRunRequest` 构造 Native `Msg`：

1. 将 committed canonical history 转成 `user/assistant` 消息。
2. 将当前 `input_text` 放入最后一条 user 消息。
3. 图片附件从已校验 Artifact CAS 分段读取，转为多模态 `image_url` block。
4. 非图片附件只嵌入有界 preview，如需全文由模型调用 `read_artifact`。

当 `request.checkpoint is None` 时，才从这批消息初始化 `LoopState`。只要 checkpoint 存在，就必须用 current codec 严格恢复，不会猜字段、降级或回退到 history 重跑。

### 3.2 恢复大 ToolResult

checkpoint 不复制大结果正文，而是保存 `tool_execution_id`引用。Adapter 恢复时扫描全部历史位置，通过：

```text
ToolBroker.materialize_committed_result(tool_execution_id, ...)
  → ToolExecution ledger / Artifact authority
  → 重建模型可见的 role=tool content
```

这使跨多轮、任意位置的大结果都可恢复，而不会撑爆 checkpoint。

### 3.3 注入 Runtime 回调

Adapter 将以下窄能力交给 kernel：

- `checkpoint` → 编码 current state，用 revision CAS 保存，可与 engine-owned events 同事务。
- `prepare_tool_batch` → Broker 原子 PREPARE 完整稳定 slot 批次。
- `run_tool_calls/execute_tool` → 只执行 Broker 绑定后的工具。
- `control_probe` → 检查 Runtime cancel 与绝对 deadline。
- `message_id_factory` → 基于 Run 和 model ordinal 生成稳定 model message slot。

Skill UI 也使用 Native 专用 awaited sink：每个 frame 先等待 `RuntimeIO.emit`。`skill_event` 是非 text 事件，Sink 会先 flush 旧 text，再将该 frame 提交后返回。

## 4. 每个 model turn 的提交顺序

默认 `native_early_tool_dispatch=off` 时，一轮的生产顺序是：

```text
control probe / hard cap / proactive compact
  → 预留 model_call_count 与 generation
  → [同事务] MODEL_REQUEST checkpoint
                  + OUTPUT_GENERATION_STARTED event
  → 发起 provider stream
  → 每个 TextDelta: await RuntimeIO.emit(text)
  → 收到唯一显式 finish marker
  → 校验完整 ToolCall batch
  → MODEL_RESPONSE_COMMITTED checkpoint
  ├─ stop + 非空正文
  │    → COMPLETED checkpoint
  │    → set_final_assistant(...)
  │    → EngineOutcome.COMPLETED
  └─ tool_calls + 完整 calls
       → Broker PREPARE 全批 stable slots
       → TOOL_BATCH_COMMITTED checkpoint
       → 受控执行工具
       → 按 call ordinal 逐个 TOOL_RESULT_COMMITTED
       → NEXT_TURN checkpoint
       → 下一次 model turn
```

关键提交屏障是：

- `MODEL_REQUEST` 落盘之前不能请求 provider；因此崩溃不能绕过 model-call 硬上限。
- 默认模式下，显式 finish 之前工具执行计数必须为零。
- 完整 batch PREPARE 失败不能暴露半批 slot。
- `TOOL_BATCH_COMMITTED` 之前不允许默认生产路径 dispatch。
- 外部 READ_ONLY 工具可并发，但 Broker 结算、模型中的 ToolResult 顺序与 checkpoint 始终按 call ordinal 稳定。

## 5. generation：为什么不能只有 text delta

模型流可能在输出了部分正文后崩溃。恢复时不能把新 delta 接在旧半截正文后面，因此每次真正拉流前都建立 generation：

```json
{
  "message_id": "稳定的 model slot",
  "generation_id": "本次尝试唯一 generation",
  "supersedes_generation_id": "被取代的 generation 或 null",
  "reason": "initial | next_turn | recovery | reactive_compact"
}
```

kernel 将内部 `output_generation_started` 转为 Canonical Event `OUTPUT_GENERATION_STARTED`，SSE 投影名为 `text_start`。每个 `text` delta 必须同时带 `message_id` 和 `generation_id`。

UI 收到 `text_start` 时只清空当前回答正文，不清除工具、Skill 或计划过程卡片。最终 `assistant_message` 是权威覆盖；fresh replay 和断线续传都用同一规则重建。

`message_id` 表示语义上的 model slot，`generation_id` 表示该 slot 的一次实际生成尝试。恢复 `MODEL_REQUEST` 时 message slot 不变，但 generation 必须换新。

## 6. `await RuntimeIO.emit` 保证什么，又不保证什么

Adapter 不再为每个事件创建一个 pull task，而是为整个 attempt 建立一个 `native-stream-pump`。pump 与主消费协程之间只有一个 `_StreamEnvelope` slot，并用 acknowledge event 交接：

```text
provider 吐出 delta
  → kernel yield text
  → pump 放入单 slot，等待 acknowledged
  → Adapter 主协程 await io.emit
  → emit 返回后才 acknowledge
  → pump 才能继续 anext(kernel/provider)
```

这是有界反压和因果顺序机制。如果 `io.emit` 被阻塞，kernel 不能再拉 provider item，因而也不能越过它进入后续 checkpoint、PREPARE、dispatch 或完成。单 slot 还保证不存在随 delta 数量增长的进程内 queue。

但要精确区分 **awaited admission** 和 **durable commit**：对一个未达阈值的 text delta，`io.emit` 可以只是在 Sink 锁内收入有界 buffer 并安排 timer，不意味着这一帧已单独写入 SQLite。真正的 durable flush 条件是：

- buffer 达到 2 KiB；
- 100 ms timer 到期；
- message/generation identity 切换；
- 任何非 text 事件；
- checkpoint（其内部先 `force_flush`）；
- attempt `close()`；
- 其他明确需要顺序屏障的调用方主动 `force_flush()`。

Native Adapter 现在**不再对每个 text delta 调用 `force_flush()`**。这保留了冻结协议的 100 ms / 2 KiB 聚合语义，同时 checkpoint/非 text/close 保证不会跨语义边界滞留。测试会用大量小 delta 验证 `force_flush` 和 cancel 查询次数不随 delta 线性增长。

## 7. Provider 流协议：只接受显式完成

### 7.1 请求形状

`NativeLlmClient.stream` 构造 OpenAI-compatible payload：

- `messages` 由 `Msg.to_wire` 转换。
- `tools` 来自启动期已严格校验的 `ToolRegistry.wire_declarations()`。
- `stream=True`，如 provider 支持则请求 usage。
- 显式关闭 provider thinking 正文混入。

provider 不支持 `stream_options` 时，只允许在尚未产出任何 item 前去掉 usage 选项重试一次。已经 emit 后绝不重试，否则会复制正文。

### 7.2 `_consume` 的严格规则

provider 流必须满足：

- 仅一个 choice，且 choice index 只能是 0。
- text delta 必须是字符串。
- ToolCall fragment 必须有非负整数 index；id/name/arguments 如果出现必须是字符串。
- ToolCall index 在结束时必须从 0 连续。
- id/name 不能在后续 fragment 变更，完整 batch 内 id 必须唯一。
- 必须看到一次显式 finish marker，finish 之后不能再有 choice data。
- usage-only chunk 可用于记录 usage，但不能独自证明 turn 完成。

流完成只能是下列两种语义：

| finish reason | 必须搭配 | 禁止 |
|---|---|---|
| `stop` | 非空最终正文 | 任何 ToolCall |
| `tool_calls` | 至少一个完整 ToolCall | 空 batch |

`length`、`content_filter`、未知 reason、reason 与 batch 矛盾都是 `MODEL_PROTOCOL_INVALID`。`stop` 但正文空是 `MODEL_EMPTY_FINAL_RESPONSE`。

零 chunk、只有 usage、自然 EOF、缺 finish marker 都是 `MODEL_STREAM_INCOMPLETE`。这一类是 Adapter 唯一明确转为 `RETRYABLE_FAILURE` 的流协议错误；它不会被合成为一个伪 `TurnEnd`。

### 7.3 输出与参数体积限制

所有大小都按 UTF-8 bytes 计算：

| 边界 | 默认值 | 错误码 |
|---|---:|---|
| 每 generation 模型输出 | 1 MiB | `MODEL_OUTPUT_LIMIT_EXCEEDED` |
| 每轮 ToolCall 数 | 64 | `TOOL_CALL_LIMIT_EXCEEDED` |
| 每 Run ToolCall 数 | 256 | `TOOL_CALL_LIMIT_EXCEEDED` |
| 单 ToolCall arguments | 64 KiB | `TOOL_ARGUMENTS_TOO_LARGE` |
| 整批 arguments | 256 KiB | `TOOL_BATCH_TOO_LARGE` |
| checkpoint | 2 MiB | `CHECKPOINT_TOO_LARGE` |

模型调用硬上限是 `max_loop_iters + 2`，默认 10。计数在请求前的 checkpoint 中预留，崩溃不退还。

## 8. 工具提前派发与工具流式展示不是一件事

`native_early_tool_dispatch` 有三个值：

### 8.1 `off`：生产默认

- 模型流未完成时，`ToolCallReady` 不会被提前产出。
- 收到显式 finish 后才得到完整 batch。
- 正文仍逐 delta 流式展示。
- 工具运行期间的 Skill/Claude Skill 进度 frame 仍实时提交。
- 完整 batch 后，经评审的 READ_ONLY、`concurrency_safe=true`、无独占资源工具仍可受控并发。

所以，关闭“提前派发”并不等于关闭正文流式输出，也不等于关闭工具运行过程的 UI 流。

### 8.2 `experimental_heuristic`：显式实验模式

累积器只在“已出现更高 index + 当前 arguments 可解析为 JSON object”时将前一个调用视为启发式就绪。就绪调用还必须满足：

- 工具是已评审 READ_ONLY。
- `concurrency_safe=true` 且无 `exclusive_resources`。
- 参数通过 JSON Schema 校验。
- 执行前先通过 Broker 建立当前 stable slot 和 ToolCall 事实。

调度器使用固定 worker 数与有界 queue，`submit()` 本身会把反压传回 provider 迭代。不会为每个 call 无界 `create_task`。cancel、EOF、异常或 GeneratorExit 都必须取消并 await worker/future。

一旦提前派发后 provider 又追加该 index 的 id/name/arguments fragment，立即以 `TOOL_REPLAY_MISMATCH` 失败关闭。已完成的只读执行可被浪费，但不能被重解释为另一个调用。该模式不属于默认生产可靠性承诺。

### 8.3 `provider_block_complete`：未来协议位

只有 provider adapter 明确声明 `tool_call_block_complete` 能力时才能使用。当前 OpenAI-compatible adapter 返回 `False`，所以 Worker 在 release 激活前就以 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE` 启动失败，kernel 内还有一层防御。

## 9. ToolCall 校验失败与 Runtime 故障必须分开

模型生成了完整但不可执行的 batch 时：

- 工具不存在 → 对应 call 得到 `TOOL_NOT_FOUND`。
- arguments 不是 JSON object 或不符合 schema → 对应 call 得到 `TOOL_ARGUMENTS_INVALID`。
- 同 batch 其他 call → `TOOL_BATCH_REJECTED`。

默认 `off` 模式下整批零 dispatch，kernel 生成按顺序配对的 call/result，并在 `NEXT_TURN` checkpoint 中原子保存，让模型下轮自我修正。`experimental_heuristic` 可能已逐 slot PREPARE 并执行安全 READ_ONLY 前缀；发现完整 batch 非法后会停止后续派发并 fail-closed，已完成的只读执行只能被浪费，不能被重解释成别的调用。

但下列错误不能伪装成普通 ToolResult 喂回模型：

- stable slot 漂移：`TOOL_REPLAY_MISMATCH`。
- ToolResult/Evidence 契约错误。
- stale fencing、lease loss、checkpoint CAS 冲突等 attempt ownership 故障。

这些是 Runtime 控制决策，必须原样冒泡给 Worker/Coordinator，否则模型可能“绕过”权威边界后错误完成 Run。

Native 工具执行层因此只将普通工具异常转成模型可见失败；`RuntimeFault` 和 `AttemptOwnershipLost` 都直接透传。正常 ToolCall 事实由 Broker PREPARE 提交，kernel 不再保留生产无调用点的 `_call_events`；只有整批零 dispatch 的模型可修正错误由 `_synthetic_events` 生成成对 call/result，普通结果投影则经 `_result_events`。

## 10. cancel、deadline 与资源清理

Native 同时在两个粒度检查控制信号：

1. kernel 在恢复工具、预留 model request 等安全边界调用 `control_probe`。
2. Adapter 为整个 attempt 建立一个 `native-cancel-watch`，以固定短周期查 `is_cancelled()`；它与 delta 数量无关。绝对 deadline 由包住整个消费循环的 `asyncio.timeout(remaining_ms)` 监督。

cancel watcher 观察到取消时会设置 control error、停止后续 pull，取消 pump 并唤醒主消费协程。pump 错误、watcher 错误和正常 EOF 都通过同一个单 slot 边界交给主协程，不需要每个 event 新建 task 或查一次 SQLite。

所有下游 timeout 使用同一个 Run 绝对 deadline 的剩余预算，不在每层重新起钟。任何 cancel、deadline、GeneratorExit、Adapter 异常或 ownership loss 都必须：

- `aclose()` provider/kernel stream；
- 取消并 await early scheduler worker/future；
- 让 Broker/HTTP/Skill 子进程获得取消；
- 不把旧 fencing attempt 的结果写成 Run 终态。

Adapter 本身只返回 `EngineOutcome`，无权直接写 Run。正常 EngineOutcome 由 Coordinator 收口；cancel、deadline、recovery/reconciliation 命令则可由 Store 在权威事务中直接提交 terminal。生成器 EOF、内部 error event 或 SSE 断开都不是终态事实。

## 11. Checkpoint 恢复对流式语义的影响

current checkpoint 只有六个 phase：

```text
MODEL_REQUEST
MODEL_RESPONSE_COMMITTED
TOOL_BATCH_COMMITTED
TOOL_RESULT_COMMITTED
NEXT_TURN
COMPLETED
```

与流式相关的恢复规则：

- `MODEL_REQUEST`：上一次 provider 流没有完整提交。保留稳定 message slot，新建 `reason=recovery` generation；已消耗 model-call 预算不退还。
- `MODEL_RESPONSE_COMMITTED`：如果尾部是无 ToolCall 的非空 assistant，Adapter 直接写 `COMPLETED` 并指定精确 final assistant，不再请求模型。如果是 ToolCall batch，继续 PREPARE/恢复工具。
- `TOOL_BATCH_COMMITTED` / `TOOL_RESULT_COMMITTED`：重建 attempt-local 关联，Broker 复用已提交结果，只补齐未配对后缀。
- `NEXT_TURN`：上一批已完整配对，可预留下一个 model request。
- `COMPLETED`：恢复精确 `final_text/message_id/generation_id`，不重放 delta、不重请求 provider。

详细 phase 不变量见 checkpoint 代码与本目录的 Runtime 持久化阅读文档。

## 12. 一张端到端时序图

```mermaid
sequenceDiagram
    participant C as Coordinator
    participant A as NativeLoopAdapter
    participant K as NativeLoop kernel
    participant R as RuntimeIO
    participant L as NativeLlmClient
    participant P as Provider
    participant B as Tool Broker

    C->>A: execute(request, io)
    A->>A: compile input / decode checkpoint
    A->>K: run(initial_state=state)
    K->>R: checkpoint MODEL_REQUEST + generation-start event
    R-->>K: committed revision
    K->>L: stream(messages, tools)
    L->>P: streaming request
    loop each text delta
        P-->>L: chunk
        L-->>K: TextDelta
        K-->>A: single-slot text envelope
        A->>R: await emit (admission/order barrier)
        R-->>A: admitted; threshold/timer may commit batch
        A-->>K: acknowledge; allow next pull
    end
    P-->>L: explicit finish=tool_calls
    L-->>K: complete ToolCalls + TurnEnd
    K->>R: checkpoint MODEL_RESPONSE_COMMITTED
    K->>B: atomic PREPARE complete batch
    B-->>K: stable slots
    K->>R: checkpoint TOOL_BATCH_COMMITTED
    K->>B: execute prepared calls
    B-->>K: ordinal-settled results
    K->>R: TOOL_RESULT_COMMITTED checkpoints
    K->>R: NEXT_TURN checkpoint
```

## 13. 阅读代码时要带着的不变量

1. 没有显式 finish marker，就没有完整 turn。
2. `stop` 只能对应非空最终正文；`tool_calls` 只能对应完整调用批次。
3. 默认 `off` 下，模型 finish 前零工具执行。
4. 每个 delta 必须经过 awaited RuntimeIO admission/顺序边界，才能继续拉流；durable text 由 100 ms / 2 KiB 与语义 flush 边界决定。
5. model request 先预留 checkpoint，工具先建 stable slot 并提交 batch，再执行。
6. 外部执行可并发，权威结算和模型可见顺序必须稳定。
7. generation 允许审计旧 partial，final assistant 只能指向最后一个完整、非空、无 ToolCall 的 assistant turn。
8. cancel、deadline、lease/fencing/CAS 冲突不能被翻译成模型可修正的 ToolResult。
9. Engine/Adapter 不能直接终态化 Run；正常 EngineOutcome 由 Coordinator 裁决，Store 的 cancel、deadline、recovery/reconciliation 命令路径保留直接终态事务。

## 建议的实际阅读顺序

```text
agent/runtime/ports/engine.py
  → agent/engine/native_loop/engine.py
  → agent/engine/native_loop/loop.py
  → agent/engine/native_loop/llm_client.py
  → agent/engine/native_loop/checkpoint.py
  → agent/runtime/adapters/brokered_tools.py
  → agent/runtime/application/events.py
```

这个顺序先看权威边界，再看算法细节，更容易理解为什么 Native 的流式实现不是“边收 token 边扔进队列”这么简单。
