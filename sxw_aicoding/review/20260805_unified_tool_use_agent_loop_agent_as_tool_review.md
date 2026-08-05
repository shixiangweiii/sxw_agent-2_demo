# 代码评审：统一 Tool-Use Agent Loop + Agent-as-Tool 改造

- 评审时间：2026-08-05
- 评审对象：提交 `bf28531`（skill优化，26 文件，+1519 / -146）；顺带复核 `4574be4`（上一轮评审问题修复）
- 对照文档：`sxw_aicoding/changelog/20260805_unified_tool_use_agent_loop_agent_as_tool.md`、`项目背景说明.txt`
- 评审方式：逐文件审读 + 对照本机 `google-adk==2.3.0` 源码核对假设 + 用项目虚拟环境做了 **70+ 项功能级验证**（目录校验、Coordinator 并发语义、guard 状态机、MessageBudget 原子裁剪、沙箱 staging/路径安全/进程组清理、fake ADK model 驱动的 `run_skill`/`ClaudeSkillTool` 全链路 6 个场景、UI 队列集成）

## 1. 总体结论

**通过（建议合并）**。这是质量很高的一次改造：架构共识（主 Agent 负责编排、Skill Runtime 只治理执行、不引入第二套编排器）在代码中得到忠实落地；取消/清理、并发门控、instruction-first 强制、结果有界化等难点都有可运行且经得起实测的实现。changelog 声称与实际代码逐条核对一致，四份主文档同步无夸大。未发现阻断性缺陷；发现 1 个中等问题（死代码）和 5 个低优先级观察项，见第 4 节。

另：上一轮评审的两个中等项（M1 中间错误帧语义、M2 连接错误无稳定码）已在 `4574be4` 中正确修复——failure-sticky 保留首个根因、`SKILL_HTTP_ERROR/SKILL_TRANSPORT_ERROR` 等稳定错误码贯通 function response 与结构化日志，已复核确认。

## 2. 分模块验证记录

### 2.1 调用契约与身份（contracts.py / call_identity.py / claude_skill_tool.py）

- envelope 结构实测与 changelog §3.1 完全一致：`skillCallId/skillId/status/summary/isError/error/meta{attempts,truncated}`，成功/失败同一 envelope。
- `resolve_skill_call_identity` 实测：无 ToolContext 时生成 `call_`/`invocation_` 前缀 UUID 并打 WARNING；ADK 路径复用框架 ID。
- 五个稳定错误码全部实测命中：`SKILL_TIMEOUT`（1s 超时 + 5s 延迟模型）、`SKILL_SANDBOX_UNAVAILABLE`（agentbay 桩）、`SKILL_INSTRUCTION_NOT_READ`（两次不读指令）、`SKILL_EXECUTION_FAILED`（空 query/一般失败）、截断场景 `meta.truncated=true` 且 summary 精确截到上限。
- `CancelledError` 显式重抛、不被转成 `SKILL_EXECUTION_FAILED`（`claude_skill_tool.py:246`），符合 changelog §3.6 关键约束；`finally` 中沙箱关闭用 shield 保护，不受调用超时影响。

### 2.2 Coordinator 并发治理（execution_coordinator.py）

6 项并发实测全部通过：

1. 同 invocation 全 safe 调用可重叠（reader 共享）；
2. writer 与 reader 零重叠（侵入式跟踪临界区验证）；
3. writer 优先：排队 writer 会挡住后到的 reader；
4. 进程级 semaphore 精确限到 `SKILL_MAX_PARALLEL_CALLS`；
5. 同名独占资源跨 invocation 串行；
6. 任务取消后租约确实释放（后续 acquire 不挂起）。

获取顺序固定为 gate → 排序后资源锁 → 全局 semaphore，无顺序死锁；`_await_release` 用 shield 保证取消时清理收口且不覆盖原始异常。`SkillPackageInvalidError` 在 lifespan 启动期 fail-fast（`agent/main.py:37` 无 try 包裹），与「配置错误直接阻止启动」一致；而 skill-center/A2A 仍 best-effort，边界区分正确。

### 2.3 完整 SKILL 包运行与 instruction-first（catalog.py / skill_runner.py / toolset.py）

- catalog 实测：非法 skill_id、非 bool `parallel_safe`、重复展示名、符号链接技能目录均在启动期拒绝；真实 `skills_data` 加载正常，`data_analysis` 的 `parallel_safe: true` frontmatter 生效。
- 用 fake ADK model 驱动真实 ADK `InMemoryRunner` 跑了 6 个端到端场景：
  - **happy**：read SKILL.md → run_python → 终局文本，attempts=1，`started/completed` 状态帧、子工具 call/result 旁路 UI、seq 单调、身份字段齐全；
  - **同轮 sibling**：read_file 与 run_python 同轮发出，sibling 被 guard 拒绝（`SKILL_INSTRUCTION_NOT_READ` 工具错误），`call_soon` 延迟激活机制按设计工作，attempt 1 判不合规 → 新会话重试 → attempts=2 成功；
  - **抗命**：始终不读指令 → 两次会话耗尽 `max_llm_calls` → 抛 `SkillInstructionNotReadError(attempts=2)`，错误码映射正确；
  - 超时/截断/空 query/agentbay 桩均返回结构化 envelope。
- `_terminal_text` 的过滤条件（author 匹配、非 partial、role=model、无 function call/response、排除 thought）实测只提取终局聚合文本；子代理正文不进 UI（只旁路 tool_call/tool_result），与「主 Agent 是唯一最终答复者」一致。
- 验证了 ADK 2.3.0 的 `populate_client_function_call_id` 会为缺失 ID 的 function call 补 ID——budget 原子配对和 UI 展示依赖的 ID 是有保证的。

### 2.4 沙箱与取消（local_sandbox.py / base.py / agentbay_sandbox.py / stream_merge.py）

- staging 实测：普通文本/脚本/二进制正确复制，`__pycache__` 等被忽略，重复目标拒绝，包内符号链接拒绝且失败时部分目标被清理；本机 Python 3.12 的 `rglob` 不跟随符号链接，无遍历放大风险。
- 路径安全：`relative_to` 校验拒绝 `../../etc/passwd` 类逃逸；`read_file` 拒绝 FIFO（实测不阻塞）、目录等非普通文件。
- 进程治理：`start_new_session=True` + killpg TERM→KILL 升级；超时杀 `sleep 5` 后实测无残留进程；取消路径对「子进程创建中被取消」也做了二次 shield 等待后清理。
- 文件 I/O 全部线程化且取消安全（`_to_thread_cancel_safe`）；`close()` 幂等删除 workdir 实测通过。
- `stream_merge` 取消协议：pump 的 CancelledError 不再伪装成 error SSE，finally 主动 cancel + 等待 + `aclose()` + 重置 ContextVar；sentinel 改 `put_nowait` 避免取消时挂起。

### 2.5 MessageBudget、SSE/UI、两引擎共享

- MessageBudget 实测：大体积 function call/response JSON 计入预算；按 call ID 原子配对裁剪后**无孤立 call/response**；`keep_recent` 边界恰好落在 call/response 对中间时也不拆对。
- `tool_call/tool_result` 透出 ADK id；Claude SKILL 的 `skill_event` 统一携带 `parentInvocationId/skillCallId/skill/seq/subEvent`，skill-center 展示帧保留 `requestId/seq` 并新增 `subEvent:"display"`；`web/app.js` 按短 ID 区分同名并行调用。UI 队列集成实测：设置队列后 ClaudeSkillTool 全程事件（status×3 + tool_call/result×2）正确入队且身份与最终 envelope 一致。
- 两引擎共享确认：`agent_loop_engine.py:57`（ctx.tools 直接入工具表，Claude Skill 不藏在 tool_search 后）与 `execution_planner.py:45/63` 都消费 ctx.tools + `merge_runner_events`，changelog「两代引擎共享 Runtime」成立。
- agent_loop instruction 更新（计划非调度器、依赖跨轮串行、同轮仅限独立任务、按 error.code/retryable 决策）与 changelog §3.7 一致。

### 2.6 验证门

`py_compile`（agent/arag/common/skillcenter/a2a_service 全量）与 `git diff --check` 复跑通过；requirements 新增 `PyYAML>=6,<7`、`.env.example` 四项配置、config 用 `Field(gt=0)` + `Literal["local","agentbay"]` 校验，均与文档一致。

## 3. 值得肯定的设计点

1. **职责边界克制**：Coordinator 明确「只治理执行，不承担任务编排」，没有引入第二套 Loop/状态机，与项目背景「用最先进的方案但不造多余抽象」吻合。
2. **instruction-first 双层保护**：guard 结果 + `call_soon` 延迟激活精确处理了 ADK 同轮并行 function call 的竞态，实测 sibling 拒绝语义完全符合设计意图。
3. **取消语义诚实**：CancelledError 全链路不吞、不降级为业务错误；所有清理用 shield + 二次等待，且清理异常不覆盖原始异常。
4. **失败要响亮**：SKILL 包配置错误阻断启动，运行期错误走稳定错误码 envelope，`retryable` 语义给主 Agent 可决策的重试/换路信号。
5. **有界结果**：summary 统一截断 + `meta.truncated`，多 Skill 场景不会撑爆主上下文。

## 4. 问题与建议

### 4.1 中等

**M1. 死代码：`ClaudeSkillTool._detect_error_in_response`**（`agent/claude_skill/claude_skill_tool.py:102-108`）。
全仓库无调用点（已 grep 确认）。要么删除，要么接到失败状态发射处（如 `run_async` 末尾 `response.get("isError")` 判断处复用其错误码提取逻辑）。当前形态会让后续维护者误以为有未覆盖的错误探测路径。

### 4.2 低

**L1. `stream_merge` 清理是 best-effort，与 changelog 表述略有落差**（`agent/skills/stream_merge.py:49-54`）。
`with suppress(asyncio.CancelledError): await task` 在二次取消到达时会提前跳出，`runner_events.aclose()` 也可能被再次取消打断。单次断开（uvicorn 常规行为）下清理能完成，但「等待 Runner 与在途 Tool 退出」并非强保证。建议代码注释或 RUNBOOK 中明示「best-effort 收口」。

**L2. `_run` 子进程创建窗口内的双重取消可能泄漏进程**（`agent/claude_skill/sandbox/local_sandbox.py:70-77`）。
取消处理器内第二次 `await asyncio.shield(create_task)` 若再被取消，`except Exception` 接不住 CancelledError，spawn 出的进程组可能无人清理。概率极低（需要在 spawn 毫秒窗口内连续两次取消），记录知悉即可；若要彻底可把终止逻辑包进 `try/finally`。

**L3. MessageBudget 冗余解包**（`agent/engine/agent_loop/message_budget.py:113-117`）。
`_, drop_end = units[first_unit]` 随后又 `drop_start, drop_end = units[first_unit]`，第一行可删。纯整洁问题。

**L4. PyYAML 1.1 布尔语义提示**（catalog frontmatter）。
`parallel_safe: yes/no/on/off` 会被 PyYAML 解析成布尔值从而通过校验。行为无害，但建议在 RUNBOOK「SKILL frontmatter 契约」处示例统一用 `true/false`，避免未来技能作者困惑。

**L5. `_parse_skill_md` 用 `split("---", 2)` 提取 frontmatter**（`agent/claude_skill/catalog.py:48`）。
frontmatter 内部出现 `---`（如多行字符串）会解析失败。当前失败是响亮的（启动期报错），且技能包来自受控仓库目录，可接受；如未来放开技能来源可换更严格的 `---` 行首匹配。

**L6. 空 query 返回 `retryable=True` 可商榷**（`claude_skill_tool.py:172-179`）。
空 query 属调用方错误，retryable 语义上更接近 false；不过主模型修正 query 后重试确实可能成功，保持现状也合理，仅提出供讨论。

## 5. 后续建议

1. changelog §9 的 10 个评测场景中，1/2/4/5/6/8/9 已被本次评审的离线验证部分覆盖（fake model + stub）；**场景 7（真实 SSE 客户端断开全链路收口）和场景 10（真实 LLM 下两引擎行为一致性）仍建议人工跑一遍**——尤其场景 7 涉及 uvicorn 取消时序，离线难以完全模拟。
2. 若后续给 `data_analysis` 之外新增技能包，建议同步补一个「non-safe + 独占资源」的样例技能，让串并行语义在演示层可见。
3. `_detect_error_in_response`（M1）处理掉之后，本次改造即可视为完全收口。
