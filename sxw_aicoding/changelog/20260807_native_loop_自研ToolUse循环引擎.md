# native_loop：不依赖 Agent 框架的自研 Tool-Use 循环（Gen3 引擎）

生成时间：2026-08-07

## 背景

### 起因

以 Claude Code 2.1.88 源码（`~/Downloads/claude_code_src-master/`）为蓝本，在现有代码基础上实现一个**不借助 google-adk 等 Agent 框架**的新引擎，复刻 CC 的 tool_use 循环思路。

### 评估阶段的一个关键修正

用户最初指名的参考文件是 `src/QueryEngine.ts`。实际读源码后发现：**`QueryEngine.ts`（1295 行）里没有 tool_use 循环**，它是会话生命周期外壳——持有 `mutableMessages` / `abortController` / `totalUsage` / `permissionDenials`，负责 system prompt 组装、slash command 预处理、transcript 落盘、budget 与 maxTurns 终止判定。

真正的循环在 `src/query.ts:241 queryLoop()`（约 1500 行），工具编排在 `src/services/tools/toolOrchestration.ts`，单工具执行在 `src/services/tools/toolExecution.ts`。后续所有设计以这三处为准。

### 为什么值得做

现有 `ENGINE=agent_loop`（[agent_loop_engine.py](../../agent/engine/agent_loop/agent_loop_engine.py)）124 行里**没有一个 `while`**——真正的循环在 ADK `BaseLlmFlow.run_async()` 内部，我们只在 Plugin / LiteLlm 子类两个扩展点上挂策略。这导致两件事在 ADK 插件面**根本表达不出来**：

1. 一轮内多个工具调用的**调度**（只读并发 / 有副作用串行）；
2. **流式工具执行**（一轮内除最后一个之外的 `tool_use` 块可提前开跑，不必等模型流结束）。

同时，为绕开 ADK 构造 `types.FunctionCall` 时的前置校验，项目背着 [tool_args_normalizer.py](../../agent/llm/tool_args_normalizer.py) 132 行 monkeypatch + `google-adk==2.6.2` 精确 pin + "私有契约须随版本审计"的长期负债。自研循环里参数解析归我们，这笔负债直接消失。

### 参考来源

| 主题 | 路径 |
|---|---|
| 循环主体 | `src/query.ts:241-1728` |
| 工具分批 / 并发 | `src/services/tools/toolOrchestration.ts` |
| 单工具执行与错误合成 | `src/services/tools/toolExecution.ts:337-490` |
| 压缩阈值 | `src/services/compact/autoCompact.ts` |
| 摘要 prompt 结构 | `src/services/compact/prompt.ts:60-130` |

---

## 拍板结果

| # | 议题 | 决策 |
|---|---|---|
| ① | 第三个引擎并存，还是替换 `agent_loop`？ | **并存**，`ENGINE` 环境变量选主引擎。理由：项目论点就是引擎演进对比；删掉 ADK 版会同时删掉"生产加固落在 ADK 两个扩展点"这条面试主线 |
| ② | 子 Agent / 子 Runner 的引擎归属？ | **也走环境变量**（`SUB_AGENT_ENGINE`）。**A2A 除外**——远端跑在 `a2a_service:8300` 自己的 ADK 上，agent 侧开关改不了它；保留 ADK `RemoteA2aAgent` 并如实标注 |
| ③ | 上下文治理做到哪一档？ | **按 CC 来**：阈值触发摘要 + compact boundary + preserved tail + 413 反应式恢复（而非 `MessageBudget` 那种硬裁头部） |
| ④ | 评测与单测？ | **本轮都不做**。只保证代码正确、高质量。文档必须如实标注"未评测" |
| A | A2A 是否强制原生？ | **不强制**（采纳建议）。手写 JSON-RPC 是约 200 行协议兼容风险换零学习价值 |
| B | 新引擎命名？ | **`native_loop`**（采纳建议）。对比轴是"循环归谁驱动"：`agent_loop` = ADK 驱动，`native_loop` = 自主实现。现有 `agent_loop` 代码名不改，只在文档里补称 "ADK-driven" |

---

## 方案

### 设计主线：CC 的 7 个不变量

| # | 不变量 | 落点 |
|---|---|---|
| 1 | 退出信号是「本轮有没有 tool_call」，**不是 `stop_reason`**（CC `query.ts:553` 明确注明 stop_reason 不可靠，Qwen 上同样成立） | `loop.py` `needs_follow_up` |
| 2 | 每个 tool_call 必须有配对 tool_result，四条合成路径：工具不存在 / 参数非法 / 工具异常 / 请求取消。少一条配对，下一轮请求被上游直接判 400 | `executor.py` |
| 3 | 工具失败 = `is_error` 的 tool_result，**不是异常**，循环从不因工具挂掉而中断 | `executor.py` try/except |
| 4 | 按只读性分批：连续 `concurrency_safe` 并发，其余串行，**批次间保序** | `executor.partition()` |
| 5 | 状态就是一个扁平 messages 数组，续推靠 `state = 新状态; continue` | `loop.py` `LoopState` |
| 6 | 每个 continue / return 点带**命名 transition**，纯为可观测 | `[LoopControl]` 日志 + `stop_reason` |
| 7 | **恢复优先于失败**：上下文超长不是终止条件，而是"压缩后重来一轮" | `compact.py` + `loop.py` |

### 模块布局

```
agent/engine/native_loop/
    engine.py       ReasoningEngine 端口适配 + NativeRuntime 进程级单例
    loop.py         ★ 核心 while 循环（queryLoop 对应物）
    executor.py     工具分批 / 并发 / 四路合成 tool_result
    llm_client.py   流式 OpenAI 兼容客户端 + tool_calls 增量累积
    messages.py     Msg 模型 + genai Content 转换 + 原子单元 + 体积治理
    tools.py        ToolSpec + 函数→Schema + ADK BaseTool 适配 + NativeToolContext
    adk_bridge.py   ADK AgentTool 桥接（见"偏离方案"第 1 条）
    compact.py      CC 式阈值压缩 + boundary + preserved tail
    history.py      会话历史存储
    sub_agent.py    researcher 原生实现
agent/engine/loop_tools/            ← 两代 loop 引擎共享
    __init__.py         LOOP_INSTRUCTION + 提醒文案 + resolve_sub_agent_engine
    task_plan_tool.py / tool_search_tool.py / sub_agent_tool.py(ADK 版)
agent/claude_skill/skill_drivers.py  Claude SKILL 子 Runner 的可换内核
```

### 关键设计决定

**ToolSpec 统一抽象** —— 一行现有工具代码都不改。两个适配器：`from_function`（inspect + docstring Args 生成 schema，**按参数名排除 `tool_context`**）、`from_adk_tool`（复用现成的 `_get_declaration()` + 转调 `run_async`）。

`NativeToolContext` 一个鸭子类型 shim 同时满足三处既有 `getattr` 需求：`function_call_id` / `invocation_id`（[call_identity.py](../../agent/skills/call_identity.py) 的技能调用身份）与 `state`（`update_task_plan` 写状态）。

**tool_calls 累积器兼容两种分片形态** —— DashScope 实际行为未经实测，累积器按 `index` 聚合、`id`/`name` 取**首次非空值**、`arguments` 字符串累加，标准分片与一次性完整返回都收敛到同一结果。

**流式工具执行的安全设计** —— 提前投递需同时满足两个条件：已出现更高 `index`（说明前一个已完整）**且**已累积参数能解析成合法 JSON 对象（半截 JSON 解析不过，天然的安全闸）。分批语义靠"只要目前为止全是并发安全的就继续提前投递，一旦出现非安全工具就停止提前"来保住 CC 的「并发前缀 + 顺序其余」。

**上下文治理的行为变更** —— `agent_loop` 的 `MessageBudget` 是"硬裁头部整条丢弃"，与摘要压缩冲突（丢掉的历史再也摘要不到）。`native_loop` 里它降级为 CC 的 `applyToolResultBudget` 形态：**只替换超大 tool_result 的体积，不丢消息**（消息条数不变 → call/response 配对天然保持），整段丢弃交给摘要。旧引擎行为不动——这个差异本身就是两代对比素材。

**preserved tail 必须落在原子单元边界** —— 从 call/response 区间中间切开会留下孤立 tool 消息，请求被判 400。已对 1~5 各档位逐一验证。

---

## 实际改动点

### 新增（约 2673 行）

| 文件 | 行数 | 说明 |
|---|---|---|
| `agent/engine/native_loop/loop.py` | 431 | 核心循环 |
| `agent/engine/native_loop/tools.py` | 367 | ToolSpec + 两个适配器 |
| `agent/engine/native_loop/llm_client.py` | 300 | 流式客户端 + 累积器 |
| `agent/engine/native_loop/messages.py` | 239 | 消息模型 + 原子单元 |
| `agent/engine/native_loop/compact.py` | 225 | CC 式压缩 |
| `agent/engine/native_loop/executor.py` | 221 | 工具执行与分批 |
| `agent/engine/native_loop/adk_bridge.py` | ~150 | AgentTool 桥接 |
| `agent/engine/native_loop/engine.py` | 134 | 端口适配 |
| `agent/engine/native_loop/history.py` | 53 | 会话历史 |
| `agent/engine/native_loop/sub_agent.py` | 43 | researcher 原生版 |
| `agent/engine/loop_tools/__init__.py` | 53 | 共享指令 + 子引擎解析 |
| `agent/claude_skill/skill_drivers.py` | 183 | 可换内核（adk / native） |
| `scripts/probe_dashscope_tool_stream.py` | 271 | P0 探针（一次性诊断，不参与服务） |

### 移动（`git mv`，两代 loop 共享工具面）

- `agent/engine/agent_loop/{task_plan_tool,tool_search_tool,sub_agent_tool}.py` → `agent/engine/loop_tools/`

### 修改

| 文件 | 改动 |
|---|---|
| `agent/config.py` | 新增 `sub_agent_engine` / `native_streaming_tool_exec` / `native_max_tool_concurrency` / `native_tool_result_max_chars` / `context_window_tokens` / `compact_buffer_tokens` / `compact_preserve_units`，全部有默认值 |
| `agent/engine/base.py` | `build_engine()` 加 `native_loop` 分支 |
| `agent/engine/agent_loop/agent_loop_engine.py` | 改用共享 `LOOP_INSTRUCTION`；researcher 按 `SUB_AGENT_ENGINE` 选实现；import 指向 `loop_tools` |
| `agent/engine/agent_loop/loop_processor.py` | import 指向 `loop_tools` |
| `agent/claude_skill/skill_runner.py` | 拆成策略外壳 + 可换内核（`_run_attempt` 改为转调 driver，230 行策略逐行保留） |
| `agent/claude_skill/claude_skill_tool.py` | 新增 `concurrency_safe` / `exclusive_resources` 公开属性，把 frontmatter 并发语义暴露给主循环调度 |
| `eval/harness/runner.py` | `--engine` choices 加 `native_loop`（留口，本轮不产数字） |
| `README.md` / `RUNBOOK.md` / `CLAUDE.md` / `AGENTS.md` | 三代引擎说明、新增环境变量表、诚实边界声明 |

### 新增配置

```
ENGINE                      plan_execute | agent_loop | native_loop   # 默认仍为 agent_loop
SUB_AGENT_ENGINE            auto | adk | native                       # 默认 auto（跟随主引擎）
NATIVE_STREAMING_TOOL_EXEC  true          # 安全阀：false 退化为"流完再统一跑工具"
NATIVE_MAX_TOOL_CONCURRENCY 10            # 对齐 CC 默认
NATIVE_TOOL_RESULT_MAX_CHARS 8000
CONTEXT_WINDOW_TOKENS       128000
COMPACT_BUFFER_TOKENS       13000         # 对齐 CC
COMPACT_PRESERVE_UNITS      6
```

---

## 与方案的偏离

### 1. 方案里有一个错误，已修（重要）

方案中写"A2A 经 `from_adk_tool` 适配"是**错的**。实际读 ADK 源码发现 `AgentTool.run_async` 硬依赖 `tool_context._invocation_context`（取 `user_id` / `credential_service` / `plugin_manager`，再据此建子 Runner）。鸭子类型 shim 给不出这些，直接转调抛 `AttributeError`，被 executor 兜成 `is_error` —— 表现为 **A2A 工具在 native_loop 下永远失败，且静默**。

已验证复现，新增 `adk_bridge.py` 自建子 Runner 驱动（与 `skill_runner.py` 给 Claude SKILL 建独立子 Runner 是同一套做法），`build_registry` 自动识别 `AgentTool` 并改道。

副作用是好的：researcher 的 ADK 版也能在 native_loop 下跑，`SUB_AGENT_ENGINE` 的组合矩阵**没有洞**（3 主引擎 × 3 子引擎设置 = 9 种全部可构造，已验证）。

### 2. `loop_tools/` 提取从 P5 提前到 P1

否则 `native_loop` 会反向 import `agent_loop`。顺带把 `LOOP_INSTRUCTION` 收成单份常量，两代 loop 引擎 import 同一个对象（`is` 判定验证过）——杜绝 prompt 漂移，否则引擎对比会退化成 prompt 之争。

### 3. 阶段边界与估算修正

- P1（骨架）/ P2（可靠性）/ P3（并发）实际一起写完：四路合成 tool_result 是执行器的核心正确性，与骨架不可分割。
- 评估阶段对 P4 的"约 150 行"估计偏低，中途已纠正为 300–400 行，最终 `compact.py` 225 行 + 循环内接入逻辑，落在修正后的区间。
- 整体从最初估的 900–1100 行修正为 1600–1800 行，实际 2673 行（含探针 271 行与 `adk_bridge.py` 这个计划外的 150 行）。

---

## 验证情况

### 已验证（编译门 + 离线自测）

`py_compile` 全量通过。以下均由自动化脚本断言，非目测：

- **工具层**：7 个函数工具的 schema 生成正确、`tool_context` 未泄漏到模型可见参数
- **累积器**：标准分片（跨片拼 arguments）与一次性完整返回两种形态都收敛；半截 JSON 不被误判就绪；安全阀关闭时永不提前投递
- **执行器**：四条合成路径（工具不存在 / 顶层数组参数 / 坏 JSON / 工具抛异常 / 取消）全部产出配对 `role=tool` 消息
- **分批调度**：连续只读并发、写状态串行、批次间保序；Claude SKILL 的 `parallel_safe` + `exclusive_resources` 真正驱动主循环分批
- **消息层**：并行调用与其两条结果合成单个原子单元；线格式符合 OpenAI 契约；体积治理不改变消息条数
- **循环机制**（假模型端到端）：工具轮→回答轮、退出信号、force-summary 注入时机（`[False, True, True]`）、硬熔断收口、plan_step 翻译与 tool_result 抑制、计划续推提醒时机、多轮历史累积、system 指令置首
- **压缩链路**：阈值判定与 `estimated` 标注、preserved tail 在 1~5 各档位都不切开 call/response、摘要抽取三种退化形态、摘要 `max_tokens=4096`（远大于 `AgentChatClient` 默认 512）
- **413 恢复**：超长 → 压缩 → 重来一轮 → 正常出答案，中间错误未泄漏给前端；反复超长时单次守卫生效，如实报错而非死循环。**验证方式是注入异常**——真实 provider（DashScope）超长时的确切报文尚未确认，探针的 `context_overflow` case 就是为拿它准备的
- **回归**：工具失败后循环继续、无悬空 tool_call、客户端断开路径可正常收口、`stop_reason` 供嵌套场景判定
- **SSE 契约**：事件**类型**覆盖 ADK 侧全集；**收口序列**在初版曾与另两代不一致（失败路径不发 `done`），已于评审后修复，现四条出口一律以 `done` 收尾

### 未验证（需真实密钥 / 端到端环境）

**P0 探针尚未运行**（无密钥，且不应由 AI 接触密钥）：

```bash
export DASHSCOPE_API_KEY=sk-***
.venv/bin/python scripts/probe_dashscope_tool_stream.py
```

探针输出决定三件事：① `arguments` 是否需跨 chunk 拼接 ② `id`/`name` 是否只在首片出现 ③ **usage 是否随流返回** —— 第 ③ 点影响压缩行为：拿不到 usage 时阈值只能用字符估算。累积器已兼容两种形态，探针结果用于确认/收紧。

其余未覆盖项：真实模型行为、多模态转换（手写的 `inline_data` → base64 `image_url`）、技能沙箱端到端、A2A 桥接实跑、并发竞态、长会话累积漂移、`SUB_AGENT_ENGINE` 非主路组合。端到端手工验收清单见 `RUNBOOK.md`。

---

## 诚实边界（已写入四份文档）

- `native_loop` **尚未评测**。`eval/reports/` 下所有数字来自 `agent_loop` / `plan_execute`，不得套用。按既有教训，同一 prompt 改动可能让不同引擎产生相反收益，新引擎必须自己跑基线。
- A2A 客户端仍是 ADK 实现，**不随 `SUB_AGENT_ENGINE` 切换**；远端 `a2a_service` 本身就是 ADK。
- compact 阈值在上游未返回 usage 时为**字符估算**，非精确 token 计数（日志标 `estimated=true`）。
- ADK 仍是 `native_loop` 的**库依赖**（A2A + 被适配的 BaseTool 工具 + 桥接用的 Runner），只是不再驱动主循环。
- 压缩采用"摘要 + 保留尾部**替换**整段历史"（同 CC），代价是早期原文不再可回溯。

---

## 代码评审与修复（2026-08-07 同日）

评审报告：`sxw_aicoding/review/20260807_native_loop_engine_review.md`（结论：通过，4 个中等问题需在跑评测前修掉）。
逐条实跑复现，**全部真实存在、无误报**，已全部修复。

评审有一个共性观察值得记下：**4 个中等问题全部落在恢复/兜底路径上，而恢复路径恰恰是本次改动主打的差异化能力**。
自评时主链路用假模型验了 8 个场景，恢复路径却只验了「注入异常 → 压缩 → 重来」这一条快乐路径，
没验「分类是否真判得出 overflow」和「收口契约是否与另两代一致」——这是验证设计的盲区，不是实现的疏忽。

| 编号 | 问题 | 修复 |
|---|---|---|
| **M1** | `HistoryStore` 400 条上限裸切片，会切开 call/response，使会话**永久性** 400 | 新增 `messages.unit_aligned_start()`，切分点一律对齐原子单元；切分点落在末单元内部时保留整个单元（宁可略超上限也不切坏）。cap=1~6 各档位实测无孤立 `tool` |
| **M2** | 压缩后 `last_usage` 未失效，旧 `prompt_tokens` 把字符估算顶回原位，触发冗余二次压缩（摘要被二次摘要） | 新增 `_adopt_compacted()`：采用压缩结果的同时作废 `last_usage`，两条压缩路径共用。另把 `compact_failures>0` 的**永久关闭**改成 `compact_cooldown` 冷却 3 轮 |
| **M3** | 三条失败收口不发 `done`，与另两代 SSE 契约不一致 → citation 丢失、评测 `finished=false` | 收敛为 `_fail()` / `_complete()` 两个出口，四条路径一律以 `done` 收尾，`finish_reason` 带 transition 名（`hard_cap` / `model_error`），下游可机器可读地区分收口原因 |
| **M4** | `classify_llm_error` 的 isinstance 分支只对 litellm 成立，native 路径退化为纯英文子串匹配，恢复链路可能在默认 provider 上是死代码 | 三层：① 共享 `exceptions.py` 扩充关键词（含 DashScope 常见措辞与中文报文，对 ADK 路径同样是净收益）；② native 侧加 provider 无关的**体积兜底判据**（400 + 请求体积 ≥ 窗口×0.9 → 按超长处理）；③ 探针新增 `context_overflow` case，直接打印上游原始报文并当场判定关键词是否覆盖 |
| L1 | Claude SKILL 子 Runner 每次调用新建 `NativeLlmClient`（httpx 连接池泄漏） | 新增 `get_shared_client()` 进程级单例，主引擎与子 Runner 共用 |
| L2 | openai 流未显式关闭（`AsyncStream` 无 `__del__`） | `_consume` 改 `async with` |
| L3 | `loop.py` 重写了 `_has_open_steps` / `TASK_PLAN_KEY` | 收进 `loop_tools/__init__`（不引 ADK），`task_plan_tool` 与 `loop.py` 共用同一对象 |
| L4 | 「tool_call 一到就开跑」口径过宽 | 四份文档 + 两处代码注释统一收紧为「一轮内除最后一个之外可提前开跑，末个必等流末」 |
| L5 | `.env.example` 缺 7 个新变量 | 补齐并加注释 |
| L6 | 三处注释/文档与代码不符 | `base.py`（两代→三代）、`context.py`（`chat` 用途已扩大）、`messages.py`（边界前历史其实已被替换掉） |
| L7 | `skill_runner._APP` 死代码 | 删除 |
| L8 | token 估算漏掉 system 指令 + 工具 schema（实测约 1805 tokens） | `estimate_tokens` 新增 `fixed_overhead_chars`，循环启动时算一次；两个方向的已知偏差写进 docstring |
| L9 | `GeneratorExit` 不走 `CancelledError` 分支，提前投递的工具会变游离 task | 两处 `except asyncio.CancelledError` 改 `except BaseException` 并置于具体异常之后 |

修复后复验：`py_compile` 全量通过；M1~M4 与 L1/L3/L8 逐条实跑断言；主链路回归（工具轮→回答轮、配对完整、收口正常）通过。

---

## 后续可做

1. 跑 P0 探针，据结果收紧累积器与压缩阈值策略。
2. 按 RUNBOOK 手工验收清单跑端到端，重点是多模态与技能沙箱。
3. 起第三个 agent 实例（:8002）跑评测，产出 `native_loop` 自己的基线。
4. 三代引擎在同一考卷上的对比分析——这是本次改动真正的价值出口。
