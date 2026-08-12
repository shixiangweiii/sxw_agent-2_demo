# Failure Matrix v1

状态：**FROZEN**

本矩阵规定失败/竞态后的唯一业务结论。若实现无法判断外部副作用是否发生，必须保留 `UNKNOWN`，不得为了让流程结束而猜测 `FAILED` 或重新执行。

## 1. Admission、API 与 SSE

| 场景 | 最后 committed 边界 | 必须结论 | 恢复 / 客户端动作 | 禁止 |
|---|---|---|---|---|
| Admission 事务提交前崩溃 | 无 Run | 未受理 | 同 Idempotency-Key 重试可创建一次 | 返回/推断 202 |
| Admission commit 后、HTTP 202 前丢失 | Run + 首 Activity/Event | durable accepted | 同 key+digest 返回原 Run，`reused=true` | 创建第二 Run；先报 conversation busy |
| 同 key、不同 payload | 原 `run_requests` | 409 `IDEMPOTENCY_KEY_REUSE` | 使用新 key 或读取原请求 | 覆盖 digest/Run |
| 同 conversation 两个真正新请求并发 | 一个 admission CAS/唯一索引获胜 | 一个 202，一个 409 `CONVERSATION_BUSY` | 等前一 Run terminal 后再建 | 用进程锁代替 DB 唯一约束 |
| 请求 commit 后、claim 前 kill | Run=`DISPATCH_PENDING`、Activity=`PENDING` | Run 保留 | Worker 重启扫描并领取一次 | API 连接关闭取消 Run |
| SSE 在任意位置断开 | committed events 不变 | Run/Worker 不变 | 客户端以 last seen seq 重连 | 隐式 cancel；写 delivery cursor |
| SSE 在 terminal commit 前断开 | 可能只见 partial | Run 继续 | GET status / replay | 以 EOF 推断成功 |
| terminal commit 后、SSE 读取前 kill | terminal 已持久化 | terminal 唯一且可重放 | API 恢复后 replay 到 `terminal` | 再次 finalize；发送旧 `done` |
| visibility 过滤导致 seq 跳号 | 全局 seq 已 committed | 合法 | cursor 更新到实际看到的 seq；查询 `>` | 要求可见 seq 连续 |

## 2. Worker、lease、LLM 与 checkpoint

| 场景 | 最后 committed 边界 | 必须结论 | 恢复 | 禁止 |
|---|---|---|---|---|
| claim 前 Worker kill | Activity=`PENDING` | 可由任一恢复 Worker 领取 | 固定排序 + `UPDATE ... RETURNING` | Run=`FAILED` |
| claim 后、LLM 前 kill | Activity=`CLAIMED/RUNNING`、lease/fencing 已提交 | Run 仍非终态 | lease 到期 classifier requeue；fencing 增长 | 立即失败 Run |
| LLM 返回后、event commit 前 kill | 没有模型输出事实 | 当前 model Activity 可重试 | 按 Engine 恢复粒度重新调用 | 从 Trace/内存恢复未提交输出 |
| 部分 delta 已 commit 后 kill | `OUTPUT_GENERATION_STARTED` + 若干 `OUTPUT_DELTA_COMMITTED` | 旧 generation 可重放审计，但不是 final history | 以 `reason=recovery` 创建新 generation；客户端只重置正文，最终语义只看 final message | 删除已提交 delta；追加到旧 generation；把 partial 纳入 conversation |
| provider 空流、usage-only 或 silent EOF | 最多只有 `MODEL_REQUEST`/未完 generation | 不合成 TurnEnd；`MODEL_STREAM_INCOMPLETE` 可重试 | 新 generation 重试，且未经 PREPARE 不得 dispatch | 当作空成功或正常 `stop` |
| checkpoint 两写并发 | 一个 expected revision 获胜 | 另一个丢失 attempt ownership | `AttemptOwnershipLost` 冒泡给 Worker，旧 attempt 停止 | last-write-wins；转成 ToolResult 或 Run terminal |
| lease 已过期，旧 Worker 返回 | 新 fencing 已生效 | 旧结果拒绝并转为 `AttemptOwnershipLost` | 当前 owner 继续；late result 仅诊断 | 接受“先返回者”覆盖；把 ownership loss 显示给模型 |
| Worker heartbeat 消失 | heartbeat stale | 仅运维信号 | 以 Activity lease 恢复 | 据此直接失败所有 Run |
| trace disabled/写失败 | Runtime DB 不受影响 | 执行与恢复继续 | 记录 best-effort 诊断告警 | 依赖 trace 裁决/恢复 |
| final message commit 前 kill | 无 `ASSISTANT_MESSAGE_COMMITTED`/terminal | Run 非成功 | Activity 恢复后重新 finalize | 单独保存 final 后补 terminal |
| Native `COMPLETED` checkpoint 已 commit、成功 terminal 前 kill | checkpoint 含精确 final text/message/generation | 不再请求模型或重放 delta | 从 checkpoint 继续 final transaction | 用模型重新生成最终答案 |
| final terminal 事务重试 | 第一次可能已整体提交 | 最多一个 terminal | CAS/幂等读取既有 terminal | 第二个 terminal/seq batch |

## 3. ToolEffect 与 cancel 竞态

| 场景 | ToolEffect | 必须结论 | 恢复 | 禁止 |
|---|---|---|---|---|
| Tool prepare 前 kill | 无 ToolExecution | 未 dispatch | model/activity 重试后稳定 slot 重建 | 声称调用过 |
| Prepare committed、dispatch 前 kill | `PREPARED` | 明确未 dispatch | recovery 按策略 dispatch 一次，或 cancel 时 `FAILED/not_dispatched` | 标 UNKNOWN；生成新 slot |
| `DISPATCHED` commit 后、真正 I/O 前 kill | `DISPATCHED` | 保守视为可能已发出 | READ_ONLY 可按规则重试；side-effect 先 reconcile/manual | 对 side-effect 当普通失败重试 |
| Tool 执行中进程 kill/timeout | `DISPATCHED` | 无证据则 `UNKNOWN` | supports_reconcile 则查询，否则 manual | 猜测失败 |
| Tool executor 在 durable dispatch 后抛 Runtime control/contract fault | ownership fault 保持 `DISPATCHED`；其余按 class 先结算 | `AttemptOwnershipLost` 原样冒泡且不写 ToolResult；其余 RuntimeFault 为 READ_ONLY=`FAILED`、effectful=`UNKNOWN`，并保留原 code | 新 owner lease recovery，或 Coordinator 按计划失败进入下述 strict boundary | 用 `except Exception` 把 ownership loss 变成模型结果；未结算 effect 就直接失败 Run |
| Tool 成功但 ACK/Runtime result commit 丢失 | `DISPATCHED → UNKNOWN` | 可能已经发生 | 使用稳定 idempotency/external ref reconcile | 再发 NON_IDEMPOTENT/UNKNOWN tool |
| 父 lease 丢失，账本含不安全 `DISPATCHED/UNKNOWN` | child=`RUNNING/RECONCILE` | 同一 recovery 事务保留有界 UNKNOWN/correlation 并转 `MANUAL_REQUIRED/MANUAL` | strict pending 只列 operator-actionable IDs；READ_ONLY/稳定幂等 remainder 仍按冻结 guard 重排 | 把不安全 ID 留在不可领取状态；把 replay-safe ID伪装成人工项；重发 UNKNOWN/NON_IDEMPOTENT |
| 自动 reconcile 无结论，进入人工处置 | `MANUAL_REQUIRED` + Tool Activity=`MANUAL` | 只接受匹配当前 pending IDs 的 `tool_reconciliation` | `mark_committed` 用成功证据/result/ref 收口；`mark_failed` 用未提交证据形成 sticky failure；`reconcile` 仅在持久化 capability 存在时授权再次查询 | 普通 signal 只唤醒父 Activity；未改 effect 就重跑；复制大 result/evidence 到 DB/Event |
| 人工 `action=reconcile` 已提交、query 尚未开始 | `RECONCILING` + child=`PENDING` + exact marker | 授权只产生 SIGNAL/Activity/Run 状态事件，不产生 ToolResult；Coordinator 在 Engine/checkpoint/history 前只路由 query hook | 校验冻结 tool name/release/capability/effect revision；原 executor 与 EngineAdapter 均不可达 | 把授权伪装成成功/失败 ToolResult；走普通 engine/tool dispatch |
| reconcile-only Worker 在 hook 前或执行中 kill/lease 过期 | `RECONCILING` + child=`PENDING/RECONCILE` | 原副作用没有获得重发授权；恢复为 `MANUAL_REQUIRED/MANUAL`，parent 回 reconcile wait，清 marker/lease | 旧 signal 仅幂等返回；新 strict signal 可再次选择 reconcile/mark_*；cancel-owned Run 始终保持 `CANCEL_REQUESTED` | 自动再次 query；恢复为普通 RUNNING；重发原副作用 |
| hook 已提交 `COMMITTED/FAILED/MANUAL_REQUIRED`，父 query settlement 前 kill | 已有 canonical ToolResult，parent 留 exact marker | committed effect 是 authority；recovery 清旧 marker | 普通 Run 无其他 unresolved 时重排 Engine，从账本复用；cancel Run 无 unresolved 时收口；deadline 已到优先 `TIMED_OUT` | 用旧 marker 再调 hook；用 lease recovery 覆盖绝对 deadline |
| hook 缺失、release digest 漂移、异常或无结论 | `RECONCILING` | fail closed 回 `MANUAL_REQUIRED/MANUAL` | 保持普通 `WAITING_INPUT` 或 cancel `CANCEL_REQUESTED`，接受新 strict signal | 借当前新 release 解释旧 effect；回退原 executor |
| 两个不同人工处置信号并发 | `MANUAL_REQUIRED` | 首个 `BEGIN IMMEDIATE` + effect revision CAS 获胜并恢复父 Run；另一个因 boundary/effect 已变化 409 | 获胜 signal 可按相同 ID/digest 幂等重放 | 两次 TOOL_RESULT、两次外部查询、后到者覆盖已裁决 effect |
| 人工 `result_ref` 不存在或 ID/Run/pending 不匹配 | `MANUAL_REQUIRED` 不变 | 整个 signal 事务回滚，无 signal/event/link/父唤醒 | 修正 Artifact/ID 后使用新 signal_id | 部分消费 signal；孤立 Artifact Link；猜测目标 ToolExecution |
| ToolResult status、ref 或外部 identity 与账本矛盾 | 原 effect 不变 | effect/result matrix、nested/column/Event/Link 和已知 `external_object_id` 必须一致；整笔 fail closed | 使用 canonical ToolResultEnvelope 与正确 Artifact metadata 重试 | COMMITTED+FAILURE；UNKNOWN 无 error；metadata link 指向另一个 ref；稳定 slot 更换外部对象 |
| Tool 明确失败且未产生 effect | `FAILED` | 可按 manifest 判断 retry | READ_ONLY 或受支持的幂等 effect 可同 execution/key 增 attempt | 给非幂等 tool 透明 retry |
| Engine/Adapter 已判普通 terminal failure，但仍有 unresolved ToolEffect | uncertainty 原子转 `MANUAL_REQUIRED/MANUAL`，父=`RECONCILE`，Run=`WAITING_INPUT` | `pending_input.pending_terminal={status:FAILED,code,message}` sticky 保存原计划终态 | 只接受 strict tool_reconciliation；最后一个 effect 确定后原子提交原 FAILED | 直接 FAILED 留下无主账本；丢失原错误；重新请求模型 |
| pending FAILED boundary 的最后一个 `mark_committed/mark_failed` | effect=`COMMITTED/FAILED` | ToolResult/Signal/Activity 与原 `FAILED + RUN_TERMINATED` 同一事务 | 返回 terminal Run；Engine 不再领取 | 解决 effect 后改成成功；先重排 Engine 再失败 |
| pending FAILED 的 query hook 已 settle、父 Worker 在 query settlement 前 kill | effect 已确定，pending terminal 仍 durable | lease recovery 直接提交原 FAILED；若 deadline 已先到则 TIMED_OUT | 清理 parent lease/marker 与 terminal 在同一恢复事务完成 | 清 pending terminal 后普通 claim；重查/重发原 effect |
| 已 `COMMITTED` 的 Tool slot 重放 | `COMMITTED` | 返回已保存完整结果/ref | 不调用外部系统 | 重复副作用 |
| 同 slot tool name/digest/release/effect 语义改变 | 任意已有状态 | Store 与 Broker 都从冻结账本 fail closed `TOOL_REPLAY_MISMATCH` | Run terminal failure/人工分析 | 用当前更“安全”的 manifest 解释旧 UNKNOWN；按 args hash 猜测匹配 |
| terminal 先 commit，随后 cancel | terminal | 409 `RUN_ALREADY_TERMINAL` | 返回现有 terminal | 覆盖成 CANCELLED |
| cancel CAS 先 commit，无 dispatch | Run=`CANCELLED` | 后续执行结果无权提交 | fencing/CAS 拒绝 late result | SUCCEEDED 覆盖 cancel |
| cancel CAS 先 commit，有 `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` | Run=`CANCEL_REQUESTED` | 等安全边界/reconcile | resolved 后 CANCELLED；deadline 未解则 TIMED_OUT + IDs | 立即宣称无副作用取消 |
| cancel 已先提交，收到人工 mark_* | Run=`CANCEL_REQUESTED` + `MANUAL_REQUIRED` | effect/child/result/signal 同一事务提交；仍有 unresolved 则继续 `CANCEL_REQUESTED`，最后一个才唯一 `CANCELLED` | terminal payload 记录本次 signal、全部已解决与剩余 execution IDs；terminal insert 失败全部回滚 | 恢复普通 Engine；部分消费 signal；effect 已改但 terminal/event 丢失 |
| cancel 已先提交，收到人工 `reconcile` | Run=`CANCEL_REQUESTED` | 只把 exact reconcile-only marker 交给 Worker；claim 不把 Run 改成 RUNNING | hook 确定且无其他 unresolved 后 `CANCELLED`；仍不明回人工；deadline 优先 `TIMED_OUT` | 调 EngineAdapter；执行原 Tool；把 Run 复活为正常执行 |
| replay-safe effect 已 recovery 到 `DISPATCH_PENDING`，replacement claim 前 cancel | parent=`PENDING`，effect=`DISPATCHED/UNKNOWN` | cancel 接管 idle boundary，把剩余 uncertainty（含 replay-safe）转 `MANUAL_REQUIRED/MANUAL` | parent=`RECONCILE`、pending 只列可人工处理 IDs；最后一个 resolution 后 `CANCELLED` | 让 replacement Worker 重发；pending 列出 strict signal 无法处理的 ID |
| cancel 在 reconcile hook 内/结论提交后获得所有权 | target=`RECONCILING/COMMITTED/FAILED`，可能有 safe remainder | target 结论按 CAS 保留；其余 uncertainty 原子 manualize，Run 始终 `CANCEL_REQUESTED` | hook 无结论立即回 strict boundary；有结论后逐个处置，最后才 `CANCELLED` | 抛 invariant 留 parent RUNNING；恢复 Engine 或原 executor |
| claim 后、Coordinator 调 Engine 前 cancel | Activity 有有效 fence，Run=`CANCEL_REQUESTED` | Coordinator 在 registry/checkpoint/history/adapter 前直接 cancel settlement | 无 unresolved 则唯一 `CANCELLED`；有 unresolved 则严格 reconcile | 先调用 LLM/EngineAdapter 再把 outcome 改 CANCELLED |
| Tool complete 与 cancel 并发 | 由 DB commit 顺序决定 | terminal 先：cancel 409；cancel 先：结果 late、不得 success terminal | 两种顺序分别测试 | 按线程到达时间/内存 flag 决定 |
| unresolved effect 到 deadline | `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` | Run=`TIMED_OUT` | terminal payload 列出 `unresolved_tool_execution_ids`；账本原状态可留作 terminal 审计且不可再领取 | 改为 FAILED/CANCELLED 或清空 ToolEffect |
| 任意非 timeout terminal 入口发现 unresolved effect | 原 Run/ToolEffect 不变 | Store 最后一层 `UNRESOLVED_TOOL_EFFECTS` fail closed；普通 planned FAILED 使用专用 deferred boundary | 修复调用路径或进入 strict reconciliation | 只在 Coordinator success 分支检查；让旁路 terminal 穿透 |

## 4. Timer、Signal 与 HITL

| 场景 | 最后 committed 边界 | 必须结论 | 恢复 | 禁止 |
|---|---|---|---|---|
| retry timer 被两个扫描器同时发现 | Timer=`SCHEDULED` | 一个 CAS fired | 只创建一次 PENDING 唤醒 | 两次 attempt/Activity |
| WAITING_INPUT 中 Worker/API 重启 | checkpoint + pending input + Activity/Run waiting | 等待状态保留且不占 Worker | signal 后恢复 | 依赖内存 Future/queue |
| Engine 返回 generic WAITING_INPUT，但已有 Tool uncertainty | ToolExecution authority 优先 | 不安全 effect 先转 manual 并建立 strict Tool reconciliation；若 remainder 全部 replay-safe 则重排而不是等待普通输入 | ordinary signal 被拒绝；只有 matching `tool_reconciliation` 可处理 manual ID | 用 APPROVAL 覆盖 pending_input 后恢复普通 Engine |
| 相同 signal 重放 | signal row+digest | 返回原结果，只消费一次 | 幂等响应 | 二次唤醒 |
| 同 signal_id 不同 digest | 原 signal | 409 `SIGNAL_REPLAY_MISMATCH` | 新意图用新 ID | 覆盖 payload |
| signal commit 后、resume claim 前 kill | Run/Activity 已 DISPATCH_PENDING/PENDING | 恢复后领取一次 | Worker 扫描 | 重新消费 signal |
| terminal 后迟到 signal | terminal | 写 `REJECTED_LATE` 审计后 409 | 无 Run 迁移 | 静默丢弃或复活 Run |
| WAITING_INPUT deadline 到与 signal 并发 | DB commit 顺序 | signal 先：正常 resume；timeout terminal 先：signal late rejected | CAS 决定 | 内存时钟竞态给两个结论 |

## 5. Artifact、RAG 与 release

| 场景 | 最后 committed 边界 | 必须结论 | 恢复 | 禁止 |
|---|---|---|---|---|
| Artifact temp write 失败 | 无 CAS blob/metadata | 上传失败 | 清理 temp | metadata commit |
| rename+fsync 后、metadata commit 前 kill | orphan CAS blob | 未发布 Artifact | 24h orphan cleaner；同内容重试可复用 blob | 认为 metadata 必然存在 |
| metadata committed 后 blob 被篡改 | metadata digest 与字节不符 | `ARTIFACT_INTEGRITY_ERROR` | 拒绝 Range/model read，告警/恢复原内容 | 返回部分或新 digest 覆盖旧 ID |
| Event/Checkpoint 引用大结果 | Artifact 已 committed | 只保存 ref + 有界 preview | `read_artifact` 有界读取 | 复制完整内容到 DB/trace |
| index BUILDING 中 kill | staging version/job 非 active | 旧 active version 继续可见 | 启动扫描按状态恢复/安全重建 | 暴露部分新 chunks |
| 文档新版本更短 | 新版本验证并原子激活 | 旧 version/chunks 不可检索 | 后台延迟清理无影响 | 按 chunk upsert 留尾巴 |
| vector/BM25 丢失或 checksum 坏 | Document/chunks truth 仍在 | Retrieval=`DEGRADED` | 从 active truth 重建后原子换 snapshot | 把索引当权威或返回正常 MISS |
| 一路召回失败、另一路成功 | committed EvidenceSet | `DEGRADED`，保留有效 hits | 主 Agent best-effort 继续 | 标 `HIT` 隐藏失败或标 `MISS` |
| scope/ACL 失败 | 无可授权 evidence | `DENIED` | 主 Agent按策略继续 | 当 MISS/ERROR |
| Run release 与 Worker 不匹配 | Run 冻结 fingerprint | exact-claim SQL 不领取，不产生 Run 终态 | 匹配 Worker 恢复；否则 Run 保持 pending 并由绝对 deadline 收口 | 静默用当前 active release；Coordinator 把调度错配终态化 |
| 新 fingerprint 激活时存在活跃旧 Run | 旧 Run 与三个 active pointer 仍完整 | `ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION`，三 pointer 全部不变 | 等旧 Run 终态后重试激活 | 部分切换 pointer；让新 Worker 解释旧 checkpoint |
| DB schema identity 不符 | 完整 current `schema.sql` digest 不同或非空库缺 `schema_meta` | `CURRENT_SCHEMA_MISMATCH`，API/Worker/ARAG 启动 fail-fast | 由使用者显式删库重建 | 自动迁移、修补或重写 identity 后运行 |

## 6. Deadline 的统一规则

Deadline 是冻结在 RuntimeEnvelope 的绝对 UTC 时间。每层只计算 `deadline_at - Clock.now()` 的剩余预算并向 LLM、HTTP、Skill、Sandbox 传递；不得在每层重新开始完整 timeout。deadline terminal 与 signal/cancel/tool completion 都由数据库提交顺序和 CAS 决定。

副作用入口采用双重 deadline guard：Store 在 `mark_tool_dispatched/mark_tool_reconciling` 的短事务内要求 Run 仍有执行所有权且 `now < deadline_at`；事务提交后 Broker 在紧邻 executor/query hook 前重新计算剩余时间，`<= 0` 时绝不调用外部系统。已经落下的 `DISPATCHED/RECONCILING` 由 deadline/recovery 收口，不能用最小 1ms timeout 启动一次迟到副作用。
