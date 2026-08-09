# ADR-0001：Runtime 事务边界

- 状态：Accepted / Frozen
- 日期：2026-08-09
- 适用：R1–R4

## Context

Runtime 必须在 API 与 Worker 分进程、外部 LLM/Tool/RAG/Skill 不可事务化的前提下，给 durable accepted、连续 event seq、checkpoint CAS、Tool effect、Artifact 和唯一 terminal 明确承诺。

## Decision

所有 SQLite 写操作使用短 `BEGIN IMMEDIATE`。事务内只做确定性校验、CAS 和本地数据库写入；禁止 LLM、Tool、RAG、Skill、A2A、Sandbox、文件系统 I/O、sleep 或等待人工。

### TB-1 Admission

顺序不可交换：

1. 规范化 Pydantic 请求，保留 `attachment_refs` 顺序，计算确定性 JSON SHA-256；
2. 先按 `(principal_id, agent_id, idempotency_key)` 查询/插入 `run_requests`：相同 digest 返回原 Run，不同 digest 返回 `IDEMPOTENCY_KEY_REUSE`；
3. 只有真正的新请求才检查 active release 与 conversation 非终态唯一约束；
4. 创建/锁定 conversation 并分配下一 turn；
5. 生成并冻结 RuntimeEnvelope；
6. 同一事务插入 request、conversation/turn、Run、首个 Activity、`USER_MESSAGE_COMMITTED`、必要的 status events，并更新 `runs.next_seq`；
7. commit 后才返回 HTTP 202/Location。

提交前崩溃：请求未受理，可安全重试。提交后响应丢失：重放同 key 返回同一 Run。幂等重放优先于 `CONVERSATION_BUSY`。

### TB-2 Event append 与 seq

在单一事务中：

1. 读取并 CAS `runs.revision/next_seq`；
2. 为待插入 batch 按顺序赋 `next_seq ... next_seq+n-1`；
3. 插入全部 `run_events`；
4. 将 `runs.next_seq` 更新为 `next_seq+n`；
5. commit。

回滚同时撤销 event 和 `next_seq` 更新，因此不产生 seq 洞。禁止事务外预留号段。`UNIQUE(run_id, seq)` 与 `UNIQUE(event_id)` 是最后防线；update/delete trigger 保证 append-only。

### TB-3 Checkpoint CAS

在单一事务中验证：Run 非终态、Activity fencing 有效、`expected_checkpoint_revision` 匹配。随后 append checkpoint revision，更新 Run 当前 checkpoint 指针/WorkingState 关联，并插入 `CHECKPOINT_COMMITTED` event。任一步失败全部回滚；旧 revision 返回 `CHECKPOINT_REVISION_CONFLICT`，不得 last-write-wins。

Checkpoint 只包含 WorkingState v1 与引擎状态/引用、release/schema；Activity 完成情况从 `activities` 查询，不复制进 WorkingState。

### TB-4 Tool Broker（三段式）

**Prepare 事务**：创建/确认稳定 Tool Activity 与 ToolExecution(`PREPARED`)，校验 replay slot 的 tool name/request digest，append `TOOL_CALL_COMMITTED`；commit。

**Dispatch 标记事务**：在真正调用下游前将 effect CAS 为 `DISPATCHED` 并 commit。Store 同时验证 Run 仍为 `RUNNING`、绝对 deadline 未到，并从冻结 ToolExecution 账本验证 replay-safe guard；`PREPARED` 可首发，已有不确定状态只有 READ_ONLY 或带稳定 key 的 `IDEMPOTENT_EFFECT` 可受控再发。随后事务外执行 Tool，透传稳定 idempotency key、deadline 和调用上下文。Broker 在紧邻外部 I/O 前再次计算剩余时间，`<= 0` 时不得靠最小 timeout 启动调用。

**Result 事务**：以 Activity revision/lease/fencing CAS 提交 `COMMITTED | FAILED | UNKNOWN`、result/payload_ref、external ref、sticky error，append `TOOL_RESULT_COMMITTED` 并迁移 Activity/Run。stale fencing 一律拒绝；完整 `COMMITTED` 结果可直接复用。Store 先把公开字段归一成冻结 ToolResultEnvelope，并强制 `COMMITTED→SUCCESS|NO_OUTPUT|INTERRUPT`、`FAILED→FAILURE`、`UNKNOWN/MANUAL_REQUIRED/RECONCILING→UNKNOWN`。`result_ref` 的 envelope/列/metadata/Event/Artifact Link 必须一致；已知 `external_object_id` 单调保留且不得改变，任何矛盾在写事实前整体回滚。

`DISPATCHED` 后没有确定结果的 timeout/kill/ACK 丢失只能进入 `UNKNOWN`，不能伪装普通失败。`NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT` 的 UNKNOWN 后只允许确定性 reconcile/manual；父 lease 或安全 wait boundary 丢失时，在同一 recovery 事务中保留有界 UNKNOWN result/error/ref/external correlation 并转 `MANUAL_REQUIRED/MANUAL`。READ_ONLY 或携带稳定下游 idempotency key 的 `IDEMPOTENT_EFFECT` 可先 reconcile，在未发现已提交结果且 attempt 未耗尽时以同一 ToolExecution/key 受控重试。Store 与 Broker 都按冻结 release/effect/capability guard；当前 manifest 漂移不可改变旧账本语义。人工 `action=reconcile` 只授权 query-only marker，绝不授予原 executor 重发权限。

### TB-5 Artifact

数据库事务外：在同一文件系统写临时文件、增量计算 sha256/size、`fsync(file)`、原子 rename 到 CAS 路径、`fsync(directory)`。内容文件 durable 后，才在短事务中 upsert `artifact_metadata`、插入 `artifact_links`，并在需要时 append 引用 event。

rename 后、metadata commit 前崩溃只产生 orphan blob；它不是已发布 Artifact，由“无 metadata/link 且超过 24 小时”清理任务回收。绝不允许 metadata 指向未 durable 的 blob。

### TB-6 Finalize / terminal

Coordinator 独占 terminal 权限。在 terminal 事务前强制 flush delta。成功终态在同一事务中提交：

- `ASSISTANT_MESSAGE_COMMITTED`；
- 从 committed EvidenceSet 确定性生成的 `CITATION_SET_COMMITTED`；
- `RUN_STATUS_CHANGED(SUCCEEDED)`；
- `RUN_TERMINATED(terminal_status=SUCCEEDED)`；
- `runs` terminal payload/status/revision/next_seq；
- 当前 Activity 的成功终态。

任一步失败全部回滚，因此不会出现“可进入历史的 final message，但 Run 未成功”或相反情况。失败/取消/超时/不兼容 terminal 同样在一个事务中提交唯一 terminal 字段和 `RUN_TERMINATED`，但不提交成功 assistant message。

### TB-7 Cancel

按 `(run_id, command_id)` 幂等。在一个事务中读取 Run/ToolEffect 并 CAS：

- terminal 已提交：记录/返回 `RUN_ALREADY_TERMINAL`，不改状态；
- 无 dispatched/unknown effect 且处于可直接取消状态：`CANCELLED + RUN_TERMINATED`；
- 有执行中或 unresolved effect：转 `CANCEL_REQUESTED`，append `CANCEL_REQUESTED` event，交给安全边界/reconcile。

cancel CAS 先提交时，任何旧执行者的 success 结果均因 revision/fencing 被拒绝为 terminal；可记 late result 诊断事实。

若 replay-safe effect 已因 lease recovery 回到 idle `PENDING/DISPATCH_PENDING`，cancel 事务必须立即接管：把剩余 `DISPATCHED/UNKNOWN/RECONCILING` 原子转为 `MANUAL_REQUIRED/MANUAL`，parent 进入严格 `RECONCILE`，不得留给 replacement claim。Coordinator 已领取但尚未进入 Adapter 时也先读 Store cancel authority；无 effect 直接用当前 fence 收口，有 uncertainty 则建立同一人工边界，任何 Engine/LLM 调用均不可达。

### TB-8 Signal

按 `(run_id, signal_id)` 幂等并保存规范化 digest：相同 digest 返回原结果，不同 digest 返回 `SIGNAL_REPLAY_MISMATCH`。普通 HITL signal 匹配 waiting Activity 时，在同一事务中插入 signal、标记首次消费、Activity `WAITING_INPUT → PENDING`、Run `WAITING_INPUT → DISPATCH_PENDING`、append `SIGNAL_RECORDED`。terminal Run 的 signal 插入 `REJECTED_LATE` 审计行后返回 409。

普通 signal 事务在消费前必须先检查 ToolEffect authority：存在 `MANUAL_REQUIRED` 时只能保留/建立 reserved reconciliation boundary，存在 ownerless 不安全 uncertainty 时先转 manual；若剩余全部 replay-safe，则重排 Engine 而不是接受 generic pending input。由此 Engine 的 `WAITING_INPUT` outcome 不能覆盖既有 Tool reconcile，也不能用普通 approval 绕过副作用裁决。

`type=tool_reconciliation` 是 reserved signal：事务先校验绝对 deadline、当前 pending boundary 精确列出目标 ToolExecution，且 effect/activity 为 `MANUAL_REQUIRED/MANUAL`。`mark_committed/mark_failed` 分别 CAS 为 `COMMITTED/FAILED`，同步 Tool Activity 为 `SUCCEEDED/FAILED`，append 有界 `TOOL_RESULT_COMMITTED` 与状态事件；`result_ref` 同事务验证并建立 Artifact Link。人工 `FAILED` 写 sticky reconcile state。一次 signal 只解决一个 execution；还有其他 unresolved 时保持人工边界，最后一个才恢复普通 Run，或在 cancel 已获所有权时同事务提交唯一 `CANCELLED` terminal。

`reconcile` 只 CAS 为 `RECONCILING/PENDING`、消费 signal、append `SIGNAL_RECORDED`/状态事件，并由 Store 生成 exact `{kind,tool_execution_id,signal_id,expected_effect_revision}` marker；授权本身不写伪 ToolResult。Coordinator 在 Engine registry/checkpoint/history 前识别 marker，Broker 校验冻结的 tool release/capability/revision 后只执行 query hook。query 标记事务和 hook 紧邻入口各检查绝对 deadline。hook 的确定结果才写 ToolResult；无 hook、release mismatch、异常/无结论回 `MANUAL_REQUIRED/MANUAL`。已有 UNKNOWN 的 ref/external identity 跨授权、query 和中断恢复保留并提供给 hook；hook 新发现 correlation 先 durable settle，再返回人工边界。query settlement 以 parent fence、exact marker、signal、effect revision 和 deadline 再 CAS：普通 Run 的剩余 effect 若全 replay-safe 则重排 Engine，只有 operator-actionable `MANUAL_REQUIRED` IDs 才继续等待；cancel 获得所有权时先把剩余 uncertainty（含 replay-safe）转为 manual，最后一个解决后才 `CANCELLED`。Worker 在 hook 前/执行中丢失时 recovery 把 effect/child 恢复 `MANUAL_REQUIRED/MANUAL` 并清 marker；hook 结果已提交但父 settlement 丢失时清旧 marker并从 committed result 恢复，绝不重查或重发原副作用。任何 ID、状态、能力、Artifact、release、revision 或 fencing 不匹配都整体 fail closed。

### TB-9 Timer

Timer 唯一身份和 revision CAS 保证 `SCHEDULED → FIRED` 一次；同一事务中完成 Run/Activity 唤醒。重复扫描返回幂等成功，不创建第二次唤醒。

## Consequences

- HTTP/SSE/Worker 生命周期与业务提交解耦。
- 外部副作用无法跨库原子化，因此必须显式建模 UNKNOWN/reconcile。
- 更频繁的小事务和 output 聚合换取可证明的崩溃边界。
- 所有 fault injection 都可以定位为“最后一个已提交边界之后重做/恢复”。
