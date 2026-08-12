# ToolBroker 在 Query 到 Answer 全链路中的位置

本文只回答一个问题：用户发起 Query 后，`ToolBroker` 究竟在哪一段接管工具调用，又怎样把结果安全地交还给模型和前端。

详细状态机见同目录的《ToolBroker 详解：效应感知的持久化工具调度协议》。完整 HTTP/Worker/SSE 流程见 `全链路整理/Query到Answer全链路代码阅读指南.md`。

## 1. 一句话定位

```text
ToolBroker = Engine 与外部工具之间的 durable effect authority
```

它运行在 Runtime Worker 中，不在 API 进程中。它接收 Engine 已验证的 ToolCall batch，先创建持久化 stable slot，再执行外部工具，最后把权威 ToolResult 返回 Engine。

ToolBroker 不负责：

- 接收 HTTP CreateRun；
- 决定 Run terminal；
- 拉取 SSE；
- 生成模型文本；
- 把 Trace 当作恢复数据。

这些职责分别属于 Admission、RunCoordinator/Store 权威命令路径、SSE API、Engine/Provider 和各自的权威存储。

## 2. 在全链路中的准确位置

```text
Browser
  → POST /api/v1/runs
  → Admission transaction
  → runtime.db 中 Run + ENGINE_RUN Activity
  → Worker.claim_next(release_map)
  → RunCoordinator.execute_claim
  → EngineAdapter.execute(request, RuntimeIO)
       ├─ AdkEngineAdapter: plan_execute / agent_loop
       └─ NativeLoopAdapter: native_loop（以下为默认 `off`）
             → LLM 输出完整 ToolCall batch
             → ToolBroker.prepare_batch
             → ToolBroker.execute_prepared
             → ToolResultEnvelope 放回模型消息
             → 下一轮 LLM
  → Coordinator 原子提交 final assistant + terminal
  → SSE 从 committed run_events replay/tail
```

按阶段看：

| 阶段 | Broker 是否参与 | 说明 |
|---|---|---|
| CreateRun/admission | 否 | 冻结 Run、input、release |
| Worker claim | 否 | claim 只选 exact `(engine, release_fingerprint)` |
| 模型正文流 | 否 | Engine 通过 RuntimeIO 提交 delta |
| ToolCall batch | 是 | 原子 PREPARE stable slots 与 ToolCall events |
| 外部工具执行 | 是 | effect-aware dispatch/retry/reconcile |
| ToolResult | 是 | 严格适配、Artifact/Evidence、ledger/event 结算 |
| 下一轮模型 | 间接 | Engine 使用 Broker 返回的 envelope 构造 tool message |
| Run terminal | 否 | 正常 EngineOutcome 由 Coordinator 收口；cancel/deadline/recovery/reconciliation 可由 Store 权威事务直接提交 |
| SSE | 间接 | SSE 读取 Broker 已提交的 canonical events |

## 3. Worker 启动时 Broker 已被 release 冻结

`agent/runtime/worker/main.py::build_worker` 不是先创建 Engine、运行时再随便加工具。当前顺序是：

```text
加载全部工具源
→ 构造 native/agent_loop registry
→ 校验公开工具面 parity
→ 构造唯一 strict ToolCatalog
→ 注册 ToolBroker
→ catalog digest 进入三份 ReleaseManifest
→ 创建三个 Adapter
→ 原子激活三份 release
```

因此一个已 accepted Run 冻结的不只是 Engine 源码，还包括：

- schema/source digest；
- tool catalog digest；
- 每个 tool release/effect/result adapter；
- provider/checkpoint codec；
- Native 语义配置与资源上限；
- 真实安装依赖版本。

工具目录不完整、重复、schema 非法、结果协议不明确都会在 release 激活前阻止 Worker 启动。

## 4. Native 默认 `off` 模式中的位置

`NativeLoopAdapter` 直接实现：

```python
execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

它不再经过旧的通用 ReasoningEngine adapter 或后台 merge queue。生产默认 `native_early_tool_dispatch=off` 时，工具边界如下：

```text
1. provider stream 显式 finish
2. 严格校验单 choice、finish reason、完整 ToolCall batch
3. MODEL_RESPONSE_COMMITTED checkpoint
4. Broker.prepare_batch：整批 stable slot + TOOL_CALL_COMMITTED 原子提交
5. TOOL_BATCH_COMMITTED checkpoint
6. Broker.execute_prepared：允许安全 READ_ONLY 受控并发
7. Broker 按 call ordinal 结算 TOOL_RESULT_COMMITTED
8. Engine 按 ordinal 追加 tool messages
9. TOOL_RESULT_COMMITTED / NEXT_TURN checkpoint
10. 下一轮模型调用
```

关键边界是：模型 stream 没有完整结束之前，Broker 的外部执行计数必须始终为零。

模型正文仍会流式提交；工具运行期间的 Skill/Claude Skill 进度仍会通过 awaited RuntimeIO sink 实时展示。“关闭提前派发”不等于关闭正文流或工具进度流。

## 5. Native 实验提前派发中的位置

`experimental_heuristic` 保留，但只作为显式实验模式：

```text
provider fragments
→ accumulator 暂时识别一个安全、完整的 READ_ONLY call
→ 先为该 canonical stable slot PREPARE
→ 有界 worker 执行
→ 完整 model batch 到达后重新核对
```

它仍必须经过 Broker；不存在“因为早所以绕过账本”的路径。后续 fragment 改变已派发的 name/args 时，Broker/Native 以 `TOOL_REPLAY_MISMATCH` fail-closed。

`provider_block_complete` 只有 provider 明确声明 capability 才能用。当前 OpenAI-compatible provider 不提供该信号，因此 Worker 会在 release 激活前报 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE`。

## 6. 两个 ADK 引擎中的位置

`plan_execute` 和 `agent_loop` 继续使用 `AdkEngineAdapter`，它们的内部循环没有被 Native 重构影响。

ADK 接入点是：

```text
ADK 完整 non-partial model response
→ AdkToolBatch.prepare_model_response
→ flush 前置 text
→ Broker.prepare_batch
→ ADK 并行 tool callback
→ BrokeredAdkTool.resolve stable slot
→ ToolBroker.execute（execute_prepared 的单调用包装）
```

framework function-call id 只做 attempt 内回调关联；真正的恢复身份仍是 `adk:turn:{turn}:call:{ordinal}`。

## 7. Broker-owned Event 如何进入 SSE

当前公开 ToolCall/ToolResult 事实由 Broker/Store 提交：

```text
prepare transaction
  ├─ ToolExecution PREPARED
  └─ TOOL_CALL_COMMITTED

settlement transaction
  ├─ ToolExecution COMMITTED/FAILED/UNKNOWN...
  ├─ Artifact/Evidence metadata/ref
  ├─ TOOL_RESULT_COMMITTED
  └─ 必要时 ACTIVITY_STATUS_CHANGED
```

Tool settlement 只固化工具结果及其 Evidence/Artifact 事实，不在这里提交 citation projection。成功收口时，`store.finalize_success()` 才根据最终回答中的引用标记和已提交 Evidence index 派生 `CITATION_SET_COMMITTED`，并与 final assistant、success terminal 放在同一事务。

SSE API 不订阅 Broker 的进程内对象，而是短查询 `run_events`：

```text
committed run_events
→ replay/tail(after_seq or Last-Event-ID)
→ SSE event: tool_call / tool_result / citation
→ Web UI/评测客户端
```

因此：

- 事务未提交的 ToolCall 对 SSE 不可见；
- SSE 断开不会取消工具或 Run；
- 重连按 seq 重放，不依赖 Worker 是否仍在；
- ToolCall 与 ToolResult 之间可能插入 Activity、Skill 或其他工具事件，不能假设 seq 相邻；
- 关联应使用 `tool_execution_id`/stable logical slot。

## 8. 工具结果如何返回模型

Tool executor 的返回首先由协议 adapter 归一化为：

```text
ToolExecutionOutput(result=ToolResultEnvelope(...), evidence=...)
```

Broker 严格验证并结算后，Engine 才把 `ToolResultEnvelope` 投影成模型可读的 tool message：

- SUCCESS/NO_OUTPUT → content/ref/external id；
- FAILURE → 明确错误对象；
- INTERRUPT → pending input；
- UNKNOWN → 明确 unknown-effect 错误，进入对账/人工语义。

大结果不会完整复制进 Event 或 Checkpoint。Broker ledger 保存有界 preview/ref；Native 恢复时调用 `materialize_committed_result` 从 Artifact CAS 恢复完整 current envelope。

## 9. 与 checkpoint 的顺序关系

Native 的权威边界是：

```text
MODEL_REQUEST
MODEL_RESPONSE_COMMITTED
TOOL_BATCH_COMMITTED
TOOL_RESULT_COMMITTED
NEXT_TURN
COMPLETED
```

Broker 管 Tool effect，Native checkpoint 管模型循环位置，两者不能互相替代：

- 有 `TOOL_BATCH_COMMITTED`，说明 batch 已完成 PREPARE，恢复可以从 ledger 找 stable slots；
- 某 ToolExecution 已 COMMITTED，恢复必须复用/重物化，不得重派发；
- checkpoint 中的大 ToolResult 只留 ledger ref；
- `COMPLETED` checkpoint 保存精确 final text/message/generation，恢复不再调用 Broker 或模型。

## 10. 失败场景中的位置

| 场景 | 谁裁决 | 结果 |
|---|---|---|
| 未知工具/参数非法 | Native/Engine contract | 默认 `off`：整批零 dispatch 并生成成对 engine-owned call/result；实验模式可能浪费已执行的安全 READ_ONLY 前缀 |
| stable slot 漂移 | Broker | `TOOL_REPLAY_MISMATCH`，终端 fail-closed |
| result DTO 非严格 JSON | result adapter/Broker | `TOOL_RESULT_CONTRACT_INVALID` |
| Evidence 缺 provenance | Broker | `EVIDENCE_CONTRACT_INVALID` |
| READ_ONLY 临时失败 | Broker | 在 deadline/max attempts 内可重试 |
| 非幂等调用派发后结果不明 | Broker | reconcile 或 `MANUAL_REQUIRED` |
| stale fencing/lease loss | Store/Worker | `AttemptOwnershipLost`，旧 attempt 不终态化 Run |
| provider silent EOF | Native provider boundary | `MODEL_STREAM_INCOMPLETE`；默认 `off` 为零 dispatch，实验模式取消剩余任务但可能已有已结算的安全只读前缀 |
| Run cancel | API/Store、Worker + Adapter | Store 裁决终态；运行中的 Adapter 关闭流、取消并 await 工具任务，效应不明先对账 |

控制故障和契约故障不能被 `execute_one` 吞成普通工具错误再喂回模型。

## 11. 三条最重要的阅读结论

1. `ToolBroker` 位于 Engine 内部的工具边界，但它拥有 Tool effect 和工具公开 event 的权威；Engine 不能自说“工具成功了”。
2. PREPARE 与 dispatch 分离：先冻结稳定意图，再接触外部世界；恢复身份来自 turn/call ordinal，不来自 provider id。
3. SSE 只是 committed event 的投影；正常 EngineOutcome 由 Coordinator 原子收口，cancel/deadline/recovery/reconciliation 可由 Store 命令事务直接终态化，但任何路径都不能从某个 ToolResult 或 SSE EOF 推导终态。

## 12. 推荐源码阅读顺序

```text
agent/runtime/worker/main.py
→ agent/runtime/application/tool_catalog.py
→ agent/runtime/application/tool_outputs.py
→ agent/runtime/application/tool_broker.py
→ agent/runtime/adapters/brokered_tools.py
→ agent/engine/native_loop/engine.py
→ agent/runtime/adapters/adk_engines.py
→ agent/runtime/adapters/sqlite/store.py
→ agent/runtime/api/runs.py
```

这样能先看见启动期契约，再看 durable protocol，最后看两个 Engine family 如何接入及 SSE 如何投影。
