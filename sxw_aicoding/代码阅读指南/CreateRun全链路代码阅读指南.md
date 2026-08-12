# CreateRun 全链路代码阅读指南

本文从 `POST /api/v1/runs` 跟到 exact-release Worker 领取、Adapter 执行和终态提交。它描述的是当前实现，不以历史 API、旧数据库或进程内队列为前提。

## 1. 主链路

```text
Client
  -> POST /api/v1/runs
  -> AdmissionService -> SqliteRuntimeStore.admit() -> runtime.db

Worker
  -> claim_next(release_map) -> RunCoordinator.execute_claim()
  -> EngineAdapter.execute(EngineRunRequest, RuntimeIO)
  -> committed events/checkpoints/ToolExecution

API SSE
  <- 从 committed run_events replay/tail
```

API 进程只做 admission、查询、cancel、signal、Artifact 和 SSE；它不加载 LLM、ADK 或远程工具目录。Worker 才构建 ToolCatalog、release 和三个 Adapter。

## 2. 启动时先确定“当前世界”

`agent/main.py::lifespan` 和 `agent/runtime/worker/main.py::build_worker` 都先调用 `store.initialize()`。Runtime DB 只支持 current schema：

- 空库在一个 `BEGIN IMMEDIATE` 中创建完整 `agent/runtime/adapters/sqlite/schema.sql`，并写 `schema_meta`；
- 非空库必须保存与完整 `schema.sql` 原始字节 SHA-256 相同的 digest；
- 不相同、缺少 `schema_meta` 或陌生库均以 `CURRENT_SCHEMA_MISMATCH` fail-fast，操作者必须显式删除/重建。

没有 migration、`ALTER` 路径、旧 schema 兼容或 checkpoint upgrader。

Worker 随后加载所有工具源，校验 `agent_loop` 与 `native_loop` 公共工具面的名称、描述、schema 和 policy 一致，构造唯一严格 `ToolCatalog` 和 Broker。只有两个 `AdkEngineAdapter` 与一个 `NativeLoopAdapter` 都成功构造后，才调用 `activate_current_releases()`。

该调用一次接收 `plan_execute`、`agent_loop`、`native_loop` 三份 immutable manifest，在同一短写事务中核对/写 manifest、拒绝有异 fingerprint 非终态 Run 的切换，并原子切换三个 active pointer。release components 覆盖 schema/source/catalog/provider/checkpoint codec、语义配置、资源上限和实际依赖版本。

## 3. HTTP admission

入口是 `agent/runtime/api/runs.py::create_run`。`Idempotency-Key` 和三种 engine 之一必填；`deadline_at` 是绝对 UTC 时间，未提供时由服务端生成默认 deadline。`TraceMiddleware` 的 trace id 会保存到 Run，但不参与请求 digest。

`AdmissionService` 构造 `AdmissionCommand`；真正的原子顺序在 `SqliteRuntimeStore.admit()`：

1. 先按 `(principal_id, agent_id, idempotency_key)` 查 `run_requests`；同 digest 返回旧 Run，不同 digest 返回 `IDEMPOTENCY_KEY_REUSE`/409。
2. 在同一个事务读取所选 engine 的 active release，写入 `runs.release_fingerprint`；找不到为 `NO_ACTIVE_RELEASE`。
3. 校验 Artifact metadata、Conversation 归属和单 Conversation 非终态 Run 约束。
4. 创建/推进 Conversation、Run 和稳定 logical key 的 `ENGINE_RUN` Activity，写 idempotency、Artifact links、`USER_MESSAGE_COMMITTED` 以及 Run/Activity 状态事件。

因此 `202 Accepted` 只表示上述事实已耐久化。重放先于 conversation busy 检查，且 Run 一经 admission 即冻结 engine、release fingerprint、deadline、输入和身份；之后不会追随 active pointer。

## 4. exact-release claim 与 Coordinator

`RuntimeWorker` 将自己的 `{engine: fingerprint}` 传给 `claim_next()`。claim SQL 精确匹配 `(engine, release_fingerprint)`，并以 lease/revision/fencing 领取 Activity；错误 release 的 Worker 根本拿不到 Run。若没有正确 Worker，Run 保持待处理，直至绝对 deadline 收口，不生成伪造的 release 不兼容终态。

`RunCoordinator.execute_claim()` 先把 `CLAIMED` CAS 到 `RUNNING`，重读 Run，再在 adapter/checkpoint/history 之前处理 cancel、deadline 和 reconcile-only Activity。它还再次断言 adapter fingerprint 与冻结 fingerprint 相同；`CLAIM_RELEASE_MISMATCH` 是所有权防御错误，只中止 attempt。

正常分支从 committed USER 和成功 ASSISTANT events 编译 history，读取最新 checkpoint，创建 `CommittedEventSink` 作为 `RuntimeIO`，再调用唯一公开端口：

```python
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

partial delta、旧 attempt 的 ADK session 和 Trace 都不是 history/checkpoint 的 authority。

## 5. 两条 Adapter 路径

`plan_execute`、`agent_loop` 使用 `agent/runtime/adapters/adk_engines.py::AdkEngineAdapter`。每个 attempt 新建并最终丢弃 ADK `InMemorySessionService` 和 `InMemoryArtifactService`；generator EOF 不是成功，必须读取显式 `engine_outcome`。

`agent/engine/native_loop/engine.py::NativeLoopAdapter` 直接实现 `RuntimeIO` 协议，不走 ADK `ReasoningEngine` 或 ADK 内部事件传输。它负责编译 canonical input/附件、严格 current checkpoint、Broker session、generation 和 final assistant。Native 的 kernel 仍可供 Claude Skill 子 runner 使用，但生产持久化边界属于 Adapter。

Native 用一个一次只交接一个事件的 stream pump：每个 kernel/provider event 必须先 `await io.emit(...)` 返回，才允许拉下一项。这是顺序和背压。对 text，`CommittedEventSink` 仍按 100ms/2KiB 聚合落库；切换 message/generation、Tool、checkpoint、close 和 terminal 前才强制清空缓冲，不能误读为“每个 delta 都耐久提交”。

## 6. generation、最终回答和工具不确定性

Native 在 model slot 开始时随 `MODEL_REQUEST` checkpoint 原子写 `OUTPUT_GENERATION_STARTED`；SSE 将其投影成 `text_start`。每个 text payload 带 `message_id`、`generation_id`。新 generation 保留旧 partial events 供审计，客户端只重置当前回答正文。

最后完整、非空、无 ToolCall 的 native assistant turn 调用 `set_final_assistant(text, message_id, generation_id)`。Coordinator 在成功事务中写：

```text
ASSISTANT_MESSAGE_COMMITTED
+ CITATION_SET_COMMITTED
+ Run/Activity success state
+ RUN_TERMINATED
```

`assistant_message` 是最终文本权威；累积 delta 不是。ADK 未设置 override 时才使用 Sink 累积文本。

工具先由 Broker durable PREPARE，随后 dispatch/settlement。`AttemptOwnershipLost` 和 ownership-coded `RuntimeFault` 必须穿透 Adapter/Broker，既不是 ToolResult 也不直接终态化 Run。普通 executor `RuntimeFault` 在 `DISPATCHED` 后先按 effect class 结算（READ_ONLY 为 `FAILED`，其他 effect 为 `UNKNOWN`），再保留原 error code 向上。

若普通失败仍有 unresolved effect，Store 不得直接 `FAILED`：它把 `pending_input.pending_terminal={status: FAILED, code, message}` 固定下来，进入严格 tool reconciliation。最后一个 effect 被严格 signal 处置后，Store 才提交原本的 `FAILED`；不会重跑 Engine。`TIMED_OUT` 是唯一可携带 unresolved ToolEffect 的终态。

## 7. SSE

`GET /api/v1/runs/{run_id}/events` 只读 committed `run_events`：显式 `after_seq` 优先于 `Last-Event-ID`，按 seq replay/tail。`text_start`、`text`、`assistant_message`、`tool_call`、`tool_result` 都是投影；只有 `RUN_TERMINATED`/GET Run status 是终态事实。heartbeat 无 seq、不落库、不影响 cursor；断开订阅也不会取消 Run。

## 8. 建议阅读顺序

1. `agent/main.py`、`agent/runtime/api/runs.py`
2. `agent/runtime/application/admission.py`、`agent/runtime/adapters/sqlite/store.py`
3. `agent/runtime/worker/main.py`、`agent/runtime/worker/dispatcher.py`
4. `agent/runtime/application/coordinator.py`、`agent/runtime/application/events.py`
5. `agent/runtime/adapters/adk_engines.py`、`agent/engine/native_loop/engine.py`
6. `agent/runtime/application/tool_broker.py`
