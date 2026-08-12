# Query 到 Answer 全链路代码阅读指南

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

本文按当前 Runtime 解释一条 Query 如何被 durable admission、exact-release 执行、写入 Canonical Events 并展示为最终 Answer。符号名比行号可靠。

## 1. 架构和 authority

```text
Browser/API client
  -> Artifact upload / POST runs / GET events
  -> Runtime API (:8000): admission/status/cancel/signal/SSE
  -> runtime.db + Artifact CAS
  <- Runtime Worker: claim + Coordinator + Adapter + Broker
       -> ARAG (:8100) / Skill Center (:8200) / A2A (:8300)
```

| 问题 | authority |
|---|---|
| admission/idempotency | `run_requests` |
| Run terminal | `runs` 和 `RUN_TERMINATED` |
| history | committed USER + 成功 ASSISTANT events |
| checkpoint | append-only checkpoints + revision CAS |
| Tool effect | `tool_executions` |
| Artifact bytes | SHA-256 CAS |
| Evidence | committed strict EvidenceSet |
| release | immutable manifest + active pointer |
| Trace | 诊断 store，不是业务 authority |

SQLite/CAS 当前仅提供本机多进程恢复，不是跨主机 HA。

## 2. 先启动：schema、工具和 release

Runtime/ARAG 各只有一份 current `schema.sql`。`ensure_current_schema()` 在 `BEGIN IMMEDIATE` 中：空库创建完整 schema 并记录完整文件字节的 SHA-256；非空库只接受同 digest。否则 `CURRENT_SCHEMA_MISMATCH` fail-fast，操作者显式重建。没有 migration、incremental DDL、旧数据兼容或 checkpoint upgrader。

仅 Worker 的 `build_worker()` 加载 LLM、Skill/A2A/Claude Skill 工具和 `read_artifact`，构造 strict `ToolCatalog` 与 `ToolBroker`。它校验 `agent_loop` 与 `native_loop` 的公共工具声明/策略完全一致，构造两个 `AdkEngineAdapter`、一个 `NativeLoopAdapter`，最后才原子 `activate_current_releases()`。

release activation 必须同时提供三 engine manifest；事务内写/核对 immutable manifest、拒绝异 fingerprint 非终态 Run，并一次切换三 active pointer。release identity 包含 current schema、source、catalog、provider/checkpoint codec、语义开关、上限和依赖版本。

## 3. Query 进入：CreateRun

`agent/runtime/api/runs.py::create_run` 从 API 的 `app.state` 取得 settings/Store，构造 `AdmissionService`。API 不加载 Worker 的 engine registry。`Idempotency-Key` 必填；请求 digest 不含 trace id，避免同一请求用不同诊断 id 被误判冲突。

Store admission 的单事务顺序：

1. 先查 `(principal_id, agent_id, idempotency_key)`；同 digest 重放旧 Run，不同 digest 409。
2. 读取所选 engine active pointer，冻结 exact release fingerprint；无 pointer 为 `NO_ACTIVE_RELEASE`。
3. 校验附件 metadata、conversation 归属和单 conversation 非终态 Run。
4. 写 Run、`ENGINE_RUN` Activity、idempotency/Artifact links、USER/状态 Canonical Events。

API 返回 `202`。这不表示模型启动，且 Run 后续不跟随 active release 切换。

## 4. Worker 领取和 Coordinator

`RuntimeWorker` 以自己的 `release_map` 调 `claim_next()`。SQL 精确匹配 Run 的 `(engine, release_fingerprint)`，并在事务中领取 Activity、增加 attempt/fencing、设置 lease，普通路径推进 Run。错误 release 永远不能领取/终态化该 Run；它等待正确 Worker 或 absolute deadline。

`RunCoordinator.execute_claim()` 用 fencing 把 Activity 推到 RUNNING，重读 Run，先处理 cancel/deadline/reconcile-only。reconcile-only 分支在 registry/checkpoint/history 前，只允许查询已有 ToolEffect，绝不重跑 Engine/原工具。正常分支再做 release 防御断言、读取 checkpoint、从 committed events 编译 history，并构造：

```python
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

所有写都带 fencing；失去 lease、stale fence、checkpoint CAS conflict 和 release mismatch 统一成为 `AttemptOwnershipLost`，直接穿透并由 Worker 停止旧 attempt，而非写 ToolResult 或 Run terminal。

## 5. 两种 Engine Adapter

`plan_execute` 与 `agent_loop` 是 `AdkEngineAdapter`。每 attempt 创建 ADK InMemory session/artifact service，重放 canonical history；attempt 结束即丢弃。它们保留 ADK 内部机制和粗粒度恢复，但不会宣称 mid-turn deterministic replay。它们必须返回显式 outcome，EOF 不等于成功。

`NativeLoopAdapter` 是直接的公开端口实现，独自负责 input/附件编译、strict current checkpoint、generation、RuntimeIO、Broker 和 final assistant。它不使用 ADK ReasoningEngine 或 ADK 内部事件传输；其可复用 kernel 不等于生产 Runtime Adapter。

Native stream pump 每次只允许一个 provider/kernel event 在途：`await io.emit(event)` 返回后才拉下一项。对 text，这只建立背压；`CommittedEventSink` 仍以 100ms/2KiB 批量提交，并在 generation/Tool/checkpoint/close 边界 flush。

## 6. text、generation 和最终 Answer

Native 开始 model slot 时原子提交 `MODEL_REQUEST` checkpoint 和 `OUTPUT_GENERATION_STARTED`。后者 SSE 投影为 `text_start`，payload 有 `message_id`、`generation_id`、supersedes 信息和 reason。text delta 的耐久事件是 `OUTPUT_DELTA_COMMITTED`，同样携带 identity。

恢复或新轮会产生新 generation；旧 partial events 不删除，UI/eval 收到 `text_start` 时只清当前正文。最后完整、非空、无 ToolCall 的 assistant turn 调用 `RuntimeIO.set_final_assistant()`。Coordinator 成功事务一次写 final assistant、citations、success 状态和 `RUN_TERMINATED`。`assistant_message` 覆盖过程 text，是唯一进入未来 conversation history 的 assistant 文本。

## 7. 工具、副作用和 reconciliation

Broker 的事实序列是：

```text
stable logical slot -> PREPARE + TOOL_CALL_COMMITTED
-> effect-aware dispatch -> strict ToolExecutionOutput
-> settlement + TOOL_RESULT_COMMITTED -> model projection
```

READ_ONLY 才能安全重试；幂等 effect 向下游传稳定 idempotency key；非幂等/未知 effect 不透明重试。大结果转 Artifact/ref；Native 从 committed ledger 重物化，不重复外部 effect。

`DISPATCHED` 后 ownership control fault 原样冒泡。其他 RuntimeFault 先结算：READ_ONLY 为 `FAILED`，可能副作用的 class 为 `UNKNOWN`，然后原 code 抛给 Coordinator。普通 terminal failure 遇 unresolved effect 时，Store 保留 sticky `pending_input.pending_terminal`，进入 strict reconciliation；信号逐个处置 effect，最后一个确定才写原 FAILED，Engine 不重跑。只有 `TIMED_OUT` 能保留 unresolved effect。

## 8. SSE 与 Trace

API 从 committed `run_events` replay/tail：query `after_seq` 优先于 `Last-Event-ID`，heartbeat 不入库、无 seq。主要投影是 `text_start`、`text`、`tool_call`、`tool_result`、`skill_event`、`citation`、`assistant_message` 和 `terminal`。visibility 可能造成 seq 跳号。订阅 EOF 不代表终态，也不取消 Run。

`TraceMiddleware` 的 trace id 在 admission 保存，Worker 从 Run 恢复后记录 attempt span；`CommittedEventSink` 可为 span 增加 TTFT/计数等诊断。Trace 与 SQLite 事务是旁路关系，不能用于恢复、判定 SSE 可见性或终态。关闭 Trace 不得影响运行时事实。

## 9. 推荐阅读顺序

```text
agent/main.py
-> agent/runtime/api/runs.py
-> agent/runtime/application/admission.py
-> common/sqlite_schema.py
-> agent/runtime/adapters/sqlite/store.py
-> agent/runtime/worker/main.py
-> agent/runtime/worker/dispatcher.py
-> agent/runtime/application/coordinator.py
-> agent/runtime/application/events.py
-> agent/runtime/adapters/adk_engines.py
-> agent/engine/native_loop/engine.py
-> agent/runtime/application/tool_broker.py
-> web/app.js / eval/harness/sse_client.py
```

## 10. 诚实边界

- 当前没有跨节点 HA、PostgreSQL/Temporal/Redis 或服务端 delivery ACK。
- ADK 引擎没有 mid-turn deterministic replay；Native 也不承诺 provider token 级 replay。
- LocalSandbox 不是生产安全隔离；Runtime signal/Artifact 不代表所有子 Runner 自动支持 HITL 或跨 Skill Artifact。
- 当前目录不再包含 `tests/` 和 `scripts/check.sh`；仍存在的脚本是 `scripts/run_all.sh` 与 `scripts/probe_dashscope_tool_stream.py`。本文只描述能从生产源码、current schema、配置和运行链路直接核对的能力，不宣称仓库仍有自动化可靠性回归门禁。
