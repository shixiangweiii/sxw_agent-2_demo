# Reliability Test Catalog v1

目标状态：**FROZEN**。实现覆盖会演进，当前证据见 [REL/FI 实现覆盖追踪](implementation-coverage.md)；pytest 全绿不自动把 PARTIAL/GAP 视为满足本目录的冻结目标。

可靠性门禁使用 pytest、pytest-asyncio、FakeClock、FakeRandom、ScriptedEngine、fake LLM/tools、临时 SQLite/Artifact 根目录；真实 LLM 不参与 PASS。测试 ID 是 R1–R4 退出条件的稳定引用，重命名测试函数不得改变 ID。

## 1. 30 项自动化门禁

| ID | 阶段 | 规范断言 |
|---|---|---|
| `REL-001` | R1 | 同一幂等 key 并发/串行重放 10 次只有一个 Run，全部指向同一 run/turn/conversation |
| `REL-002` | R1 | 同 scope key 不同规范化 digest 返回 409 `IDEMPOTENCY_KEY_REUSE`，原映射不变 |
| `REL-003` | R1 | 同 conversation 两个真正新 Run 并发，只有一个成功，另一个 409 `CONVERSATION_BUSY`；幂等重放优先 |
| `REL-004` | R1 | 每个 Run 最多一个 terminal 字段和一个 `RUN_TERMINATED`，并发 finalize 只有一个 CAS 获胜 |
| `REL-005` | R1 | terminal 后不能回退或被 cancel 覆盖；cancel 返回 `RUN_ALREADY_TERMINAL` |
| `REL-006` | R1 | `(run_id, seq)` 唯一；event batch 回滚时 `next_seq` 同步回滚、不留 seq 洞 |
| `REL-007` | R1 | `ASSISTANT_MESSAGE_COMMITTED + CITATION_SET_COMMITTED + SUCCEEDED terminal` 原子提交；任一点注错全部不可见 |
| `REL-008` | R1 | SSE/Event reader 只能读取 committed event，未提交 writer transaction 的 event 永不可见 |
| `REL-009` | R1 | 从任意 `after_seq`/Last-Event-ID 重放，对所有可见事件无丢失、无重复；允许 visibility 导致跳号 |
| `REL-010` | R1 | SSE 断开/取消订阅不取消 Worker/Run，之后可重连并读到 terminal |
| `REL-011` | R1 | checkpoint expected revision 冲突返回 `CHECKPOINT_REVISION_CONFLICT`，旧 checkpoint 保留且不会静默覆盖 |
| `REL-012` | R1/R3 | 过期 fencing token 的 Activity/result/checkpoint/terminal 提交全部被拒绝 |
| `REL-013` | R1/R3 | Activity lease 过期后只被重新领取一次，attempt/fencing 单调增长，旧 owner 无提交权 |
| `REL-014` | R1/R3 | 同一 timer 被并发扫描时只从 SCHEDULED 触发一次，Activity 只唤醒一次 |
| `REL-015` | R1/R3 | WAITING_INPUT 时销毁 API/Worker/内存状态，重启后仍可用 committed signal 恢复 |
| `REL-016` | R1/R3 | signal 重放只消费一次；同 ID 不同 digest 冲突；terminal 的迟到 signal 写 `REJECTED_LATE` 后 409 |
| `REL-017` | R2A | 同一稳定 ToolExecution 重放/恢复不重复提交副作用；COMMITTED 复用完整 result/ref |
| `REL-018` | R2A/R3 | Tool 成功但 ACK/result commit 丢失进入 UNKNOWN，并经 reconcile 或 manual 确定性收口 |
| `REL-019` | R2A | NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT 无确认能力时不透明自动重试；READ_ONLY/幂等 guard 分别验证 |
| `REL-020` | R2A/R3 | cancel 与 Tool complete 两种提交顺序均得到冻结结论；cancel-first 不可 SUCCEEDED，terminal-first cancel 409 |
| `REL-021` | R2A | Artifact rename+fsync 后 metadata transaction 失败只产生可清理 orphan，绝不产生有效 ArtifactRef |
| `REL-022` | R2A | Artifact 字节篡改后普通读取、Range、模型 adapter 都返回 `ARTIFACT_INTEGRITY_ERROR` |
| `REL-023` | R2A | 大 Tool 结果完整内容只存在 Artifact；Event/Checkpoint/Trace 只有 ref + 受限 preview，未复制 payload |
| `REL-024` | R2B | 同文档激活更短新版本后，旧 active version 的尾部 chunks 不可检索 |
| `REL-025` | R2B | 删除/corrupt vector 与 BM25 投影后，从 active Document/chunks truth 可重建等价 snapshot |
| `REL-026` | R2B | Retrieval `HIT/MISS/DEGRADED/DENIED/ERROR` 在双路结果、ACL 与 transport 组合下稳定判定 |
| `REL-027` | R2B | 每个 citation 可经 evidence_id 追溯到 document version、index version、chunk hash、page/span、scope 与 query |
| `REL-028` | R1/R3 | release/schema 完全匹配可恢复；显式 upgrader 可升级；无 upgrader 收口 `INCOMPATIBLE_RELEASE` |
| `REL-029` | R1–R4 | trace disabled、trace writer 失败或 trace 文件缺失时，所有对应恢复测试结果不变 |
| `REL-030` | R1/R4 | `plan_execute/agent_loop/native_loop` 三个 Adapter 均通过公共 Run/terminal/event 契约；无 Tool 文本 smoke 不靠 EOF/done 成功 |

## 2. 12 个 R3 故障注入点

| ID | 精确注入点 | 对应断言 |
|---|---|---|
| `FI-01` | admission commit 后、claim 前 kill | Run durable；重启领取一次；`REL-001/013` |
| `FI-02` | LLM 调用前 kill | lease recovery；没有模型输出事实；`REL-012/013` |
| `FI-03` | LLM 返回后、任何对应 event commit 前 kill | 未提交内容不可见；model Activity 按恢复等级重试；`REL-008/030` |
| `FI-04` | Tool `PREPARED/TOOL_CALL` committed 后、dispatch 前 kill | 不产生副作用；稳定 slot 恢复；`REL-017/019` |
| `FI-05` | Tool 已 dispatch、执行中 kill | effect 进入 UNKNOWN/reconcile 或只读安全重试；`REL-018/019` |
| `FI-06` | Tool 外部成功，但 ACK 或 Runtime result commit 丢失 | 不盲目重发；reconcile/manual；`REL-017/018` |
| `FI-07` | Artifact rename+fsync 后、metadata commit 前 kill | 仅 orphan，可回收；`REL-021` |
| `FI-08` | Run/Activity 已 WAITING_INPUT 时重启 | 不占 Worker，signal 可继续；`REL-015` |
| `FI-09` | signal commit 后、resume Activity claim 前 kill | signal 只消费一次，恢复领取一次；`REL-014/016` |
| `FI-10` | final message/terminal 原子事务 commit 前 kill | 两者全部不可见，恢复后一次 finalize；`REL-004/007` |
| `FI-11` | terminal commit 后、SSE 第一次读取前 kill | terminal 可查询、可 replay；`REL-004/009/010` |
| `FI-12` | cancel 与 Tool complete 并发，在两个 commit 顺序各注入 barrier | 两种顺序均符合竞态规则；`REL-005/020` |

## 3. 阶段退出映射

| 阶段 | 必须绿的测试 |
|---|---|
| R0 | Schema 通过 Draft 2020-12 元模式且跨文件 `$ref` 可解析；状态/ADR/测试 ID 与实现覆盖节点 lint；每项 R1–R4 退出条件有上述 ID |
| R1 | `REL-001`–`REL-016`、`REL-028`–`REL-030` 的适用子集 |
| R2A | `REL-017`–`REL-023` 加全部既有 R1 测试 |
| R2B | `REL-024`–`REL-027` 加全部既有 R1/R2A 测试 |
| R3 | 冻结目标：30 个 REL 与 `FI-01`–`FI-12` 全部具备 DIRECT 自动化证据；当前差距以实现覆盖追踪为准 |
| R4 | `scripts/check.sh` 全绿；旧 API/ENGINE/History/Session SSOT/embedding truth 过时字符串扫描全绿；四服务五进程启动与三引擎真实 LLM smoke 另行记录 |

## 4. 统一门禁

`scripts/check.sh` 最终必须顺序执行：

```text
py_compile
pytest tests/reliability
SQLite schema/checksum verification
文档/API 过时字符串扫描
```

R4 额外执行四服务启动、三引擎各一个真实 LLM smoke、ARAG-down 降级和新协议行为 harness。行为得分只记录，不作为可靠性 PASS。
