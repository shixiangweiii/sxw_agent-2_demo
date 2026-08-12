# 事件持久化与 SSE 可见性的分层设计

本文说明当前架构中“谁产生事件、谁提交事实、SSE 何时可见”。关键结论是：**SSE 永远从已提交 `run_events` 读取，从不直连引擎内存流。**

## 1. 总体分层

```text
                                  产生层
       ┌───────────────────────────────────┐
       │ plan_execute / agent_loop (ADK)  │
       │ native_loop kernel              │
       │ Skill / Tool protocol adapters  │
       └───────────────────────────────────┘
                    │
                    ▼
                                  适配层
       ┌───────────────────────────────────┐
       │ AdkEngineAdapter                │  只服务两个 ADK 引擎
       │ NativeLoopAdapter               │  直接 RuntimeIO
       └───────────────────────────────────┘
                    │
          ┌──────────────┴──────────────┐
          ▼                             ▼
             RuntimeIO / Sink                Tool Broker
       engine-owned Event             Broker-owned Tool facts
          │                             │
          └──────────────┬──────────────┘
                         ▼
                    Runtime Store
        checkpoint / run_events / tool_executions
                         │ commit
                         ▼
              GET /runs/{run_id}/events
                replay + polling tail + heartbeat
                         │
                         ▼
                Web UI / eval SSE client
```

## 2. EngineAdapter 已分成两条路径

### 2.1 `AdkEngineAdapter`

`agent/runtime/adapters/adk_engines.py` 只接受 `plan_execute` 和 `agent_loop`。它为每个 attempt 创建 ADK InMemorySessionService/ArtifactService，从 Canonical Events 重建 history，并在 attempt 结束后丢弃临时 ADK 状态。

ADK 内部 event queue/merge 仍保留，但只属于这两个引擎。Adapter 对合并后事件做两件事：

- Broker 已经提交的 tool 投影：只 `force_flush()` 前面的 text，不重复写 tool event；
- 其他事件：`await io.emit(...)`。

stream 自然结束不能推导 Run 成功；ADK 引擎必须在 attempt-local RunContext 中给出明确 EngineOutcome。

### 2.2 `NativeLoopAdapter`

`agent/engine/native_loop/engine.py` 直接实现公开端口：

```text
execute(EngineRunRequest, RuntimeIO) → EngineOutcome
```

它直接负责 canonical history/附件、strict checkpoint、Native kernel 驱动、Broker 调度与最终 Assistant。Native 不经过 ADK 兼容抽象、RunContext outcome、merge queue 或后台无界 Queue。

Native 的模型和 Skill UI 事件都使用 awaited RuntimeIO：前一个事件没有提交完成，Adapter 不拉取下一个 provider 事件，也不会绕过屏障做 checkpoint/PREPARE/dispatch。

## 3. RuntimeIO 的事件与 checkpoint 契约

`RuntimeIO` 的重要能力包括：

```text
emit(event_type, payload)
force_flush()
checkpoint(working_state, expected_revision, engine_state, events)
tool_broker
is_cancelled() / remaining_ms()
set_final_assistant(text, message_id, generation_id)
abort()
```

### 3.1 text 聚合与提交可见性

`CommittedEventSink` 将 text 缓冲，默认达到 2 KiB 或等待 100 ms 后写 `OUTPUT_DELTA_COMMITTED`。切换 message/generation、进入非 text 事件、checkpoint 或 close 时都先 flush。

Native 在每个 text 帧后显式 `force_flush()`，因此它将“已向上层交付帧”和“已持久化”对齐，提供 commit-before-pull 背压。

### 3.2 checkpoint + engine-owned events 原子提交

`checkpoint()` 先 flush 旧 text，然后调用 Store `save_checkpoint()`。在后者的单个 SQLite 事务内，同时完成：

- expected revision CAS；
- 新 checkpoint row；
- 调用方传入的 engine-owned EventDraft 组；
- `CHECKPOINT_COMMITTED`；
- 从 WorkingState plan 变化派生的 plan events。

Store 会拒绝把 Store-owned event 伪装成 checkpoint event 提交。

Native 的 `MODEL_REQUEST` checkpoint 与 `OUTPUT_GENERATION_STARTED` 在这个原子边界内一起提交。默认提前派发为 `off` 时，不合法 ToolCall batch 产生的成对 synthetic call/result 也与 `NEXT_TURN` checkpoint 一起提交，保证整批零 dispatch 且无 orphan call。实验模式可能已有逐 slot PREPARE/执行的安全 READ_ONLY 前缀，完整校验失败后只保证停止后续派发并 fail-closed。

## 4. Tool Broker 拥有工具事实

`ToolBroker.prepare_batch()` 不是简单发一个 UI 事件，而是通过 Store 冻结完整工具批次：

```text
stable logical slot
  + tool name / request digest / tool release / effect policy
  → ToolExecution(PREPARED)
  → Tool Activity(PENDING)
  → TOOL_CALL_COMMITTED
```

三者同事务提交。某个 slot 重放漂移时整批回滚，不会只写一半。

`execute_prepared()` 处理外部执行，而 Store settlement 事务负责：

- 更新 ToolExecution effect status/result/ref/error；
- 更新 Tool Activity；
- 注册/链接大结果 Artifact；
- 提交 `TOOL_RESULT_COMMITTED` 和 Activity status event。

这是为什么 EngineAdapter 必须跳过 Broker 已有的 tool UI 投影：否则同一工具事实会有两条写路径。

## 5. generation 输出模型

Native 为每次生成提交 `OUTPUT_GENERATION_STARTED`，API 映射为 `text_start`：payload 包含稳定 message slot、当次 generation、可选 superseded generation 和 reason。

```text
OUTPUT_GENERATION_STARTED / text_start
  → OUTPUT_DELTA_COMMITTED / text (0..N)
  → 可能重试/恢复，再开始新 generation
  → ASSISTANT_MESSAGE_COMMITTED / assistant_message
  → RUN_TERMINATED / terminal
```

旧 generation events 保留用于审计，客户端收到新 `text_start` 只清空回答正文，不清 Tool/Skill/plan 卡片。最终 `assistant_message` 是完整语义权威，必须覆盖而不是追加到 delta 投影。

Native 的 `COMPLETED` checkpoint 保存精确 final text 与 generation identity。如果崩溃发生在 COMPLETED checkpoint 和 Run 成功终态之间，恢复只重新设置 final override，不重请模型、不重放 delta。

## 6. Store 提交与 SSE 可见性

`append_events()` 在短 SQLite 写事务中分配 Run seq 并更新 `runs.next_seq`。事务回滚时不留 seq 空洞。

SSE endpoint 每次通过短读查询：

```text
store.list_events(run_id, after_seq=cursor, limit=500)
```

只有事务 commit 后事件才能被这条查询读到，所以天然满足 commit-before-visible。SSE 不订阅内存 Queue，Worker/API 也不需要共进程。

Canonical Event 的 seq 是 Run 级全局顺序。公开 SSE 会过滤 INTERNAL 事件，因此客户端看到 seq 跳号是正常的；它仍应把最后已处理 seq 作为 cursor。

## 7. SSE replay/tail 与 UI 重建

API 支持：

- query `after_seq`；
- header `Last-Event-ID`；
- 两者都存在时 query 优先。

每批事件按 seq 输出；看到 `RUN_TERMINATED` 立即结束连接。若 Run 已终态且无更多事件，也结束。未终态且无事件时，按配置发 heartbeat comment。

Web UI 有两种 cursor 语义：

- 同一 DOM 存续期间断线：用 `lastSeq` 续传；
- 刷新页面后 DOM 已丢失：将 projection cursor 重置为 0，从 committed events fresh replay 重建 UI。

Eval harness 也同样按 committed cursor 重连，并实现 `text_start` 清正文、`assistant_message` 权威覆盖。

## 8. 故障边界

| 故障点 | 恢复后可见结果 |
|---|---|
| Sink buffer 尚未 commit 就丢失 ownership | `abort()` 丢弃 buffer，从最后 committed boundary 恢复 |
| checkpoint row 与附带 event 事务失败 | 二者均不可见 |
| Tool batch 任一 slot replay mismatch | 整批 PREPARE 回滚，零新 dispatch |
| Tool 完成但结算事务失败 | SSE 不会看到伪完成 ToolResult；按 ledger/reconcile 规则处理 |
| SSE 连接中断 | Run 继续；客户端按 cursor 重连 |
| COMPLETED checkpoint 后、Run terminal 前崩溃 | Native 从 final text/identity 恢复，不再请求模型 |

## 9. 建议的源码阅读顺序

1. `agent/runtime/ports/engine.py`：EngineAdapter/RuntimeIO 边界。
2. `agent/runtime/application/events.py`：engine-owned 事件提交。
3. `agent/runtime/adapters/adk_engines.py`：两个 ADK 引擎的兼容适配。
4. `agent/engine/native_loop/engine.py`：Native 直接 RuntimeIO。
5. `agent/runtime/application/tool_broker.py` 和 `agent/runtime/adapters/sqlite/store.py`：工具账本与事件事务。
6. `agent/runtime/api/runs.py`：SSE projection/replay/tail。
7. `web/app.js` 和 `eval/harness/sse_client.py`：客户端 generation/reconnect 语义。
