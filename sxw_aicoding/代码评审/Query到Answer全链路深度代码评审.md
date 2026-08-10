# Query 到 Answer 全链路深度代码评审

> 评审日期：2026-08-10  
> 评审版本：`25167b4`  
> 评审对象：`sxw_aicoding/代码阅读指南/全链路整理/Query到Answer全链路代码阅读指南.md` 所梳理的实现链路  
> 评审约束：本次只读代码、规范与生产参考实现；未修改业务代码、测试、配置或既有文档

## 1. 结论先行

当前实现已经建立了一套相当扎实的**单机持久化可靠 Runtime 骨架**：Admission 有事务化幂等账本，Run/Activity 有明确状态机，Worker 使用 lease + fencing，Canonical Event 先提交后发布，Tool Broker 对副作用分级，成功终态将 assistant、citation 与 terminal 原子提交，SSE 只投影 committed event。统一门禁也全部通过。

但本次沿真实代码链路和失败分支评审后，结论是：**暂不建议把这条链路判定为可靠性评审通过**。发现 4 个 P1 正确性问题和 4 个 P2 完整性/一致性问题，其中前三个会直接破坏冻结的 Runtime 不变量，第四个会产生错误引用归因：

| ID | 严重级别 | 结论 |
|---|---|---|
| QR-01 | P1 | 带显式 deadline 的同键同摘要重放，会在 deadline 过期后被 400 拒绝，破坏 durable accepted 幂等语义 |
| QR-02 | P1 | `plan_execute`、`agent_loop` 仍把真实子流的正常 EOF 转写成 COMPLETED，未满足“EOF 不能裁决成功” |
| QR-03 | P1 | 未决 ToolEffect 可进入普通重试，随后可被提交为 FAILED，破坏外部副作用不确定性守恒 |
| QR-04 | P1 | 一个 Run 多次 `knowledge_search` 时，局部 `[n]` 会跨 EvidenceSet 串号，可能提交错误 citation |
| QR-05 | P2 | Web 每次 CreateRun 都生成新幂等键，请求结果丢失后的重试不是同一 intent |
| QR-06 | P2 | 页面刷新只重建最后一个 assistant 容器，忽略 `user_message`，不能重建完整会话 UI |
| QR-07 | P2 | 文档“附件”绕过 Runtime Artifact/Run provenance，且默认数据集中的逻辑 ID 仅由文件名决定 |
| QR-08 | P2 | native 工具适配在每个 attempt 才发生且失败时静默跳过，实际工具面可能与已激活 release 不一致 |

此外，最新项目背景已经明确要求从真实集群化、分布式部署出发进行设计；当前 R0 规格仍明确冻结为单机 SQLite + 本地 CAS，并把多机 HA、PostgreSQL/Temporal 列为非目标。两者不是一个层级的承诺：

- 以当前 R0 为基线，单机实现本身是有意设计，不应简单评价为“SQLite 用错了”；
- 以最新长期目标为基线，当前 `runtime.db`、本地 Artifact CAS、进程发现的工具目录和本机 lease/fencing 不能支撑多节点权威一致性，不能宣称已经具备分布式生产部署能力；
- 后续若启动集群化改造，应先用 ADR 冻结新的权威存储、协调、队列、对象存储和故障模型，再完整替换，不应在 SQLite 旁增加 Redis 双写形成第二事实源。

## 2. 评审基线与方法

### 2.1 事实与规范基线

本次以以下材料交叉校验，优先级不是“阅读指南说了什么”，而是“冻结规范要求什么、真实代码做了什么”：

1. 最新 `sxw_aicoding/项目背景说明.txt`：生产级边界、集群化思考、无历史兼容包袱、代码简洁直白。
2. 当前 `AGENTS.md`：唯一事实来源、三代引擎边界、ToolEffect、Artifact、ARAG、SSE 以及长期集群化方向。
3. `docs/reliability/`：R0 冻结状态机、Failure Matrix、状态所有权、事务边界、EngineOutcome 契约。
4. 目标阅读指南：核对其描述是否完整、准确，能否用于理解真实 Query→Answer 链路。
5. 真实实现：从 Web/API、Admission、SQLite Store、Worker、Coordinator、三代引擎、Tool Broker、ARAG/Evidence、SSE 一直追到浏览器投影。
6. 四个生产参考目录：只对照接入/会话、Runtime、Skill/A2A、ARAG 的设计取向，没有机械复制企业内部治理代码。

### 2.2 评审范围

覆盖的主链路和关键旁路如下：

```text
浏览器输入/文档入库/图片上传
  → CreateRun admission + idempotency + conversation serialization
  → runtime.db 中 Run/Activity/Event/Artifact link
  → Worker claim + lease/revision/fencing
  → RunCoordinator
  → EngineAdapter
       ├─ plan_execute
       ├─ agent_loop
       └─ native_loop
  → Tool Broker + ToolExecution ledger
       └─ knowledge_search → ARAG → EvidenceSet Artifact
  → EngineOutcome
  → assistant/citation/terminal 原子提交
  → committed event SSE replay/tail
  → Web 增量显示、重连与页面刷新恢复
```

同时检查了 deadline、cancel、retry、未知副作用、Worker 恢复、空事件流、重复检索、HTTP 响应丢失和页面刷新等失败边界。

### 2.3 严重级别

- **P1**：破坏已冻结不变量、可能误判终态、重复副作用或生成错误业务事实，应优先修复并补可靠性门禁。
- **P2**：不会在所有请求上立即失败，但会造成端到端语义不完整、恢复错误、发布契约漂移或生产化阻塞。
- **P3**：文档精度、可维护性或当前尚未激活的契约问题。

## 3. 做得好的部分

以下设计有清晰的生产级不变量，应在后续修复中保留：

1. `agent/runtime/adapters/sqlite/store.py:386-530` 将幂等检查、conversation busy、Run/Activity、Artifact link、输入事件放在短事务内，且幂等重放先于 busy 检查。
2. Event append 与 `runs.next_seq` 同事务，SSE 从 `run_events` 短查询 replay/tail，满足 commit-before-publish。
3. `agent/runtime/adapters/legacy_engines.py:130-181` 每个 attempt 新建隔离的 ADK session/artifact 适配器，跨 attempt 历史从 committed facts 编译。
4. Worker claim、lease、revision、fencing 组合完整，旧 owner 的提交会被拒绝；Worker 丢失不会直接把 Run 判失败。
5. Tool Broker 没有把“HTTP 超时”直接猜成失败，对 `DISPATCHED/UNKNOWN/MANUAL_REQUIRED` 保留持久事实。
6. `finalize_success` 在同一事务内提交完整 assistant、从 EvidenceSet 派生的 citation 和唯一 terminal。
7. ARAG 使用 durable index job 和 active version pointer，SQLite 是文档/版本/chunk 的事实源，内存索引只是可重建投影。
8. Web SSE 重连携带 cursor，SSE 断开不隐式取消 Run；`assistant_message` 可以覆盖不完整 delta，避免只靠传输片段恢复答案。

这些优点说明问题主要集中在几个**收口 guard 和端到端契约接缝**，不是整体架构毫无基础。

## 4. 详细问题

### QR-01 [P1] deadline 校验先于幂等查账，已受理请求无法永久重放

**证据**

- `agent/runtime/application/admission.py:50-62` 先读取当前时间并执行 `deadline <= now` 校验，随后才计算 request digest 和调用 Store。
- `agent/runtime/adapters/sqlite/store.py:386-402` 真正的幂等账本查询位于 `store.admit()` 事务内。
- `AGENTS.md:69-71` 冻结语义是同一 `(principal_id, agent_id, idempotency_key)` 且 digest 相同返回原 Run，幂等重放先于其他 admission 决策。

**最小复现**

1. FakeClock=`1000`，显式 `deadline_at=2000`，首次 CreateRun 成功。
2. 将 FakeClock 推进到 `3000`，用完全相同的 key 和 payload 重放。
3. 当前返回 `DEADLINE_IN_PAST / HTTP 400`，而不是首次 Run 且 `reused=true`。

**影响**

客户端在首次响应丢失后可能晚于 deadline 重试。服务端其实已经 durable accepted，但调用方无法通过原幂等键找回 Run，durable accepted 和幂等账本失去端到端意义。这也使行为依赖重放时刻，不再只依赖 key + digest。

**建议**

- 将“查已有 key、比 digest、返回原 Run”置于所有只针对新请求的时效校验之前，并保持在一个权威事务里。
- 对首次请求才检查 deadline、active release、conversation 和附件；已有同摘要请求不能被新的 wall clock 否决。
- 不要为了修复而在 Service 层增加一次事务外预查，否则会制造 TOCTOU 和第二套 admission 判断。

**必须补测**

- 首次 deadline 有效、重放时 deadline 已过期，仍返回原 Run。
- 同 key 不同 digest 即使 deadline 已过期也稳定返回 409，而不是被 400 抢先。
- 默认 deadline 不进入 request digest 的现有语义继续成立。

### QR-02 [P1] 两个 ADK 引擎仍由子事件流 EOF 推导成功

**证据**

- Adapter 在 `agent/runtime/adapters/legacy_engines.py:171-180` 明确要求事件迭代器耗尽不能证明成功；只有 `rc.engine_outcome` 存在时才接受。
- `agent/engine/plan_execute/plan_execute_engine.py:53-61` 在 `_executor.execute(...)` 的 `async for` 正常结束后，无额外控制结果即写入 `COMPLETED`。
- `agent/engine/agent_loop/agent_loop_engine.py:126-134` 在 `merge_runner_events(...)` 正常结束后，无额外控制结果即写入 `COMPLETED`。
- `native_loop` 会依据 loop 返回的明确 stop reason 收口，问题主要在两个 ADK 引擎。

**最小复现**

分别替换为“正常返回但不产生任何事件”的 ExecutionPlanner 和 ADK Runner，直接运行真实 `PlanExecuteEngine`、`AgentLoopEngine`：两者最终都得到 `EngineOutcome(COMPLETED)`。

现有 `test_rel_30_text_then_eof...` 只把整个 engine 替换成一个不设置 `engine_outcome` 的假实现，因此验证了 Adapter 会 fail closed，却没有覆盖真实引擎在 EOF 后自行填写 COMPLETED 的路径。

**影响**

如果 ADK/执行器因协议回归、上游静默截断或桥接错误而“干净结束”，Run 会被误判成功；甚至可能原子提交空/不完整 assistant 和 SUCCEEDED terminal。代码注释称其为 explicit control boundary，但真实控制信号仍然只是 Python 生成器的正常退出，语义与注释不一致。

**建议**

- `plan_execute`：ExecutionPlanner 返回独立的、可枚举的执行控制结果，Engine 只在结果明确为 completed 时设置成功；生成器 EOF 不能替代结果。
- `agent_loop`：从 ADK 的 final response / invocation completion contract 获取明确完成证明，并明确区分正常完成、被截断、达到调用上限、Runner 无 final response。
- 桥接层对缺失 final control result 统一返回 `ENGINE_OUTCOME_MISSING` 或更具体的协议错误。

**必须补测**

- 使用真实两个 Engine 类覆盖：空流、只有 text 后 EOF、tool_call 后 EOF、存在 final response 的成功路径、异常传播路径。
- 测试断言 Run terminal，而不仅是 request-local `rc.engine_outcome`。

### QR-03 [P1] 未决外部副作用可以进入普通 retry，最终还能被判 FAILED

**证据**

- `agent/runtime/application/coordinator.py:443-474` 只有 COMPLETED 分支主动查询 `unresolved_tool_execution_ids`。
- `coordinator.py:488-515` 的 RETRYABLE_FAILURE 直接 `schedule_retry`，普通失败直接 `finalize_failure`。
- `agent/runtime/adapters/sqlite/store.py:1300-1325` 仅当当前 Run 已是 `CANCEL_REQUESTED` 时阻止带 unresolved effect 的非超时失败。
- `store.py:1573-1580` 通用 terminal helper 只对 CANCELLED 再查 unresolved，没有对 SUCCEEDED/FAILED 建立统一 guard。
- `store.py:1645-1690` 普通 RUNNING 状态下的 `schedule_retry` 不检查 unresolved。

**最小复现**

1. UNKNOWN_EFFECT 工具 dispatch 后执行器抛错，ToolExecution 进入 UNKNOWN/MANUAL_REQUIRED。
2. 对本次 Activity 调用 `schedule_retry`，当前 Run 和 Activity 都进入 `WAITING_RETRY`，未决记录仍存在。
3. timer 重新派发后调用 `finalize_failure`，当前可以提交 `FAILED`，同时 unresolved ToolExecution 仍存在。

**影响**

- retry 可能再次运行模型并生成新的副作用意图，而上一次外部效果是否已经发生仍未知；
- FAILED 会向调用方传达“本次已明确失败”，但真实世界可能已经成功产生不可逆效果；
- 终态事实与 ToolExecution 事实互相矛盾，后续人工 reconcile 也失去清晰的父 Run 状态边界。

按照冻结状态机，`SUCCEEDED/FAILED/CANCELLED` 都不能掩盖未决外部效果；只有 `TIMED_OUT` 可以携带 unresolved IDs 终止，以表达超时后仍需追查。

**建议**

- 把 ToolEffect 收口分类下沉为 Store 内的原子 guard，而不是由 Coordinator 事务外“先查再写”。
- 所有进入 `SUCCEEDED/FAILED/CANCELLED` 的路径在同一事务内拒绝 unresolved；`TIMED_OUT` 显式携带 IDs。
- `schedule_retry` 也必须原子分类：只有已提交可复用、明确失败或真正 replay-safe 的执行可进入普通 retry；UNKNOWN/MANUAL_REQUIRED 应转入 `WAITING_INPUT + TOOL_RECONCILIATION_REQUIRED`。
- cancel、deadline、lease recovery、普通失败和最大重试耗尽应共享同一套结算函数，避免分支规则继续漂移。

**必须补测**

- UNKNOWN/MANUAL_REQUIRED × completed/retryable/terminal failure/cancel/deadline/lease expired 的矩阵测试。
- Store 级并发测试：在“查询未决效果”和 terminal commit 之间插入并发 ToolExecution 状态变化，证明无 TOCTOU。

### QR-04 [P1] 多次知识检索时 `[n]` 不是全局证据身份，会产生错误 citation

**证据**

- `agent/tools/knowledge_search.py:57-81` 每次工具调用都把返回 chunk 从 `n=1` 重新编号，并告诉模型用 `[n]` 引用。
- `agent/runtime/adapters/sqlite/store.py:1221-1263` 从 assistant 文本只提取裸整数 marker，再遍历本 Run 的所有 committed `knowledge_search` ToolExecution；任一 EvidenceSet 中 `n` 相同的证据都会加入 citation。
- 状态所有权约束明确：Evidence 的稳定身份是 `evidence_id/query_id/version/content_hash`，显示序号不是身份。

**最小复现**

同一 Run 提交两次 `knowledge_search`，两个 EvidenceSet 都有各自的 `n=1`，evidence 分别为 `ev-A`、`ev-B`。最终 assistant 只写一个 `[1]`，当前终态事务会生成两条 citation，同时归因给 `ev-A` 和 `ev-B`。

**影响**

这是静默的数据正确性问题：回答表面带有引用，审计数据也完整，但引用可能指向模型并未使用的另一轮搜索结果。多轮检索、query rewrite、子任务检索越多，误归因概率越高。

**建议**

- 不再把一次工具调用内的局部 `n` 当作 Run 级身份。
- 给模型的 citation token 必须能唯一绑定 ToolExecution/query 和 evidence，例如 `[q2:1]`，或使用运行时分配的短 token 映射到 `evidence_id`。
- 终态派生只按精确 token 查一条证据；显示序号可以在最终 UI 投影时重新编号，但不能参与权威关联。
- EvidenceSet 继续保留完整稳定字段，不要把大证据内容复制进 Event/Checkpoint。

**必须补测**

- 同一 Run 两次检索都含局部 `n=1`，最终 marker 只能命中指定 query 的证据。
- 同证据重复召回、不同 query 命中同 chunk、无效 token、无引用、DEGRADED/ERROR EvidenceSet。

### QR-05 [P2] Web 没有真正使用服务端 CreateRun 幂等能力

**证据**

- `web/app.js:363-369` 在每次 `createRun()` 调用内部即时生成新的 `Idempotency-Key` 和 `client_request_id`。
- 只有拿到成功响应后才在 `app.js:378-387` 持久化 `run_id`；请求已在服务端提交但响应丢失时，本地没有可恢复身份。

**影响**

网络超时/刷新后再次点击会被服务端识别为新 intent。第一次 Run 尚未终止时可能得到 conversation busy；若第一次已经终止，则可能创建重复 Run。服务端的幂等账本正确，但浏览器没有完成端到端闭环。

**建议**

- POST 前先持久化 pending request envelope、稳定 idempotency key、client_request_id 和 request digest 所需字段。
- 在明确拿到 accepted Run 或明确废弃 intent 前，所有重试复用同一 key 和完全相同的 payload。
- 页面恢复先处理 pending intent，再处理已知 run_id；不要用新 UUID 猜测是否需要重建请求。
- cancel/signal 的 command identity 也应采用相同原则。

### QR-06 [P2] “刷新后从 seq=0 重建 UI”只恢复答案，不恢复完整会话

**证据**

- SSE 公开映射包含 `user_message`：`agent/runtime/api/runs.py:233-245`。
- `web/app.js:291-320` 的事件处理没有 `user_message` 分支。
- `web/app.js:450-472` 刷新时只为 localStorage 中最后一个 Run 创建空 assistant 容器并从 seq=0 重放。
- 目标指南 `9.2` 将此描述为“从 committed events 重建 UI / 重放所有事件”，表达过度完整。

**影响**

刷新后用户问题、附件信息和更早 conversation turns 消失；只有最后一个 Run 的 assistant/process/citation 被投影。它满足“最后一个答案可恢复”，不满足“会话 UI 可重建”。现有 Web 恢复测试主要检查静态字符串和 cursor 重置，无法证明语义投影完整。

**建议**

- 最小修复：消费 `user_message` 并在重放时创建对应 user/assistant turn，避免预先硬编码单个 assistant 容器。
- 完整方案：提供 conversation 级 committed projection/read API，按 `turn_seq` 重建多轮；Run SSE 继续负责单 Run tail。
- 增加浏览器语义测试：刷新前后 DOM 中 user、assistant、citation、terminal 一致，而不只是源码包含 `lastSeq = 0`。

### QR-07 [P2] 文档附件与图片附件走两套不同的权威链路

**证据**

- `web/app.js:238-267` 在浏览器提取文档文本，先调用 `/api/v1/documents/index` 并等待 ACTIVATED；`doc_id` 是 `web:${filename}`，`dataset_id` 固定为 `default`。
- `web/app.js:270-276` 只有图片上传 Runtime Artifact。
- `web/app.js:401-425` 文档索引成功后才 CreateRun，而传入 `attachment_refs` 的只有图片 Artifact。
- `agent/runtime/adapters/sqlite/store.py:493-513` 只能为真正的 `attachment_refs` 建立 Run/Activity/Event provenance link。

**影响**

- UI 把文档和图片都称为附件，但文档本质是 Run 之前发生的全局知识库写入；该 Run 中没有文档 Artifact 身份或索引 job/version provenance。
- 文档已激活而 CreateRun 失败时，知识库副作用仍永久存在。
- 不同用户上传同名文件会命中同一默认 dataset/logical doc，可能更新同一个 active pointer；当前学习项目不接真实流量，但这不能作为未来多租户设计。
- 阅读指南用统一“query + attachments”描述入口，会让读者误以为所有附件都先进入 CAS 并随 Run 冻结。

**建议**

短期必须先把产品语义说清：这是“知识库入库 + 随后提问”，还是“本轮 Run 附件”。若是前者，应在 UI 和指南中分区，并显示 dataset/document version；若是后者，应先上传 Artifact，再以主体/租户作用域的 logical document identity 创建 index job，并把 Artifact、index job、document version 与 Run 建立 provenance。

### QR-08 [P2] native 实际工具面可与已激活 release 静默漂移

**证据**

- Worker 在 `agent/runtime/worker/main.py:57-93` 基于 `context.tools` 计算统一 catalog digest，构造 Adapter 后即激活三个 release。
- `agent/runtime/adapters/legacy_engines.py:139` 到每个 attempt 执行时才 `build_engine(...)`。
- native 的 `agent/engine/native_loop/tools.py:326-345` 在适配单个工具失败时捕获所有异常、记录 `tool skipped` 后继续构建 registry。

**影响**

release manifest 可能声明某个 catalog，但 native attempt 实际缺少其中一个工具；另两个引擎仍可能拥有该工具，形成跨引擎不一致。由于失败发生在 release 激活之后且不 fail-fast，同一个 release fingerprint 不能完全证明实际执行工具面不可变。

**建议**

- Worker 启动阶段预构建并校验三个引擎的真实工具 registry，再发布 active pointers。
- 工具适配、ADK 私有契约或 schema 不匹配应阻止对应 release 激活，不应静默跳过。
- release digest 应覆盖各引擎最终可执行的规范化 tool declarations，而不仅是原始 `context.tools`。
- 增加“任一工具适配失败则 Worker readiness/release activation 失败”和“三引擎共享工具面一致”的门禁。

## 5. 目标代码阅读指南的准确性评审

### 5.1 准确且值得保留的内容

目标指南对以下骨架描述基本准确：

- CreateRun 与 SSE 分离，HTTP 202 只表示 durable accepted；
- admission 的幂等范围、conversation 单飞和事务化写入；
- Worker claim 的 lease/revision/fencing；
- Coordinator 是唯一 terminal 裁决者；
- per-attempt EngineAdapter/session 边界；
- output delta 的 100ms/2KiB 聚合和切换语义事件前 flush；
- success 时 assistant + citation + terminal 同事务；
- SSE 读取 committed events、cursor 重连和断开不取消 Run。

### 5.2 需要纠正的描述

1. **Activity ID 生成位置不准确**：指南 `3.1` 把 `activity_id = UUIDv5(...)` 写在 AdmissionCommand 构造阶段；真实 `AdmissionCommand` 不含 activity_id，它在 `store.admit()` 事务内由 `agent/runtime/adapters/sqlite/store.py:476` 生成。
2. **“显式 EngineOutcome”描述掩盖真实 EOF 问题**：指南强调 Adapter 不接受 EOF，但没有继续审查两个 ADK engine 如何设置 `rc.engine_outcome`，因此得出了比实现更强的保证。
3. **“刷新后重建 UI”表述过度**：实际只重放最后一个 Run 到一个 assistant 容器，且忽略 `user_message`；应改为“恢复最后一个 Run 的答案和过程投影”。
4. **“附件”概念混用**：图片是 Runtime Artifact attachment，文档是先行 ARAG ingestion；指南应拆成两条入口链路。
5. **代码片段近似实现但非严格摘录**：适合作为导读，但关键状态 guard 应给出真实函数和不变量，避免注释替代代码证明。

### 5.3 关键缺失链路

当前指南更接近“Admission→Worker→SSE 的 happy path”，还不是完整的 Query→Answer 深度阅读指南。至少缺少：

- 三代引擎各自如何把 model/ADK/native loop 的控制结果转换为 EngineOutcome；
- Tool Broker 的 stable slot、effect class、dispatch、reconcile、result reuse 和大结果 Artifact 化；
- `knowledge_search → ARAG retrieve → EvidenceSet Artifact → citation` 的权威链路；
- cancel、deadline、retry、lease expiry、fencing reject、WAITING_INPUT、signal 和 tool reconciliation；
- 文档 index job 的 PREPARED→ACTIVATED 状态机和 active version 读取；
- release manifest/active pointer 与 Worker readiness；
- 浏览器幂等 intent、页面刷新投影和 conversation 级恢复边界；
- API 与 Worker 各自独立 Trace，且 Trace 不能参与业务恢复。

建议后续把指南改成两张图：一张 happy path，一张 failure/recovery path；每个阶段都标出 Authority、事务边界、可重放身份和终止 guard。否则读者容易只看见流式调用栈，看不见真正决定可靠性的持久化状态转换。

## 6. 最新“集群化生产级”目标与当前 R0 的架构差距

### 6.1 当前实现能承诺什么

`docs/reliability/README.md:6,12-16,72-74` 明确冻结的是单机、本地磁盘、可恢复、可验证的 Runtime。当前 SQLite 的 `BEGIN IMMEDIATE`、唯一索引、WAL/FULL、revision/fencing 在**共享同一文件的本机 API/Worker**内成立，Artifact CAS 的 atomic rename/fsync 也只覆盖该主机。

因此当前准确表述应是：**单机进程级恢复参考实现**，不是分布式 HA。

### 6.2 多实例后会失效的权威边界

如果直接把现有 API/Worker 横向部署到不同节点：

- 每个节点的 `runtime.db` 会形成不同的 admission、Run、event、lease 和 release authority；
- conversation unique/busy、幂等唯一约束、`next_seq`、checkpoint CAS、terminal CAS 无法跨节点成立；
- 本地 Artifact CAS 无法保证任意 Worker 能读取上传节点上的字节；
- 本机 polling、cancel/signal 和 timer 不能天然提供跨节点唤醒/调度；
- Worker 各自启动时发现的远程 Skill/A2A catalog 可能不同，active release pointer 也不再是全局事实。

### 6.3 推荐的演进原则

这里不建议简单回答“加 Redis”：Redis 很适合协调、通知、限流和短期租约，但 Run/Event/ToolEffect/Checkpoint 需要事务、唯一约束、CAS 和可审计历史。更稳妥的候选分层是：

| 能力 | 候选权威/组件 | 原则 |
|---|---|---|
| Run、Activity、Event、ToolExecution、Checkpoint、release pointer | 共享事务数据库（优先评估 PostgreSQL） | 保持一个业务事实源和原子状态迁移 |
| 工作通知、延迟调度 | durable queue，或以共享数据库为 authority 的 outbox/claim | 消息只唤醒，不能替代事实 |
| cancel/signal/短期协调 | Redis 或等价组件 | 可加速广播，但最终状态仍回到权威 Store |
| Artifact/EvidenceSet bytes | 具备 checksum/version 的对象存储 | metadata/link 仍由事务数据库裁决 |
| release/tool catalog | 不可变制品 + 全局 active pointer | Worker readiness 必须证明加载内容与 pointer 一致 |

正式演进前需要先补一份 ADR，明确一致性模型、节点故障、网络分区、消息重复/乱序、数据库事务边界、对象存储提交协议和迁移策略。根据当前项目“不保留兼容/双写层”的决策，一旦选择新架构，应做完整 authority replacement，而不是长期维护 SQLite + Redis/PostgreSQL 两套裁决。

## 7. 验证结果与测试缺口

### 7.1 已运行门禁

执行：

```bash
bash scripts/check.sh
```

结果：

- `247 passed`，8 条上游 ADK/实验 API 类 warning；
- py_compile、Runtime/ARAG migration checksum、schema、traceability、旧协议扫描、diff check 均 PASS；
- 未使用真实 LLM key，未把行为评测分数算作可靠性结论。

### 7.2 额外最小复现

所有复现都使用临时目录/临时 SQLite 或 stub runner，没有修改仓库文件：

- 显式 deadline 首次成功、过期后同键重放返回 `DEADLINE_IN_PAST`；
- 真实 `plan_execute` 和 `agent_loop` 的空子流均得到 `COMPLETED`；
- UNKNOWN/MANUAL_REQUIRED ToolExecution 可经过 `WAITING_RETRY` 后进入 `FAILED`；
- 两个 EvidenceSet 各自 `n=1` 时，一个 `[1]` 派生出两条 citation。

### 7.3 为什么现有门禁仍全绿

现有测试对许多局部组件覆盖良好，但上述问题恰好位于跨层组合边界：

- 测试了 Store 内幂等优先级，没有测试 Service 的时效校验抢先；
- 测试了 Adapter 缺 outcome 会 fail closed，没有测试真实 Engine 在 EOF 后伪造 outcome；
- 测试了 cancel + unresolved effect，没有覆盖普通 retry/failure + unresolved effect；
- 测试了单次检索 citation，没有覆盖一个 Run 多次检索的 marker namespace；
- Web 测试偏静态契约，没有验证响应丢失和 DOM 语义重建。

这说明下一轮应优先增加**跨层可靠性测试**，而不是只提高单函数覆盖率。

## 8. 建议修复顺序

1. **先修 QR-03**：未决副作用与 retry/terminal 的矛盾风险最大，并把 guard 收敛到 Store 原子事务。
2. **再修 QR-02**：为两个 ADK 引擎建立真正独立于事件 EOF 的完成证明。
3. **修 QR-01**：恢复 durable accepted 的永久幂等查回能力。
4. **修 QR-04**：升级 citation token/identity，避免继续产生错误引用事实。
5. **联动修 QR-05、QR-06、QR-07**：把客户端 intent、会话投影和附件/知识入库语义补成端到端闭环。
6. **修 QR-08**：在 Worker 激活 release 前验证三代引擎的实际工具面。
7. 更新 Query→Answer 阅读指南，把 happy path 与 failure/recovery path 分开，并同步所有事实所有权和诚实边界。
8. 单独讨论集群化 ADR；不要把它和上述 R0 correctness 修复混成一次无边界重构。

## 9. 最终评审意见

这套实现的方向是对的，尤其是“持久事实优先于 HTTP/SSE/Trace”“Coordinator 唯一终态”“副作用有账本”“Evidence 可审计”这些核心选择。但可靠系统最危险的地方通常不是主流程，而是两个看似正确的局部契约在接缝处失效。本次 4 个 P1 正好都属于这种情况：Service 抢在 Store 前做时效裁决、Engine 把 EOF 包装成显式结果、Coordinator 只在成功分支检查副作用、citation 用局部显示序号关联全局证据。

建议在上述 P1 修复并加入跨层故障测试后再做一次复审。当前可以继续将项目描述为“具备生产级可靠性设计思想的单机参考实现”，但不应描述为“Query→Answer 全链路可靠性已经闭环”，也不应在没有新 ADR 和共享权威组件前描述为可直接集群化部署。
