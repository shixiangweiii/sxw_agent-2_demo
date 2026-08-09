# ADR-0004：Engine Adapter 与恢复等级

- 状态：Accepted / Frozen
- 日期：2026-08-09

## Context

三代引擎当前由流式生成器驱动并各自持有历史。可靠 Runtime 必须统一终态、事件和 checkpoint 权威，同时诚实保留不同引擎能达到的恢复粒度。

## Decision

### 1. 统一契约

启动期 `ENGINE` 选型被每 Run 必填 `engine` 替代：

```text
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

`RuntimeIO` 只暴露 committed EventSink、checkpoint CAS、Tool Broker、Artifact、Clock、绝对 deadline/剩余预算与 cancel probe。`EngineOutcome` 仅为：

```text
COMPLETED | RETRYABLE_FAILURE | TERMINAL_FAILURE |
WAITING_INPUT | CANCELLED
```

Engine 可以提交事件草稿和返回 outcome，但不能写 Run terminal。唯一 terminal 裁判是 RunCoordinator。EOF、旧 `done/error` 和 generator 正常退出不参与裁决；异常不得被 `merge_runner_events` 吞掉后伪装成功。

### 2. Canonical history

每个 attempt 的模型输入由以下 committed facts 编译：

- conversation 的全部 `USER_MESSAGE_COMMITTED`；
- 仅属于成功 Run 的 `ASSISTANT_MESSAGE_COMMITTED`；
- 当前 Run 的 checkpoint/WorkingState 和引擎恢复状态；
- 当前 transition 必需的续推/收口指令。

失败 partial delta、Trace、SSE、旧 Session 或进程级 native History 不进入历史。

### 3. 恢复等级

| Engine | R1/R2 恢复粒度 | R3 目标 | 明确边界 |
|---|---|---|---|
| `native_loop` | model/tool boundary checkpoint | 可恢复 Kernel Step：model 前、ToolCall batch、每个 ToolResult、compact/next-turn/stop 都 checkpoint | 半个 model stream 可重做；early tool 只允许已 committed PREPARED 的 READ_ONLY |
| `plan_execute` | decision plan 持久化；execution attempt 重启复用计划 | step/activity boundary | 决策 plan 已提交后不得重新规划；不承诺模型 token/mid-call replay |
| `agent_loop` | 整个 ADK invocation 为粗粒度 Activity | invocation boundary | attempt 可整体重做，Tool Broker 拦截已提交 effect；不承诺 ADK mid-turn deterministic replay |

### 4. 临时适配器状态

ADK Session 每 attempt 创建，由 canonical history 填充，attempt 完成后销毁。ADK InMemoryArtifactService 只承载从 CAS 构造的 attempt 多模态输入。native messages/request-local `tool_state` 只作调用镜像，checkpoint 必须来自 Runtime Store。

### 5. Tool safety

- stable Tool identity 由 Runtime 派生，不依赖框架 callback ID。
- `COMMITTED` 结果重放直接复用。
- `UNKNOWN` 先 reconcile。`NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT` 只能确定性收口或 manual；READ_ONLY 与携带稳定下游 key 的 `IDEMPOTENT_EFFECT` 仅在 query 未发现已提交结果、attempt guard 仍允许时，才可用同一 ToolExecution/key 受控重试。人工 `action=reconcile` 的专用 marker 永远只有 query 权限。
- native streaming early execution 只开放给完整解析且 committed PREPARED 的 READ_ONLY；side-effect/UNKNOWN 必须等待完整 ToolCall batch committed。

### 6. Cancel/deadline

Adapter 在 model、tool、batch、checkpoint 安全边界检查 cancel；绝对 UTC deadline 的剩余预算向下传递，不在各层重新启动完整 timeout。stale attempt 的迟到结果由 fencing/CAS 拒绝。

## Consequences

三代引擎共享一个 Runtime/Event/Tool/Artifact 契约，可在同一 Worker 内按 Run 选择；对比仍诚实体现恢复粒度差异，不通过声称 ADK mid-turn replay 来抹平边界。
