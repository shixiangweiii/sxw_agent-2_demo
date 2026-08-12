# ADR-0004：Engine Adapter 与 Native Direct Recovery

- 状态：Accepted / Frozen
- 日期：2026-08-12
- 替代：2026-08-09 版 Native 间接 Adapter 语义

## Context

可靠 Runtime 必须统一终态、事件和 checkpoint 权威，同时诚实保留不同引擎的恢复粒度。原 Native 生产路径先产生内部流，再经后台 merge queue 写 Runtime，会让 kernel 在前序事件尚未 committed 时 checkpoint、派发工具或返回完成。Native 必须直接拥有 Runtime 提交屏障，不再经过 ADK 兼容面。

## Decision

### 1. 统一公开契约

```text
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

`RuntimeIO` 暴露 committed EventSink、checkpoint CAS/与 engine-owned events 的原子提交、强制 Tool Broker、Artifact、显式 final assistant、Clock、绝对 deadline/剩余预算和 cancel probe。`EngineOutcome` 仅为：

```text
COMPLETED | RETRYABLE_FAILURE | TERMINAL_FAILURE |
WAITING_INPUT | CANCELLED
```

Engine 不能写 Run terminal；唯一 terminal 裁判是 RunCoordinator。EOF、旧 `done/error` 或 generator 正常退出不参与裁决。

### 2. Adapter 分工

- `AdkEngineAdapter` 只组装 `plan_execute|agent_loop`；二者的 ADK 内部循环、attempt-local Session/Artifact 和原有 queue/merge 路径保持不变。
- `NativeLoopAdapter` 直接负责 canonical history/current input/附件编译、strict checkpoint 恢复、kernel 驱动、RuntimeIO 提交、Tool Broker 调度和 final assistant 指定。
- Native 生产路径不经过 `ReasoningEngine`、attempt-local engine outcome、后台无界 queue、merge runner events 或 authority route。
- Native kernel 保持 Runtime-independent，仍可供 Claude Skill 子 Runner 使用；RuntimeIO/Broker 只存在生产 Adapter 和窄回调中。
- Native Skill UI 帧使用 awaited sink：每帧 committed 后才继续读下一帧，形成自然背压。

### 3. Canonical history 与 final assistant

每个 attempt 的模型输入只由以下 committed facts 编译：

- conversation 的全部 `USER_MESSAGE_COMMITTED`；
- 仅属于成功 Run 的 `ASSISTANT_MESSAGE_COMMITTED`；
- 当前 Run 的唯一 current checkpoint；
- 当前 transition 必需的续推/收口指令。

失败 partial delta、Trace、SSE、旧 Session 或进程级 native History 不进入历史。Native 必须通过 `RuntimeIO.set_final_assistant(text, message_id, generation_id)` 指定语义 final；只有最后一个完整、非空、无 ToolCall 的 assistant turn 进入 conversation history。前面轮次的文本仅作显示过程。ADK adapter 未设置显式 final 时保留现有累计文本行为。

### 4. Native current checkpoint

Native 只接受一个 strict typed codec，phase 固定为：

```text
MODEL_REQUEST
MODEL_RESPONSE_COMMITTED
TOOL_BATCH_COMMITTED
TOOL_RESULT_COMMITTED
NEXT_TURN
COMPLETED
```

`checkpoint is None` 时才可从 canonical history 初始化。checkpoint 存在但缺字段、多字段、phase 未知、role 非法、call id 重复、call/result 不配对或 phase 与消息矛盾时直接 `NATIVE_CHECKPOINT_INVALID`，不 fallback、不猜默认、不读旧 marker。

大 ToolResult 使用 `LedgerToolResultRef(tool_execution_id)`；恢复时扫描所有历史位置，经 Broker/Artifact 重物化。`COMPLETED` 保存精确 final text/message/generation identity；checkpoint 已提交但成功 terminal 未提交时，恢复不再请求模型或重放 delta。

### 5. 模型流提交与 generation

每个 model turn 先原子提交 `MODEL_REQUEST + OUTPUT_GENERATION_STARTED`，再发起 provider stream。每个 delta 必须先 `await RuntimeIO.emit`，才能拉取下一块。Provider 必须给出显式、自相一致的 `stop|tool_calls` finish marker；静默 EOF、usage-only、空流或半个 ToolCall 不能合成 TurnEnd。

`OUTPUT_GENERATION_STARTED` 对外投影为 `text_start`，包含稳定 `message_id`、本次 `generation_id`、被替代 generation 和 `initial|next_turn|retry|recovery|reactive_compact` reason。重试/恢复创建新 generation，旧 event 保留审计；客户端收到 `text_start` 只清空回答正文，不清工具、Skill 和计划卡片。

### 6. Tool safety 与提前派发

Native 生产路径强制使用 Tool Broker；stable identity 由 Runtime slot 派生，不依赖 framework callback ID 或 args hash。默认 `native_early_tool_dispatch=off`：模型流完整结束、整批校验、`MODEL_RESPONSE_COMMITTED`、Broker 原子 PREPARE 和 `TOOL_BATCH_COMMITTED` 全部成功后才允许 dispatch。这不关闭模型正文流或工具执行期的 Skill 进度流。

`experimental_heuristic` 仅保留为非生产实验：每个已评审 READ_ONLY 调用也必须先按 canonical logical slot 提交 Broker PREPARE，之后才允许外部执行；固定 worker/有界队列限制峰值，独立 ordinal gate 保证流式逐项 PREPARE 与 ToolResult settlement 都按模型 call 顺序。模型流结束后仍对完整 batch 再做一次原子 PREPARE/一致性核验，任何 name/args 漂移以 `TOOL_REPLAY_MISMATCH` 失败关闭。该模式不进入默认可靠性等级。

只允许已评审的 READ_ONLY、`concurrency_safe=true`、无独占资源工具走上述实验路径，并遵守 cancel/deadline/lease-loss 清理。后续 fragment 改变已派发调用时 `TOOL_REPLAY_MISMATCH` fail-closed。`provider_block_complete` 只能在 provider 声明明确 capability 时启用；当前 OpenAI-compatible provider 选择该值必须启动失败。

### 7. Cancel、deadline 与 ownership

Adapter 在 model、tool、batch、checkpoint 安全边界检查 cancel 与绝对 deadline。cancel、deadline、GeneratorExit、Adapter 异常或 lease loss 都必须关闭 provider stream，取消并 await 工具任务、HTTP 调用和 Skill 子进程。stale fence、lease loss 和 checkpoint CAS 冲突统一为 `AttemptOwnershipLost`，直接冒泡给 Worker，不转成模型 ToolResult 或 Run terminal。

这条控制异常边界延伸到 Tool Broker executor：`AttemptOwnershipLost` 不得被 `except Exception` 捕获或结算账本；其他发生在 durable `DISPATCHED` 之后的 RuntimeFault 则必须先按 effect class 结算，再由 Coordinator 计划 terminal。若 planned `FAILED` 遇到 unresolved effect，Store 保存 sticky pending terminal 并进入 strict reconciliation，最后确定 effect 后提交原失败，不能用一次新的 Engine invocation“补做收口”。

## Consequences

三引擎共享一个 Runtime/Event/Tool/Artifact 契约，但 Native 不再受 ADK 兼容层的提交顺序限制。默认 `off` 模式的模型输出、ToolCall 事实、ToolExecution 和 checkpoint 具有可测试的 happens-before 关系；ADK 两引擎仍只承诺粗粒度恢复。
