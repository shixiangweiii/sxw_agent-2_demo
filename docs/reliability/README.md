# R0 可靠性规格 v1

状态：**FROZEN**  
冻结日期：2026-08-09  
适用阶段：R1–R4  
实现目标：单机、本地磁盘、可恢复、可验证的可靠 Agent Runtime

本目录是 R1–R4 的规范性输入。代码、测试或其他文档与本目录冲突时，在显式修订本规格前，以本目录为准。

## 已锁定范围

- Runtime 使用 `aiosqlite + 显式 SQL`；领域层只依赖 Store/UoW 端口。
- `engine` 是每个 Run 的必填不可变字段；Worker 可执行三种引擎。
- API 进程与 Runtime Worker 分离，SQLite 只位于本机磁盘。
- 同一 `conversation_id` 同时最多一个非终态 Run。
- 附件先进入内容寻址 Artifact Store，再通过 `attachment_refs` 创建 Run。
- Runtime、Document、Artifact、Trace 各自有明确且不重叠的事实所有权。
- 旧接口、旧 Session、旧 native History 和旧 embedding 数据不迁移；R4 直接切换所有权。
- Runtime/ARAG 各只接受一份 current `schema.sql` 的完整 SHA-256 digest；无 migration、upgrader 或旧 checkpoint codec。
- 三份 immutable release 原子激活，Worker 只能精确 claim `(engine, release_fingerprint)`；活跃旧 Run 会阻止新 fingerprint 激活。
- `NativeLoopAdapter` 直接 `RuntimeIO` 和强制 Tool Broker；工具提前派发默认关闭，但模型正文与工具/Skill 进度仍流式提交。

## Delivery v1 的准确承诺

首版只承诺：Canonical Event 在发布前已提交，因此处于 **committed / AVAILABLE** 状态；客户端用本地保存的单调 `seq` cursor，通过 `after_seq` 或 `Last-Event-ID` 重放。

首版**不承诺** `DELIVERED` 或 `ACKED`，不接受服务端 Delivery ACK，不创建 `delivery_cursors` 表。SSE 断开只代表订阅结束，既不取消 Run，也不改变 Run 或 Event 状态。可见性过滤可造成客户端看到的 `seq` 不连续，cursor 是不透明单调位置，不是连续计数器。

## 规范索引

| 规范 | 作用 |
|---|---|
| [State Ownership Registry](state-ownership-registry.md) | 冻结每类事实的唯一裁判、合法投影和禁止写路径 |
| [状态机与错误码](state-machines.md) | Run、Activity、ToolEffect 邻接表、guard 和非法迁移结果 |
| [Failure Matrix](failure-matrix.md) | 冻结崩溃、竞态、超时、取消、信号和外部副作用的唯一结论 |
| [可靠性测试目录](reliability-test-catalog.md) | 30 个门禁测试和 12 个故障注入点 |
| [实现覆盖追踪](implementation-coverage.md) | 将每个 REL/FI 映射到真实 pytest 节点，并诚实标记 DIRECT/PARTIAL/GAP |
| [ADR-0001：事务边界](adr/0001-transaction-boundaries.md) | admission、seq、checkpoint、tool、artifact、finalize 的提交边界 |
| [ADR-0002：流式提交](adr/0002-streaming-commit.md) | committed delta、聚合、flush 和 SSE 发布顺序 |
| [ADR-0003：SQLite 权威](adr/0003-sqlite-authority.md) | SQLite 事实范围、PRAGMA、并发和 fail-fast 原则 |
| [ADR-0004：Native direct 恢复](adr/0004-engine-recovery.md) | Engine Adapter 契约、strict checkpoint、generation 和三引擎恢复粒度 |
| [ADR-0005：Current Schema / Exact Release](adr/0005-release-compatibility.md) | schema digest、不可变 release、原子激活与 exact claim |
| [ADR-0006：权威序列化边界](adr/0006-authority-serialization-contracts.md) | 真实 model/Broker 形状、compact UUID 与 epoch/RFC3339 边界 |
| [ADR-0007：跨进程 trace 关联](adr/0007-cross-process-trace-correlation.md) | `runs.trace_id` 承载入口轨迹键，Worker 侧恢复绑定，冻结 envelope 不变 |
| [`schemas/`](schemas/) | RuntimeEnvelope、Canonical Event、WorkingState、ToolResult、Artifact、Evidence JSON Schema v1 |

## JSON Schema

所有 Schema 使用 JSON Schema Draft 2020-12。带 `schema_version` 字段的根对象固定为字符串 `"1"`；`WorkingState` 与 `ToolResultEnvelope` 的版本由契约名和所属 checkpoint/release 固定，不虚构模型中不存在的版本字段。领域 ID 由 Runtime 生成：业务对象使用带类型前缀的 UUIDv4；可恢复 Activity/Tool 子身份使用 `run_id + logical_key` 派生的 UUIDv5。UUID 使用 `uuid.UUID.hex` 的 32 位小写 compact 表示（无连字符），与标准 UUIDv4/v5 身份等价。

Schema 校验的是各对象的真实权威序列化形状，而不是把 Store 对象和公开投影混成一种时间格式：

- `RuntimeEnvelope`、`CanonicalEvent` 和 checkpoint 内的 `WorkingState` 是 Runtime Store/领域对象；其中绝对时间使用 UTC epoch milliseconds。
- `ArtifactRef` 是上传 API 的公开 JSON，`EvidenceSet` 是可审计 JSON Artifact；二者时间使用 RFC3339 UTC（`Z`）。
- SSE 是 committed `CanonicalEvent` 的只读删减投影：`occurred_at` 在 API 边界转换成 RFC3339，且不会把 `producer/visibility/sensitivity` 等内部字段伪装成公开协议字段。SSE payload 不应拿去反向验证或写回 Canonical Event authority。

| 文件 | 根对象 |
|---|---|
| [runtime-envelope-v1.schema.json](schemas/runtime-envelope-v1.schema.json) | 冻结的入口身份与执行上下文 |
| [canonical-event-v1.schema.json](schemas/canonical-event-v1.schema.json) | append-only committed Runtime Event |
| [working-state-v1.schema.json](schemas/working-state-v1.schema.json) | 可恢复认知状态，不复制 Activity 进度 |
| [tool-result-envelope-v1.schema.json](schemas/tool-result-envelope-v1.schema.json) | Tool 五类结果的统一语义 |
| [artifact-v1.schema.json](schemas/artifact-v1.schema.json) | 内容寻址 ArtifactRef；来源 Link 仍由 SQLite 表裁决 |
| [evidence-v1.schema.json](schemas/evidence-v1.schema.json) | RetrievalStatus 与可追溯 EvidenceSet |

## 变更规则

R0 冻结后，任何状态、字段、事务边界或失败语义的变更都必须：

1. 新增或更新 ADR，并写明替代关系；
2. 修改对应 JSON Schema 或状态邻接表；
3. 增加/更新自动化可靠性测试；
4. 不通过隐式兼容分支同时保留两个权威实现。

## 非目标

本规格不建设历史数据迁移、旧 API 兼容、Memory、完整 Context Compiler、服务端 Delivery ACK、分布式多 Worker/多机 HA、主机或磁盘容灾、Temporal/PostgreSQL backend、ADK mid-turn deterministic replay、Claude SKILL Artifact 跨 Skill 传递或子 Runner HITL。
