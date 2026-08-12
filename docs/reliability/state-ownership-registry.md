# State Ownership Registry v1

状态：**FROZEN**

## 1. 分类

- **Authority**：回答业务问题的唯一最终裁判；只允许其所属应用服务写入。
- **Projection / Index**：可从 Authority 重建；损坏不得反向污染 Authority。
- **Cache / Adapter State**：仅服务一次 attempt 或性能优化；丢失不得改变业务结论。
- **Artifact**：大体积、内容寻址且可校验的完整内容；SQLite 保存元数据和引用关系。
- **Diagnostic**：用于解释“发生了什么”，不得用于恢复或裁决。
- **Client state**：由客户端持有，服务端不把它当作业务事实。

## 2. 所有权注册表

| 问题 / 数据域 | 分类 | 唯一权威与唯一写者 | 允许的派生、缓存或适配 | 明确禁止 |
|---|---|---|---|---|
| 请求是否 durable accepted | Authority | `runtime.db.run_requests`；Admission Service | HTTP 202、日志、指标 | 根据 HTTP 连接、SSE 是否打开或 Worker 是否已领取反推受理 |
| 幂等 key 对应的请求摘要与 Run | Authority | `run_requests`；Admission Service | CreateRun 的 `reused` 响应 | 在 Worker、Engine、Session 中另建幂等账本；先做 conversation busy 检查再做幂等重放 |
| Conversation 身份和下一 turn 序号 | Authority | `conversations`；Admission Service | UI conversation 列表 | 从 Session 消息数量推导 turn；多个 writer 分配 turn |
| Run 当前状态、revision、terminal | Authority | `runs`；RunCoordinator/命令处理器通过 CAS | GET Run、UI 状态、指标 | Engine、SSE `done`/EOF、Trace、ADK Session、native History 直接裁决 Run |
| 被 ToolEffect uncertainty 阻挡的计划失败 | Authority | `runs.pending_input_json.pending_terminal`；Store 仅从 Coordinator 的 `finalize_failure` 写入 | strict reconciliation UI、状态事件 | 从 Activity error/Event/Trace 重建原错误；effect 解决后重新运行 Engine；用 pending intent 冒充已提交 terminal |
| 同一 Conversation 是否有活动 Run | Authority | `runs` 的非终态唯一索引 | 409 `CONVERSATION_BUSY` | 进程锁或查询后再插入的 TOCTOU 检查代替数据库约束 |
| Activity 调度与完成进度 | Authority | `activities`；Dispatcher/Coordinator，受 revision、lease、fencing 保护 | Plan UI、Worker 指标、Trace | WorkingState/model plan 复制 completed activities；旧 fencing token 提交结果 |
| 模型对任务的认知计划 | Authority | 最新 committed `checkpoints.working_state.model_plan`；Checkpoint Store CAS | `MODEL_PLAN_UPDATED` 事件、Plan UI | 把 model plan item 当 Activity；由 `update_task_plan` 裁决 Activity/Run |
| 可恢复认知状态 | Authority | append-only `checkpoints`；Checkpoint Store CAS | Prompt preview、attempt-local engine state | ADK/native 各自维护另一套恢复 checkpoint；从 Trace 恢复 |
| 用户输入 | Authority | `run_events.USER_MESSAGE_COMMITTED`；Admission Service | Conversation history、Prompt、SSE/UI | 从 HTTP 请求日志或 Session 恢复；修改已提交事件 |
| 成功的 Assistant 语义历史 | Authority | `run_events.ASSISTANT_MESSAGE_COMMITTED`；Finalize Transaction | Conversation history、Prompt | 把中间工具轮文本、失败 partial delta、EOF 或 request-local history 纳入后续历史；Native 只提交最后完整、非空、无 ToolCall 的 assistant turn |
| Native 输出 generation 身份 | Authority | `run_events.OUTPUT_GENERATION_STARTED`；Native RuntimeIO transaction | SSE `text_start`、UI 回答重置 | 删除旧 generation event；把重试 delta 追加到旧回答；清空工具/Skill 过程卡片 |
| 流式输出 delta | Authority | `run_events.OUTPUT_DELTA_COMMITTED`；Event Sink | SSE `text`、实时 UI | 未提交 token 直接发布；用 delta 拼接结果替代 final assistant message |
| Canonical Event 顺序 | Authority | `runs.next_seq + run_events` 同一事务；Event Store | SSE 投影、报表 | 在事务外预留 seq；要求可见 seq 连续；更新或删除事件 |
| 客户端观看位置 | Client state | 浏览器/调用方持有的 `last_seq` | `after_seq`、`Last-Event-ID` 请求参数 | 创建 `delivery_cursors`；用 cursor/ACK 修改 Event 或 Run；宣称服务端知道 DELIVERED/ACKED |
| Cancel 命令与结果 | Authority | Runtime 命令事务和 `runs` CAS；Cancel Service | `CANCEL_REQUESTED` 事件、API 响应 | HTTP 断连隐式取消；terminal 后覆盖终态；忽略 dispatched/unknown effect |
| Signal 接收、消费与迟到审计 | Authority | `signals`；Signal Service/Coordinator | `SIGNAL_RECORDED` 事件、pending input UI | request-local queue 作为事实源；重复 signal 重复消费；丢弃迟到 signal 不留审计 |
| Retry/deadline/wait timeout 唤醒 | Authority | `timers`；Timer Service CAS | 定时器指标 | 纯进程定时器作为唯一来源；重复触发同一 timer |
| Tool 调用稳定身份和 Runtime 已知 effect | Authority | `tool_executions`；Tool Broker | Tool 事件、日志、Trace | 根据 HTTP timeout 猜成功/失败；用 `tool_name + args hash` 作为调用身份；另建重复幂等表 |
| 外部业务对象真实状态 | Authority | 外部 Tool 系统 | Runtime 的 `external_object_ref` 与 reconcile 结果 | Runtime 声称其 SQLite 可原子裁决外部对象；把 runtime.db 与外部系统当一个事务 |
| Artifact 完整字节 | Artifact | 内容寻址 Artifact Store | 8KiB preview、32/64KiB 有界读取、HTTP Range | 完整结果复制到 Event/Checkpoint/Trace/Session；未验 hash 就读取 |
| Artifact 元数据与来源关系 | Authority | `artifact_metadata`、`artifact_links`；Artifact Service | ArtifactRef、UI | 仅凭路径名证明 digest/size；blob rename 前提交 metadata |
| 原始文档、版本、active pointer | Authority | `rag.db.documents/document_versions/active_document_versions`；Index Job Coordinator | Web job 状态、文档列表 | vector/BM25 反向成为 Document truth；未验证版本直接激活 |
| Chunk truth | Authority | `rag.db.chunks`，归属明确的 document version | immutable vector snapshot、BM25 | 旧 active version 的 chunk 继续可见；用易碰撞 `doc_id#index` |
| Vector/BM25 | Projection / Index | 从 active chunks 和 `chunk_embeddings` 构建的不可变投影 | 进程内 snapshot | 删除投影导致文档丢失；投影损坏时返回正常 HIT/MISS；写索引后反推 active version |
| Index Job 状态 | Authority | `rag.db.index_jobs`；Index Worker | 202/GET job 响应、日志 | HTTP 生命周期内同步双写并宣称完成；未完成 job 无恢复策略 |
| Retrieval 状态 | Authority | 一次 committed EvidenceSet 的 `retrieval_status` | 有界 hits preview、Event、指标 | 用空 `chunks` 同时表达 MISS、DOWN、DENIED；异常时伪装 MISS |
| Evidence 完整集合 | Artifact | committed strict EvidenceSet Artifact；Retrieval producer 填全 provenance，Tool Broker 只校验 Runtime 身份 | citation、模型 preview | Broker 补造 query/hash/document/index version/scope；从 legacy hits/隐藏字段转换；填 `unknown/default/unversioned` |
| Citation | Projection | 从 committed EvidenceSet + final assistant message 确定性生成 | `CITATION_SET_COMMITTED`、SSE/UI | 请求内临时检索结果独立裁决 citation；terminal 后补写成功 citation |
| Release 清单 | Authority | immutable `release_manifests(engine, components)`；Worker 注册器 | digest、运维查询 | 依赖版本缺失时继续启动；原地修改同一 release；运行中静默切换语义 |
| 每引擎当前 release | Authority | `active_releases`；三 pointer 原子 Release Registrar | CreateRun admission 查询、Worker exact release map | API 自己临时拼 release；无 active release 仍受理 Run；有异 fingerprint 活跃 Run 时切换；错误 release Worker claim |
| Worker 活性 | Diagnostic / Operations | `runtime_workers` heartbeat/draining | 运维观察 | 用 heartbeat 直接判 Run FAILED；代替 Activity lease recovery |
| Trace | Diagnostic | Trace Store JSONL | 调试、评测 summary | 参与 Run/Tool/Checkpoint/terminal 裁决；Trace disabled 阻断恢复 |
| Trace 关联键 | Diagnostic | `runs.trace_id`（admission 写入，Worker 只读绑定，见 ADR-0007） | span/日志联查、`GET /api/v1/traces/{id}` | 参与 request digest、幂等或任何状态裁决 |
| ADK Session | Cache / Adapter State | 单次 ADK attempt 内临时对象 | 从 canonical history 编译的模型输入 | 进程级共享、跨 attempt 恢复、函数返回后保留或写回权威历史 |
| ADK InMemoryArtifactService | Cache / Adapter State | 单次 attempt 适配器 | 从 CAS 构造的临时多模态输入 | 作为 Artifact 权威或跨 Run 持久化 |
| native messages/tool state | Cache / Adapter State | 单次 Kernel Step / attempt | 从 canonical events + checkpoint 编译 | 进程级历史对象；将 request-local `tool_state` 当 checkpoint |
| 长期 Memory | 暂不建设 | 无 | 无 | 创建空壳 MemoryStore、把 WorkingState 或 Conversation History 改名为 Memory |

## 3. 唯一写路径

```text
Client command
  → Application Service validates intent
  → short UoW / CAS writes Authority
  → commit
  → projection, SSE, adapter or diagnostic consumes committed fact
```

投影、缓存、SSE、Trace 都是单向消费者。任何反向写回 Authority 的路径都违反本注册表。

## 4. 关键区分

### 4.1 Runtime Activity 与 Model Plan Item

Activity 是 Runtime 的调度单元，状态只由 `activities` 裁决。Model Plan Item 是模型的认知内容，只存在于 WorkingState；其 `status` 只用于展示/推理，不能证明某 Activity 已执行。

### 4.2 Checkpoint 与 History

Checkpoint 是带 revision CAS 的 append-only 可恢复业务事实；其 release 由所属 Run 的冻结 fingerprint 权威派生，表和 WorkingState 不重复保存。Native checkpoint 只有一个 strict current codec。History 是从 committed 用户消息、仅成功提交的 assistant message 与当前 checkpoint 编译出的模型输入投影。失败 partial delta 不进入 History。

### 4.3 Runtime Event 与 Delivery

Event 提交后即 AVAILABLE。首版没有服务端 Delivery 状态机；客户端 cursor 是客户端状态。SSE visibility 过滤不改变全局 seq，因此可见 seq 可以跳号。

### 4.4 Runtime ToolEffect 与外部对象

Runtime 只裁决自己掌握的 `PREPARED/DISPATCHED/COMMITTED/FAILED/UNKNOWN/...` 证据；外部工单、消息或业务对象的真实状态仍由外部系统裁决。二者通过稳定 idempotency key 和 `external_object_ref` 关联，不伪造跨系统原子事务。

### 4.5 Planned terminal 与 committed terminal

`pending_input.pending_terminal` 只在普通 `FAILED` 已被决定、但 unresolved ToolEffect 禁止 terminal commit 时存在；它保存精确 status/code/message，是后续人工处置和恢复必须保留的 intent authority。真正 Run terminal 仍只由 `runs.terminal_status/terminal_payload_json + RUN_TERMINATED` 原子事务裁决。两者不能混为一谈：pending terminal 不释放 conversation，也不让客户端宣称 Run 已结束；最后一个 effect 确定后才由 Store 提交原失败。
