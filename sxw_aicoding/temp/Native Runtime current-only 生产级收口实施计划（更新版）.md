# Native Runtime current-only 生产级收口实施计划（更新版）

## 目标与边界

目标架构保持为：

```text
single current schema + schema digest
→ immutable current release + exact Worker claim
→ NativeLoopAdapter.execute(request, RuntimeIO)
→ mandatory Tool Broker
→ strict Checkpoint / ToolResult / Evidence contracts
```

本轮同时修复提交屏障、控制故障吞噬、大 Artifact 恢复、静默 EOF、最终 Assistant 不等价、工具并发失控六类 fresh-Run 问题。

`plan_execute`、`agent_loop` 的内部实现和现有行为不变。工具提前派发机制保留，但默认关闭；本轮生产级正确性结论以 `off` 模式为准。

## 1. Current schema 与 exact release

- Runtime、ARAG 的 `schema_meta` 统一为 `id + schema_digest + created_at`；digest 为完整 current `schema.sql` 字节的 SHA-256。
- 空库原子建表；非空库只接受完全一致的 digest，否则 `CURRENT_SCHEMA_MISMATCH` 并提示删除重建。
- 直接替换 current schema，不提供 `ALTER`、migration、upgrader、双读或旧 checkpoint codec。
- 删除内部冗余：
  - `release_manifests/runs/run_events/checkpoints.schema_version`
  - `checkpoints.engine_state_ref`
  - checkpoint、WorkingState、Event 表中可由 Run 权威派生的重复 release fingerprint
  - `ReleaseManifest.schema_version/runtime_contract/engine_contract`
- 对外 RuntimeEnvelope、Canonical Event、Artifact、Evidence 的 `schema_version: Literal["1"]` 保留，作为当前传输契约。
- `ReleaseManifest` 只保留 `engine + components`，components 覆盖：
  - current schema digest
  - engine/runtime/shared source digest
  - 最终工具目录 digest
  - 模型/provider/checkpoint codec
  - 全部语义配置和资源上限
  - 真实安装依赖版本
- 依赖 metadata 缺失时 Worker 启动失败，不再填 `unknown` 或 requirements 中的假定版本。
- 三份 release 在一个 `BEGIN IMMEDIATE` 中注册和激活：
  - 写入或核对 immutable manifest
  - 检查不存在与目标 fingerprint 不同的非终态 Run
  - 原子切换三个 active pointer
- 同 fingerprint 多 Worker 启动允许；新 fingerprint 遇到活跃旧 Run 报 `ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION`。
- admission 和 activation 都在写事务中读取/写入 active pointer，串行化二者竞态。
- `claim_next` 改接收完整 `release_map`，SQL 同时匹配 `(engine, release_fingerprint)`；普通、恢复和 reconcile claim 使用相同条件。
- 删除 `INCOMPATIBLE_RELEASE` 的 RunStatus、状态迁移、SQLite CHECK、Coordinator 分支、API/UI、文档和测试。
- 错误 release Worker 不能领取；Run 保持 pending，最终由绝对 deadline 收口。
- Coordinator 保留不可达的 `CLAIM_RELEASE_MISMATCH` 防御断言，只中止 attempt 并报警，不产生 Run 终态。

## 2. 唯一严格 Tool Catalog、ToolResult 与 Evidence

- Worker 启动顺序固定为：

```text
加载工具源
→ 构造最终 ToolCatalog
→ 严格校验
→ 计算 release
→ 创建 Adapter
→ 原子激活 release
```

- 每个 ToolBinding 固定：
  - name、description
  - Draft 2020-12 object 参数 schema
  - effect、重试、并发、独占资源和结果策略
  - executor/result adapter
  - 独立 tool release digest
- 删除目录宽容行为：重复名称、空 schema fallback、声明异常、适配失败、缺失 manifest、非法 JSON Schema、目录部分跳过均阻止启动。
- 可选 Skill/A2A 下游连接不可用继续按既有 best-effort 返回空目录；但只要成功返回目录，任何畸形条目都必须 fail-fast。
- agent_loop/native_loop 的公开工具面必须逐项同名、同描述、同 normalized schema、同 effect policy；子 Runner 实现 digest 可以不同。
- Broker 成为 Native 生产路径强制依赖，新增：
  - `prepare_batch(...)`
  - `execute_prepared(...)`
  - `materialize_committed_result(...)`
- ADK 现有单调用入口保留为上述能力的包装，不改变两个 ADK 引擎。
- 引入唯一内部工具输出：

```text
ToolExecutionOutput
  ├─ result: ToolResultEnvelope
  └─ evidence: EvidenceSet | null
```

- builtin、Skill、Claude Skill、A2A 在各自协议入口完成适配：
  - 普通 JSON → `SUCCESS.preview`
  - `None` → `NO_OUTPUT`
  - Skill 当前的 `isError/errorCode` 只在 Skill adapter 解析
  - Broker 内部只接受严格 typed output，不识别别名或任意 dict
- EvidenceSet/EvidenceItem 使用 `strict + extra=forbid` DTO。
- knowledge_search 必须产生完整 current EvidenceSet；Broker只核对 run/activity/tool_execution 身份，不补造 query、hash、document/index version、scope。
- 删除 `__evidence_set__` 隐藏字段、legacy hits 转换和 `unknown/default/unversioned` provenance。
- 缺失或矛盾 Evidence 统一报 `EVIDENCE_CONTRACT_INVALID`。

## 3. NativeLoopAdapter 直接 RuntimeIO

- 将当前通用 Adapter 收窄并重命名为 `AdkEngineAdapter`，只接受 `plan_execute|agent_loop`。
- 从 ADK `build_engine`、RunContext engine 类型和 Adapter 分支中移除 native。
- 新建：

```text
NativeLoopAdapter.execute(EngineRunRequest, RuntimeIO) → EngineOutcome
```

- Native Adapter 直接负责：
  - canonical history/current input/附件编译
  - strict checkpoint 恢复
  - Native kernel 驱动
  - RuntimeIO event/checkpoint
  - Tool Broker 调度
  - 最终 Assistant 指定
- Native 不再经过：
  - ReasoningEngine
  - RunContext.engine_outcome
  - merge_runner_events
  - 后台无界 Queue
  - StreamEvent.authority 路由
- Native kernel 保持 Runtime 无关，继续可被 Claude Skill 子 Runner 使用；RuntimeIO/Broker 只存在于生产 Adapter 和窄回调。
- Skill UI 增加 native 专用 awaited sink：每个 frame 直接等待 RuntimeIO 提交，形成自然背压。
- 原有 queue/merge 路径完整保留给两个 ADK 引擎。
- 删除 Native 生产路径的“无 RuntimeIO”“无 Broker”fallback和追加式 compact 无效预留。

## 4. 默认关闭但保留工具提前派发

配置由含义模糊的布尔值改为：

```text
native_early_tool_dispatch =
  off
  experimental_heuristic
  provider_block_complete
```

默认值固定为 `off`。

### off：本轮生产默认

```text
模型流完整结束
→ 校验完整 ToolCall batch
→ checkpoint
→ Broker 原子 PREPARE
→ 派发工具
```

- 模型生成期间绝不创建工具任务。
- 保留模型正文流式输出。
- 保留工具运行期间的 Skill/Claude Skill 进度流式展示。
- 保留完整 batch 后 READ_ONLY 工具的受控并发。
- ToolResult 仍在完成后一次性权威提交。

### experimental_heuristic：保留现有机制

- 保留当前基于“更高 index + JSON 暂时可解析”的提前识别逻辑，不删除相关 accumulator、ToolCallReady 和实验测试。
- 明确标注为非生产语义，不计入默认模式的可靠性声明。
- 只允许已评审的 READ_ONLY、`concurrency_safe=true`、无独占资源工具提前执行。
- 每个调用仍须先通过 Broker 建立稳定 slot 和 ToolCall 事实，才能执行。
- 发现后续 fragment 改变已派发调用时，必须以 `TOOL_REPLAY_MISMATCH` fail-closed；已完成的只读调用允许被浪费，但不能被解释成另一调用。
- 即使在实验模式，也必须遵守：
  - 全局并发上限
  - 每轮 call/args 上限
  - cancel/deadline/lease-loss 清理
  - 零游离 task
- 使用固定数量 worker task 和有界队列，不再为每个 early call 直接 `create_task`。

### provider_block_complete：未来目标接口

- 只在 provider adapter 明确声明 `tool_call_block_complete` capability 时可启用。
- 当前 OpenAI-compatible provider 没有该能力；选择此值时启动失败 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE`。
- 后续完善时用 provider 的明确 block 完成信号替代 experimental heuristic，不再通过 JSON 可解析推断完整性。

三种模式均纳入 release fingerprint，避免不同 Worker 用不同派发语义恢复同一 Run。

## 5. 模型流、Checkpoint 与工具事务顺序

默认 `off` 模式下，每个 Native model turn 固定执行：

1. 检查 cancel、deadline、预算并完成必要 compact。
2. 短事务保存 `MODEL_REQUEST` checkpoint，并追加 `OUTPUT_GENERATION_STARTED`。
3. 发起 provider stream；每个 delta 必须先 `await RuntimeIO.emit`，才继续拉下一块。
4. 收到显式完整 finish marker 后，验证完整 ToolCall batch。
5. 保存 `MODEL_RESPONSE_COMMITTED` checkpoint。
6. 无工具且正文非空：保存 `COMPLETED` checkpoint，设置最终 Assistant，返回 COMPLETED。
7. 有工具：Broker 单事务 PREPARE 全部 stable slots 和 ToolCall events。
8. 保存 `TOOL_BATCH_COMMITTED` checkpoint，之后才允许 dispatch。
9. 每个 Broker 结果先结算 ToolExecution/Artifact/Event，再按模型 call ordinal放回消息并保存 `TOOL_RESULT_COMMITTED`。
10. 全部完成后保存 `NEXT_TURN`，进入下一模型轮。

严格 provider 规则：

- 只接受单 choice、显式 `stop` 或 `tool_calls`。
- tool index 必须连续，id/name 唯一且不能在后续 fragment 改变。
- usage-only、零 chunk、自然 EOF 无 finish、finish 后继续输出、重复 index、缺 id/name不能合成 TurnEnd。
- `stop` 必须无 ToolCall；`tool_calls` 必须有完整 calls。
- `length/content_filter/未知 finish reason`失败关闭。
- `MODEL_STREAM_INCOMPLETE` 为可重试失败。
- `MODEL_PROTOCOL_INVALID`、`MODEL_EMPTY_FINAL_RESPONSE` 为终端失败。
- JSON/schema 不合法属于模型可修正错误：整批零 dispatch，原子保存成对 call/result；问题调用返回 `TOOL_ARGUMENTS_INVALID/TOOL_NOT_FOUND`，其余返回 `TOOL_BATCH_REJECTED`。

Checkpoint 只接受一个 current typed codec，phase 固定为：

```text
MODEL_REQUEST
MODEL_RESPONSE_COMMITTED
TOOL_BATCH_COMMITTED
TOOL_RESULT_COMMITTED
NEXT_TURN
COMPLETED
```

- `checkpoint is None` 才允许从 canonical history 初始化。
- checkpoint 存在但缺字段、多字段、未知 phase、非法 role、重复 call id、call/result 未配对或 phase 与消息状态矛盾时，直接 `NATIVE_CHECKPOINT_INVALID`。
- 不 fallback、不猜默认值、不读取旧 marker。
- 大 ToolResult 使用 `LedgerToolResultRef(tool_execution_id)`；恢复时扫描所有历史位置并通过 Broker/Artifact 重物化。
- `COMPLETED` checkpoint 保存精确 final text 和 generation identity；若崩溃发生在 checkpoint 与成功终态之间，恢复后不再请求模型或重放 delta。

## 6. 代际输出、最终消息和结构化并发

新增 Canonical Event `OUTPUT_GENERATION_STARTED`，SSE 名称 `text_start`：

```json
{
  "message_id": "稳定 model slot",
  "generation_id": "本次 attempt generation",
  "supersedes_generation_id": null,
  "reason": "initial|next_turn|retry|recovery|reactive_compact"
}
```

- Native OUTPUT_DELTA 必带 message/generation id。
- 重试或恢复创建新 generation；旧事件保留审计。
- UI 收到 `text_start` 时只清空回答正文，不清工具、Skill、计划等过程卡片。
- fresh replay 与断线续传按相同事件规则重建，最终 assistant_message 权威覆盖。
- RuntimeIO 增加：
  - checkpoint 与一组 engine-owned events 同事务提交
  - `set_final_assistant(text, message_id, generation_id)`
- Coordinator 对 Native 使用显式 final override；ADK 未设置时继续使用当前累计文本。
- 只有最后一个完整、非空、无 ToolCall 的 assistant turn 进入 Conversation history。
- READ_ONLY 且无独占资源的工具才并发；副作用与 UNKNOWN 工具串行。
- 并发只发生在外部执行阶段；Broker 结算、模型结果和 checkpoint 按 call ordinal 提交。
- cancel、deadline、GeneratorExit、Adapter 异常、lease loss 都必须关闭 provider stream，并取消、await 工具任务、HTTP 调用和 Skill 子进程。
- Worker 续租失败立即取消对应 attempt。
- 引入 `AttemptOwnershipLost`：stale fence、lease loss、checkpoint CAS 冲突直接冒泡给 Worker，不能变成模型 ToolResult或 Run terminal。
- `TOOL_REPLAY_MISMATCH`、ToolResult/Evidence contract 错误保留原错误码并终端失败。

## 7. 配置与资源上限

| 配置 | 默认值 |
|---|---:|
| `native_early_tool_dispatch` | `off` |
| `native_max_tool_concurrency` | 10 |
| `native_max_tool_calls_per_turn` | 64 |
| `native_max_tool_calls_per_run` | 256 |
| `native_max_tool_argument_bytes` | 64 KiB |
| `native_max_tool_batch_argument_bytes` | 256 KiB |
| `native_max_model_output_bytes` | 1 MiB/generation |
| `native_max_checkpoint_bytes` | 2 MiB |
| `native_max_tool_catalog_bytes` | 1 MiB |
| `native_max_skill_event_bytes` | 64 KiB |
| `native_max_skill_events_per_run` | 2000 |
| `native_max_skill_event_bytes_per_run` | 8 MiB |

- 所有尺寸按 UTF-8 bytes 计算。
- model call 硬上限继续为 `max_loop_iters + 2`，默认 10；调用开始前写入 checkpoint，崩溃不退还。
- 启动时校验：
  - `max_loop_iters > 0`
  - `compact_buffer_tokens < context_window_tokens`
  - 并发数不超过每轮 call 上限
  - batch args 上限不小于单 call 上限
- 稳定限制错误码：
  - `MODEL_OUTPUT_LIMIT_EXCEEDED`
  - `TOOL_CALL_LIMIT_EXCEEDED`
  - `TOOL_ARGUMENTS_TOO_LARGE`
  - `TOOL_BATCH_TOO_LARGE`
  - `CHECKPOINT_TOO_LARGE`
  - `SKILL_UI_LIMIT_EXCEEDED`

## 8. Demo、文档与门禁

- `/reliability-demo` Adapter、Worker 路由、effects.db、配置和手工入口从生产源码删除。
- 等价实现移入 reliability test support。
- 普通输入包含 `/reliability-demo` 时不触发特殊逻辑。
- 删除 `SchemaCompatibilityError` 包装，统一使用 `SchemaIdentityError`。
- 更新 current schema、release identity、Native recovery ADR、状态所有权表、README、RUNBOOK、AGENTS.md、CLAUDE.md、eval 文档和 `.env.example`。
- `scripts/check.sh` 增加静态扫描：
  - migration/ALTER/upgrader
  - `engine_state_ref`
  - `INCOMPATIBLE_RELEASE`
  - legacy Evidence helper
  - 生产 demo 路由
  - 旧的 `native_streaming_tool_exec` 名称

## 9. 测试与验收

- Schema：并发空库 bootstrap、digest 篡改、陌生库、Runtime/ARAG 双库。
- Release：activation/admission 竞态、三 pointer 原子切换、活跃 Run 阻止新 release、不同 fingerprint Worker 不可 claim。
- Adapter 隔离：plan_execute/agent_loop 继续走 ADK Adapter，现有事件、附件、工具和终态测试保持不变。
- 提交屏障：阻塞 RuntimeIO 时，Native 不能继续拉流、PREPARE、dispatch 或完成。
- Provider：正文、工具流、零 chunk、usage-only、silent EOF、缺 finish、finish 矛盾、多 choice、重复 index、缺 id/name、fragment 追加。
- Generation：首 delta 后崩溃、恢复、reactive compact、SSE 重连和 fresh replay。
- Checkpoint fault matrix：每个 phase、每个第 k 个 ToolResult、COMPLETED 后终态前；非法 codec 全部失败关闭。
- Tool batch：任一 slot replay 漂移全批回滚且零新增 dispatch；invalid batch 全部成对。
- Artifact：多个大结果跨多个 turn kill/restart，任意位置均可重物化，无 orphan call 和重复 ToolResult。
- 默认 early dispatch：
  - `off` 时模型 finish 前工具执行计数始终为零。
  - 完整 batch 后 READ_ONLY 仍可并发。
  - 工具运行期间 skill_event 仍实时提交。
- 实验 early dispatch：
  - 机制仍可显式启用。
  - 只允许安全 READ_ONLY。
  - 峰值不超过 N。
  - pending worker/task 有界。
  - cancel/EOF/lease loss 后零悬挂任务。
  - 后续 fragment 漂移必须 `TOOL_REPLAY_MISMATCH`。
  - 测试和文档明确该模式尚不属于生产保证。
- 最终语义：`中间文本→工具→最终答案` 的 crash/no-crash 只提交相同的最后 Assistant。
- ToolResult/Evidence/catalog：覆盖 builtin、Skill、Claude Skill、A2A；所有目录和契约错误按预期 fail-fast。
- Demo：生产 Worker 无特殊路由，测试 fixture 继续验证 WAITING_INPUT 与幂等副作用。
- 最后执行完整 `scripts/check.sh`，三个引擎 reliability suite 全部通过。

## 假设

- `local_storage` 已清空，本次不提供任何数据或协议过渡机制。
- SQLite 继续承担本机多进程 authority；本轮不引入 PostgreSQL、Temporal 或 Redis，也不宣称跨主机 HA。
- `plan_execute`、`agent_loop` 内部循环不重构，只调整 Adapter 命名和共享 Runtime 端口适配。
- 工具提前派发代码和实验能力保留；默认生产路径固定为 `off`，待后续取得 provider 明确 ToolCall block 完成信号后再升级其可靠性等级。
