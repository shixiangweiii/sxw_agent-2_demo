# 代码评审：Native Runtime current-only 生产级收口

- 评审日期：2026-08-12
- 评审对象：`aa5208c`（129 个文件 `+12774/-9636`）+ `b2821ab`（实施计划备份）
- 对照实施方案：`sxw_aicoding/temp/Native Runtime current-only 生产级收口实施计划（更新版）.md`
- 对照约束：`sxw_aicoding/项目背景说明.txt`、`AGENTS.md`、`CLAUDE.md`、`docs/reliability/`
- 本次评审只读，未修改任何文件

---

## 〇、结论

**主体实现质量高于"按计划打勾"的水平，但当前提交状态不可直接交付：门禁是红的，且有一处违反冻结 ADR 的写放大。**

实施计划 9 节的核心项都真实落地，而且不是表面改名——`schema_meta` 换 digest、三 release 单事务激活、`claim_next` 精确 `(engine, fingerprint)` 匹配、strict checkpoint codec、Broker `prepare_batch/execute_prepared/materialize_committed_result`、`ToolExecutionOutput` 统一出口、Evidence 去掉补造、`native_early_tool_dispatch` 三态、`AttemptOwnershipLost`、`OUTPUT_GENERATION_STARTED`/`text_start` 全链路，逐条都能在代码里对上。

需要先处理的是两件事：

1. **`scripts/check.sh` 实跑 `exit=1`**（依赖锁与实际环境不一致），且这个不一致会污染 release fingerprint 语义。
2. **Native 每个模型 token 强制落盘一次**，直接违反冻结的 ADR-0002 §2 与 `CLAUDE.md:60` / `AGENTS.md:76` 的 100ms/2KiB 聚合不变量。

其余问题集中在**副作用记账的失败路径**和**重复/死代码**，核心事务逻辑没有发现破口。

---

## 一、实测结果

以下不是读代码得出的推断，是本次评审实际跑出来的结果。

| 验证项 | 方法 | 结果 |
|---|---|---|
| 全量门禁 | `bash scripts/check.sh` | **FAIL（exit=1）** |
| 可靠性测试 | 门禁内 `pytest -q tests/reliability` | **362 passed / 1 failed** |
| 失败定位 | 读门禁输出 | `test_release_fingerprint_coverage.py::test_runtime_dependency_lock_matches_release_registry_and_installed_metadata` |
| 依赖锁一致性 | `importlib.metadata.version` 逐个比对 `requirements.txt` | **3 处不一致**（见下表） |
| 门禁后续阶段是否执行 | 观察输出，`[check] SQLite checksums...` 未出现 | **未执行**（`set -e` 在 pytest 处中断） |
| 被删依赖是否仍被引用 | 搜 `sse_starlette` / `sse-starlette` / `tiktoken` | 零代码命中（仅 `arag/components/chunker.py:3` 一处注释提及 tiktoken，属说明性文字） |
| `NativeLoopAdapter` 装配路径 | 搜全仓引用 | 仅 `worker/main.py:91` 一处生产装配，API 进程不引入 |
| 旧协议扫描字面量 | 读 `scripts/check.sh` 末段 `patterns` | 新增 6 条（migration/ALTER/upgrader、`engine_state_ref`、`INCOMPATIBLE_RELEASE`、legacy evidence、生产 demo 路由、`native_streaming_tool_exec`），与计划 §8 一致 |
| Evidence 契约与 ARAG 实际响应是否对齐 | 逐字段比对 `EvidenceItem`（`models.py:293`）与 `RetrievedChunk`（`arag/schemas.py:77`） | **完全对齐**（`EvidenceItem` 仅多一个 `n`），`strict + extra=forbid` 不会误伤 |
| `text_start` 全链路 | 搜 `OUTPUT_GENERATION_STARTED` | `loop.py` 产出 → `engine.py:518` 映射 → `runs.py:235` SSE 名 → `web/app.js:300` 消费 → `canonical-event-v1.schema.json:50/135` 冻结契约，五处齐全 |

### 依赖锁不一致明细

| 包 | `requirements.txt` 锁定 | `.venv` 实际安装 |
|---|---|---|
| `starlette` | `1.3.1` | `1.4.0` ← 测试报的就是这条 |
| `pydantic` | `2.12.5` | `2.13.4` |
| `jsonschema` | `4.23.0` | `4.26.0` |

---

## 二、必须处理

### 2.1 Native 每个 delta 强制落盘，聚合失效

**位置**：`agent/engine/native_loop/engine.py:387-389`

```python
await io.emit(event.event, event.data)
if event.event == "text":
    await io.force_flush()
```

上方注释解释的是 `await io.emit` 作为提交屏障——这部分没问题，屏障靠 `await` 本身就成立。问题是多出来的 `force_flush()`：`emit → emit_text` 本来只写内存 buffer，攒够 2KiB 或 100ms 才 flush；紧跟一次 `force_flush` 等于把**每个 provider token 变成一次独立的 `append_events` 写事务**。

**违反的冻结契约**：

- `docs/reliability/adr/0002-streaming-commit.md` §2（状态 Accepted / Frozen）列举的强制 flush 边界是「message 切换 / 完整 ToolCall batch / 任一 ToolResult / checkpoint / Engine stop / terminal」，**没有"每个 delta"**；
- 同 ADR 的 Consequences 明写「SQLite 写放大受聚合控制，代价是最多约 100ms/2KiB 的持久化粒度」；
- `CLAUDE.md:60` 与 `AGENTS.md:76` 都把 100ms/2KiB 写成不变量。

**实际代价**：`synchronous=FULL` + WAL 下每 token 一次 fsync 事务。一段 2000 token 的回答 = 2000 次 fsync 写事务，全部争抢 SQLite 单写者锁；`runtime_worker_concurrency=4` 时多个 native Run 会在 token 级互相阻塞，`SQLITE_BUSY` 风险显著上升。这与背景说明第 11 行「生产级……背压、资源上限」的要求方向相反。

**而且它是不必要的**：

- `CommittedEventSink.checkpoint()`（`events.py:400`）在 `save_checkpoint` 之前已经 `await self.force_flush()`，所以 `MODEL_RESPONSE_COMMITTED` 之前该代的 delta **必然已提交**；
- 崩溃恢复的语义本来就是新建 generation + `text_start` 让 UI 清正文重放（计划 §6），根本不依赖单 delta 持久化；
- 对照 ADK 侧 `adk_engines.py:161-165`，只在 ToolCall batch 边界 `force_flush`，写法是对的。两条路径不一致本身就是信号。

**没有测试要求这个行为**：`test_native_runtime_io_integration.py:728` 断言的是 fake IO 的 `emit` 调用序列，不是提交次数。删掉 `force_flush` 不会导致测试失败。

**建议**：删除该行；若确实需要按代边界 flush，改为仅在 `message_id`/`generation_id` 切换时触发（`emit_text` 内部已有该判断，见 `events.py:256-260`）。

---

### 2.2 依赖锁与运行环境不一致，门禁红

**位置**：`requirements.txt`（本次提交从松散约束改为精确锁）vs `.venv`

这不是纯环境问题。`agent/runtime/adapters/releases.py:308` 的 `_installed_version()` 把**实际安装版本**直接写进 release fingerprint：

```python
components.update({
    f"installed_dependency_{component}": _installed_version(distribution)
    for component, distribution in _RUNTIME_DISTRIBUTIONS
})
```

因此"锁文件"和"实际环境"必须严格同步才有意义——否则 fingerprint 描述的是**机器**而不是**仓库**，两台装了不同小版本的机器会算出不同 fingerprint，互相无法 `claim` 对方的 Run（`claim_next` 精确匹配 `(engine, release_fingerprint)`）。在计划宣称的"多实例"语境下，这是必须收敛的。

`test_runtime_dependency_lock_matches_release_registry_and_installed_metadata` 正是为守住这条而写的，它现在如实报了红。

**建议**：二选一，不能两边都不动。

- 重装 venv 对齐锁文件：`.venv/bin/pip install -r requirements.txt`；
- 或按实际安装版本重写锁文件的这三行。

---

## 三、建议处理

### 3.1 `except Exception` 会吞掉 `AttemptOwnershipLost`

**位置**：`agent/runtime/application/tool_broker.py:919`

```python
except Exception as exc:  # after dispatch, side-effect failures are conservative
    await settlement_boundary()
    result, execution = await self._settle_dispatch_failure(...)
```

`AttemptOwnershipLost` 是 `RuntimeError` 子类（`domain/errors.py:19`），**不是** `RuntimeFault`，所以会被这里捕获，变成一条模型可见的 ToolResult。这正是计划 §6 引入该异常要禁止的事：

> 引入 `AttemptOwnershipLost`：stale fence、lease loss、checkpoint CAS 冲突直接冒泡给 Worker，不能变成模型 ToolResult 或 Run terminal。

同文件 `tool_broker.py:1194` 的 `_resolve_uncertain` 就显式写了 `except (RuntimeFault, AttemptOwnershipLost): raise`，说明作者意识到了这条边界，只是 `_execute_ledger` 漏了。

**当前可达性**：追了一遍调用栈，目前从 tool executor 内部抛出 `AttemptOwnershipLost` 的路径**应该不存在**——Skill UI sink 抛的是 `RuntimeFault`，由 `except RuntimeFault`（`tool_broker.py:874`）正确透传，之后才在 `engine.py:236` 由 `raise_if_ownership_lost` 转换。所以这是**防线缺口而非现行 bug**。但成本只是把它加进 `except (RuntimeFault, AttemptOwnershipLost)`，值得补。

---

### 3.2 控制类 RuntimeFault 不结算 ToolExecution，未决副作用在失败终态被丢弃

**位置**：`agent/runtime/application/tool_broker.py:874-890` + `agent/runtime/application/coordinator.py:368-371`

`_execute_ledger` 的 `except RuntimeFault` 分支中，只有 `TOOL_RESULT_CONTRACT_INVALID` / `EVIDENCE_CONTRACT_INVALID` 会走 `_settle_dispatch_failure`，其余 `RuntimeFault` 直接 `raise`，ToolExecution 停在 `DISPATCHED`。

本次新增的 `SKILL_UI_LIMIT_EXCEEDED`（`events.py:200`）就是这样一条路径：

```text
Skill 执行中推 UI 帧超配额
→ CommittedEventSink.emit 抛 RuntimeFault
→ executor.execute_one 的 `except RuntimeFault: raise` 透传（executor.py:171）
→ _execute_ledger 不结算，直接 raise
→ Coordinator 判 TERMINAL_FAILURE
```

而未决副作用门禁 `unresolved_tool_execution_ids` **只在 COMPLETED 分支执行**（`coordinator.py:368`）：

```python
if outcome.kind is EngineOutcomeKind.COMPLETED:
    unresolved = await self.store.unresolved_tool_execution_ids(...)
    if unresolved:
        final = await self.store.wait_for_input(...)
```

所以这个 `DISPATCHED` 行永远不会进 `WAITING_INPUT` / manual 边界，直接随失败终态被丢弃。

对 READ_ONLY 工具无所谓；对 UNKNOWN_EFFECT 的 Skill（按 `brokered_tools.py:70` 未评审 Skill 默认就是 UNKNOWN）这是真正的副作用记账漏洞——与 `CLAUDE.md` 的「dispatch 后 timeout/ACK 不明进入 UNKNOWN → reconcile/manual」冲突。

**这条需要先定语义再改**，两个方向：

- 在该路径也做 UNKNOWN 结算（让它进 manual 边界）；
- 或把未决效果检查从 COMPLETED 分支提到所有终态提交之前。

---

### 3.3 `_model_result` 两份重复副本

**位置**：`agent/engine/native_loop/engine.py:555` 与 `agent/runtime/adapters/brokered_tools.py:759`

两份约 18 行、逻辑相同的「`ToolResultEnvelope` → 模型可见投影」，且有一处细微分叉：

| | engine.py:569 | brokered_tools.py:777 |
|---|---|---|
| 错误码取值 | `result.error_code or result.status.value` | `result.error_code or result.status` |

因为 `ToolResultStatus` 是 `StrEnum`，两者 JSON 序列化结果目前相同，**不构成现行 bug**。但这是喂给模型的语义契约，两份就是两份，一旦一侧改动就是两个引擎行为不一致。背景说明第 16 行明确要求"消除实质性重复"，这条正好撞上。

**建议**：合并到一处（`tool_outputs.py` 或 `brokered_tools.py`），另一处引用。

---

### 3.4 Evidence 身份靠 `idempotency_key` 借道传递

**位置**：`agent/tools/knowledge_search.py:244` ↔ `agent/runtime/application/tool_broker.py:1074-1090`

```python
# knowledge_search.py:244
tool_execution_id=request["idempotency_key"],
```

Broker 随后断言它等于 `execution["tool_execution_id"]`。两者能对上，**只是因为** `store.py:3630` 恰好写了：

```python
"idempotency_key": tool_execution_id,
```

根因是 `_with_tool_request_context`（`brokered_tools.py:806-820`）只透传了 `activity_id / deadline_at_ms / idempotency_key`，没有 `tool_execution_id`，knowledge_search 拿不到别的来源。

哪天存储层想给下游一个不同的幂等键（这是很自然的演进方向），knowledge_search 会**全量** `EVIDENCE_CONTRACT_INVALID`，而且报错完全指不到真正原因。

**建议**：`SkillRequestContext` 增加 `tool_execution_id` 字段并在 `_with_tool_request_context` 透传，让 Evidence 直接引用它。

---

## 四、低优先级 / nit

| 位置 | 问题 |
|---|---|
| `native_loop/loop.py:994` | `_call_events` 生产路径已死：native 全部 `project_event=False`、`TOOL_CALL_COMMITTED` 归 Broker，唯一调用方是 `test_native_tool_event_authority.py`（三处 `# noqa: SLF001`）。纯为测试存在的生产代码 |
| `native_loop/engine.py:354` | `loop.run(state.messages, initial_state=state)` 的第一个位置参数在传了 `initial_state` 时被完全忽略（`loop.py:284`），读起来像有两个状态来源 |
| `native_loop/engine.py:114-117` | 用 `try/except Exception` 包一个 property 读取来转成 RuntimeError，捕获面过宽 |
| `native_loop/loop.py:646` | `_complete(state, finish_reason)` 收下 `finish_reason` 但不使用 |
| `runtime/worker/main.py:153` | `_assert_loop_tool_parity` 与 `_run` 之间缺空行（`check.sh` 只跑 py_compile，扫不到） |
| `native_loop/engine.py:357-375` | `next_event()` 为**每个事件**创建一个 `asyncio.Task` 包 `anext(stream)`，仅为让取消可轮询；叠加 `engine.py:390` 每事件一次 `io.is_cancelled()` DB 读，在 2.1 修复后仍是每 token 一次 task 分配 + 一次 SELECT。改成单 pull task + 独立 cancel watcher 会干净很多（`is_cancelled` 每事件一读是 ADK 侧既有模式，非本次回归） |

---

## 五、做得扎实的地方

不是客套。以下几处是评审中让我停下来确认"确实想到了"的：

**release 激活的原子性**（`store.py::activate_current_releases`）
单个 `BEGIN IMMEDIATE` 内完成三件事：manifest 写入/核对 → 非终态 Run 阻塞检查（`terminal_status IS NULL AND release_fingerprint<>?`）→ 三 pointer 切换。配合 schema 里新增的 `release_manifests_no_update` / `no_delete` 触发器做数据库级不可变约束。`claim_next` 的 `(r.engine=? AND r.release_fingerprint=?)` OR 谓词也与计划 §1 完全一致，普通/恢复/reconcile 三种 claim 共用同一条件。

**checkpoint codec 是真严**（`native_loop/checkpoint.py`）
`_validate_message_sequence`（:198）真的在校验 call/result 严格配对、名称与顺序匹配、phase 与消息尾部一致、logical_key 全局唯一、`model_call_count == generation_counter`。其中 `MODEL_REQUEST` 不允许存在 assistant 尾部（:251-256）尤其关键——它结构性地挡住了"半提交响应恢复后变成第二次模型请求"这类最难查的重复副作用。没有 fallback、没有默认值猜测，与背景说明第 5 行一致。

**`ToolSettlementOrder` 把并发边界划对了**（`tool_broker.py:121-204`）
外部执行与 durable `DISPATCHED` 转换保持并发，只有 ToolResult 结算按 call ordinal 串行；控制失败时 `_abort_all` 同步唤醒所有等待者，不会留下孤儿 waiter。这是把计划 §6「并发只发生在外部执行阶段；Broker 结算按 call ordinal 提交」真正落到实处的做法。

**`executor.run_calls` 的结构化并发**（`executor.py:262-302`）
first-failure future + 按调用序回收 + `except BaseException` 兜底取消全部兄弟任务 + `finally` 收尾。`signal_control_failure` 里对 `task.cancelled()` 先判再取 `exception()` 也是对的。零游离 task。

**`_user_object_count` 的改法**（`common/sqlite_schema.py`）
从只数 `type='table'` 改成数所有非 `sqlite_%` 的 `sqlite_master` 对象。只含 view/trigger 的库不再被误当空库接管——细但真实的坑。

**Evidence 彻底去补造**（`knowledge_search.py` + `tool_broker.py::_validate_evidence_identity`）
删掉 `__evidence_set__` 隐藏字段与 legacy hits 转换；`_retrieval_request` 在缺 Runtime 身份时直接 `EVIDENCE_CONTRACT_INVALID` 而不是回落到匿名默认值；`_evidence_from_retrieval` 用 `strict=True` 逐字段校验 ARAG 原样返回，缺一不补。Broker 侧只核对三段身份，不补 provenance。与计划 §2 完全一致。

**设计上值得写进面试材料的一点**：把「工具目录构造 → 严格校验 → 计算 release → 创建 Adapter → 原子激活」定成 Worker 启动的固定顺序（`worker/main.py:60-101`），并让 `_assert_loop_tool_parity` 在 release 计算**之前**卡住两个 loop 引擎的公开工具面差异。这意味着"两代引擎工具面漂移"这类问题在启动期就被消灭，而不是等到某个 Run 恢复时才暴露成不可解释的行为差异。

---

## 六、建议处理顺序

1. **删 `engine.py:389` 的 `force_flush`**（或改为仅在 generation 切换时 flush），恢复 ADR-0002 语义 —— 独立、低风险、可立即验证。
2. **对齐 `requirements.txt` 与 `.venv`**，让 `check.sh` 回绿，并把"锁文件即 fingerprint 输入"这条写进 RUNBOOK 的运维注意事项。
3. **`tool_broker.py:919` 补 `AttemptOwnershipLost`** —— 一行改动，补一条 fail-closed 防线。
4. **定 3.2 的语义**（未决副作用在失败终态的归属），再动代码。这条涉及状态机语义，需要先决策。
5. 合并 `_model_result`；`SkillRequestContext` 补 `tool_execution_id`；清理 `_call_events` 等死代码。

前三项互不依赖，可并行。
