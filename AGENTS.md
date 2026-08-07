# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

> 项目定位与能力总览见 `README.md`，逐步运行、环境变量和排障见 `RUNBOOK.md`，评测方案见 `eval/README.md`。本文件记录 AI 编码时需要掌握的项目背景、目标、跨模块架构与工程约定。

## 项目背景与定位

本项目由公司生产项目的核心链路抽取、简化而来，用于个人学习、技术方案验证和面试准备。目标是保留生产级 Agent 系统的主链路形状和关键工程取舍，并将它们组织成可在本机独立运行、便于讲解和持续实验的样板工程；它不是生产源码的完整镜像，也不承担真实线上流量。

需要对照生产实现时，可参考以下本机源码，但应提炼设计而不是机械复制内部治理逻辑：

| 方向 | 参考路径 |
|---|---|
| 接入层、会话管理、文件上传 | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/fy26_albert_chat2/albert-chat-2` |
| Agent 核心运行时与推理引擎 | `/Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2` |
| 技能中心与 A2A | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/2026_albert-skill-center_proj/albert-skill-center` |
| ARAG | `/Users/shixiangweii/PycharmProjects/arag_learn_proj/lippi-arag` |

## 项目目标

1. 展示 Plan-Execute → ADK 驱动的 Tool-Use Loop → 自研 Tool-Use Loop 三代推理引擎的演进、统一抽象、适用场景和行为差异。
2. 复刻工具调用、技能执行、子代理委派、异常反馈、熔断、降级和引用生成等生产级主链路。
3. 展示向量检索、BM25、RRF 融合、查询改写、多模态入库与持久化组成的工程化 RAG。
4. 通过 SSE、trace_id、结构化日志和真实 LLM 黑盒评测，让系统可运行、可观察、可比较。
5. 为面试讲解和后续技术实验提供结构清晰、边界诚实、易于演进的代码基线。

## 改造原则与交付要求

- **先进方案优先**：本项目不要求历史兼容、存量技术债兼容或线上灰度兼容。旧接口、数据结构和实现不再合理时，可以直接调整、替换或删除，无须为历史行为保留兼容层。
- **主链路必须完整**：不背兼容包袱不等于降低工程质量。改动后应保持四服务可启动、核心链路可运行、可选下游故障可降级，并完成与风险相称的验证。
- **跨文件保持一致**：代码变更涉及架构、配置、端口、命令、能力边界或评测行为时，同步更新 `README.md`、`RUNBOOK.md`、本文件、`CLAUDE.md` 和相关评测资料。
- **聚焦核心价值**：优先实现能说明 Agent Runtime、RAG、技能/A2A、可靠性或评测方法的能力；与学习和面试主线无关的企业内部治理可以继续裁剪。
- **诚实描述边界**：不得把演示桩或预留端口写成已投产能力。`native_loop` 尚未评测，`eval/reports/` 下的数字全部来自 `agent_loop`/`plan_execute`，不得套用；其压缩阈值在上游不返回 usage 时是**字符估算**（日志标 `estimated=true`），不是精确 token 计数。PromptCache 的显式缓存断点仅对 Anthropic 生效；AgentBay 未接真实 SDK；GraphStore 未接检索流；LocalSandbox 不是生产级隔离；Claude SKILL 尚不支持 Artifact 跨 Skill 传递和 HITL/暂停恢复。
- **保护敏感信息**：API Key 只允许通过真实环境变量或被 Git 忽略的本地 `.env` 注入，禁止写入代码、文档、评测产物或提交历史。

## 当前系统概览

基于 **Google ADK 2.6.2** 精简复刻的生产级 AI Agent 系统，由 **4 个独立的 FastAPI/uvicorn 服务**经 HTTP 协作。核心卖点是「**Agent 运行时的规划/推理引擎**」与混合召回 RAG。代码与文档以中文为主。

## 常用命令

所有 Python 命令都走仓库自带 `.venv`（**不要用系统 python**）：

```bash
PY=.venv/bin/python

# 一键启动全部 4 服务（先下游后 agent，自动健康检查 + 入库样本知识库），Ctrl-C 一并退出
bash scripts/run_all.sh

# 单独起某个服务（注意 agent 须最后起：它启动时会拉技能目录 + 发现 A2A）
$PY -m uvicorn a2a_service.main:app --port 8300
$PY -m uvicorn skillcenter.main:app --port 8200
$PY -m uvicorn arag.main:app        --port 8100
$PY -m uvicorn agent.main:app       --port 8000

# 入库样本知识库（首次运行或清空 local_storage/embedding 后执行；重复入库按 chunk_id 覆盖）
curl -X POST http://127.0.0.1:8100/v1/index/sample

# 重建虚拟环境（仅当 .venv/ 不存在）
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**本仓库没有单元测试**。`py_compile` 是约定的编译校验门（替代单测）：

```bash
find agent arag common skillcenter a2a_service -name '*.py' | xargs "$PY" -m py_compile
```

端到端评测（真实 LLM 黑盒、解析 SSE）走 `eval/` harness；它依赖**两个 agent 实例**（8000=agent_loop / 8001=plan_execute，因 `ENGINE` 是启动期配置不可单请求切换）：

```bash
export DASHSCOPE_API_KEY=sk-***   # 仅环境变量，切勿写入任何文件
bash eval/run_eval.sh             # 两引擎各跑一遍并聚合报告；arag-down pass 需手动停 arag 后单独跑
# 或手动分步：
$PY -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000 --out eval/reports/<ts>
$PY -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001 --out eval/reports/<ts>
# native_loop 需第三个实例（:8002）；本轮尚未产出评测数字
$PY -m eval.harness.report --out eval/reports/<ts>      # 聚合出 summary.md
# 单 suite：--suite routing；arag-down 鲁棒性子集：先停 arag 再加 --only-arag-down
# 降方差：--repeat 3；不重跑 LLM 只重评分：$PY -m eval.harness.rescore --out eval/reports/<ts>
```

> 首版报告位于 `eval/reports/20260629-090605/`；A/B prompt 回归位于 `eval/reports/{baseline-r3,improved-r3}/`。现有结果表明同一 prompt 改动可能让两代引擎产生相反收益，因此修改 prompt、工具集或循环控制时必须分别验证两代引擎，不能用一套结果代替另一套。

## 架构大局

### 服务拓扑与调用链
```
用户 → agent(:8000) ──┬─→ arag(:8100)         知识检索（混合召回 RAG）
                      ├─→ skill-center(:8200)  技能目录/执行 + A2A 注册表
                      └─→ a2a_service(:8300)   A2A 远程子代理（ADK to_a2a 暴露 math_expert）
```
**对外只需访问 agent**。下游不可用时一律 **best-effort 降级**（不阻断 agent 启动 / 不中断对话），对应能力静默跳过——排障时按 `[QaRetrieve] degraded`、`[SkillCatalog] ... skip`、`[A2ALoad] ... skip` 等日志定位。

agent 还内置浏览器 Web Chat：`web/` 中的静态资源由 `agent/main.py` 挂载到 `GET /chat-ui/`，根路径 `/` 重定向到该页面；文档上传经 agent 的 `POST /api/v1/documents/index` 代理转发到 arag `/v1/index`。

### 三代推理引擎（核心抽象）
`agent/engine/base.py` 定义统一端口 `ReasoningEngine.run_stream(ctx) -> AsyncIterator[StreamEvent]`，由 `build_engine()` 按 `ENGINE` 配置选型。对比轴是**循环归谁驱动**：
- **`plan_execute`**（Gen1）：`decision_planner` 出计划 → `execution_planner` 逐步执行。前置规划、可控可解释。
- **`agent_loop`**（Gen2，默认）：Tool-Use Agent Loop，但 `while` 在 ADK `BaseLlmFlow.run_async()` 内部；我们只在 Plugin / LiteLlm 子类两个扩展点上挂策略。
- **`native_loop`**（Gen3）：**自研循环，不依赖任何 Agent 框架**，以 Claude Code `src/query.ts:241 queryLoop()` 为蓝本，`while` 在 `agent/engine/native_loop/loop.py` 里。

**三引擎共享**同一套工具面、系统指令、检索/技能/citation 下游与 SSE 契约；切换只改「循环归谁驱动」。

**工具面**：`translate`/`text_stats`/`tool_search`/`update_task_plan`/`researcher` 是两代 loop 引擎共有、`plan_execute` 没有的（`plan_execute` 只用 `ctx.tools`）。共享部分收在 `agent/engine/loop_tools/`（含 `LOOP_INSTRUCTION`），两代 loop 引擎 import 同一份——**改工具面或 prompt 必须两边同时生效**，否则对比就退化成工具面之争 / prompt 之争。评测只在共享子集上对比。

**`native_loop` 相对 `agent_loop` 多出的能力**（都是 ADK 插件面表达不了的）：
1. 按只读性分批的**工具并发调度**（连续 `concurrency_safe` 并发、其余串行、批次间保序；Claude SKILL 的 `parallel_safe`/`exclusive_resources` frontmatter 在这里真正驱动主循环调度）；
2. **流式工具执行**——**一轮内除最后一个之外的 tool_call 可提前开跑**，不必等模型流结束（完整性信号是「出现了更高 index」，故末个调用必然等到流末；单工具调用轮因此无收益）。安全阀 `NATIVE_STREAMING_TOOL_EXEC=false` 可退化为流完再跑；
3. CC 式**上下文压缩**：阈值触发摘要 + compact boundary + preserved tail + 413 反应式恢复（`agent_loop` 是 `MessageBudget` 硬裁头部，两者行为不同，是有意保留的对比素材）；
4. 自己拥有工具参数解析，因此**不需要** `tool_args_normalizer` 那套 ADK 私有符号 monkeypatch。

`native_loop` 的每个续推/收口点都带命名 transition（`next_turn`/`force_summary`/`reactive_compact_retry`/`hard_cap`/`completed`/`model_error`），走 `[LoopControl]` 日志。

Claude SKILL 采用 **Agent-as-Tool**：一个 Skill Agent 只执行一个 SKILL 包，对主循环表现为一次标准工具调用。项目不另设 MultiSkillOrchestrator、DAG 或独立 Skill 状态机；有依赖的 Skill 由主 Agent 收到上游 `tool_result` 后跨轮串行调用，同轮并行仅用于彼此独立的调用。`update_task_plan` 只是复杂任务的可选进度记录，不是调度器。

### 生产级加固落在两个 ADK 扩展点（面试主线）
1. **ADK Plugin** `agent/plugins/agent_invocation_plugin.py`：`before_tool_callback` 短路工具参数解析 sentinel；`on_tool_error_callback` 把框架级异常封装成 `function_response` 喂回模型、**不中断 turn**；`before_model_callback` 做计划续推 / 消息预算 / force-summary。
2. **LiteLlm 子类/适配层** `agent/llm/`：在 ADK 构造 FunctionCall 前把非对象参数规范化为 sentinel（计划顶层数组可无歧义恢复），并提供上下文超长截断重试、异常分类、PromptCache 缓存断点（provider-aware）。

> ⚠️ 诚实声明：PromptCache 显式缓存断点为 Anthropic 专属，本 demo 默认 provider（DashScope/Qwen）下为 no-op。

### 三种「扩展智能体」机制（不要混淆）
- **skill-center 技能**（`agent/skills/` → `skillcenter/`）：远程 MCP 风格执行网关；NDJSON 契约为数据帧 `eof=false` + 独立 `eof=true,data=null`；缺 EOF/坏帧按稳定错误码处理，首个失败 sticky 保留到 function response 与 `[SkillInvoke]` 日志；展示帧经 UI 队列合并为 `skill_event`。
- **SKILL Agent-as-Tool**（代码目录 `agent/claude_skill/`）：每次调用把完整技能包复制到独立沙箱的 `skills/<skill_id>/`，子 Agent 必须先读取根部 `SKILL.md` 再执行；独立 Runner 带轻量 ToolArgsGuard。Runtime 负责调用身份、超时、取消和并发治理，主 Agent 只接收标准、长度受限的 `tool_result`。`LocalSandbox` 可跑，`AgentBay` 仍是不可运行的云桩；该能力纯本地，不依赖下游服务。
- **A2A 远程子代理**（`agent/a2a/loader.py` + `a2a_service/`）：ADK 原生 A2A，经 agent-card 发现 + JSON-RPC 委派；每次远程调用是无父历史的新会话，request 必须自包含；skill-center 作注册表。

### RAG（arag）
`query → rewrite → 向量(numpy 余弦)+全文(BM25/jieba) 双路 → RRF 互惠排名融合 → 低价值过滤`。入库链路 `parse → image caption(视觉) → chunk → embed → store`。存储是**端口-适配器**设计（`arag/store/`：VectorStore / FullTextIndex / GraphStore）。当前 `local` 向量库会把 embedding 与 chunk 元数据持久化到 `local_storage/embedding/`，arag 重启后自动加载并用 chunks 重建 BM25；GraphStore 仍是内存端口占位。

### 流式与可观测
统一 SSE 事件：`text · tool_call · tool_result · plan_step · citation · skill_event · done · error`。`common/obs.py` 提供 `trace_id` 全链路透传 + 结构化 JSON 日志；日志按 `[Tag]` 前缀检索（`[QaRetrieve]` `[SkillInvoke]` `[ClaudeSkill]` `[A2ALoad]` `[LoopControl]` …）。`common/skill_contract.py` 是技能线协议契约。

## 项目特定约定与坑

- **配置**：每个服务用 `pydantic-settings` 读取**同目录 `.env`**（字段名大写即环境变量名，真实环境变量优先）。改 `.env` 后须**重启对应服务**才生效。密钥 `DASHSCOPE_API_KEY` 切勿提交。
- **换模型/换厂商**只改 `LLM_MODEL` / `LLM_BASE_URL` / `DASHSCOPE_API_KEY`（内部用 `openai/<LLM_MODEL>` 走 litellm 的 openai 兼容 provider）。
- **A2A 依赖精确锁定**：`google-adk[a2a]==2.6.2`、`a2a-sdk==1.1.2`。ADK 的 A2A 仍标 EXPERIMENTAL，导入有 `UserWarning` 属正常。
- **ADK 私有契约须随版本审计**：工具参数 shim 依赖 LiteLlm 私有转换切面，`ClaudeSkillTool._detect_error_in_response` 依赖 function flow 的动态 telemetry hook。仓库精确 pin `google-adk==2.6.2`；私有符号不匹配应启动失败而非静默降级。
- **熔断**：`MAX_LOOP_ITERS`（默认 8）是 loop 引擎软收尾轮次；硬熔断 = 该值 + 2。`agent_loop`/`plan_execute` 靠 `RunConfig.max_llm_calls`（抛 `LlmCallsLimitExceededError`），`native_loop` 是循环自查后收口为 `error` 事件。
- **子引擎切换**：`SUB_AGENT_ENGINE=auto|adk|native` 决定 researcher 与 **Claude SKILL 子 Runner** 用哪一代循环（`auto` 跟随主引擎，`plan_execute` 视同 `adk`）。**A2A 不受此项影响**——远端跑在 `a2a_service` 自己的 ADK 上，agent 侧开关改不了它。
- **ADK `AgentTool` 在 native_loop 下必须走桥接**：`AgentTool.run_async` 硬依赖 `tool_context._invocation_context`（取 user_id / credential_service / plugin_manager），鸭子类型 shim 满足不了。`build_registry` 会自动识别并改走 `agent/engine/native_loop/adk_bridge.py`（自建子 Runner）。新增 ADK 原生工具时注意这条，否则表现为「该工具永远失败」。
- **Claude SKILL 运行时配置**：`SKILL_CALL_TIMEOUT_SECONDS=120`（含排队、装载、执行和结果整理）、`SKILL_MAX_LLM_CALLS=16`、`SKILL_MAX_PARALLEL_CALLS=2`、`SKILL_RESULT_MAX_CHARS=8000`。修改后须重启 agent。
- **Claude SKILL 并发**：同一 invocation 内默认串行；只有 frontmatter 为 `parallel_safe: true` 且 `exclusive_resources: []` 时才允许并行，同时受进程级并发上限控制。声明同名独占资源的调用跨请求互斥。
- **沙箱**：`SANDBOX_PROVIDER=local` 才真实可跑（子进程，**非生产隔离**）；`agentbay` 是云桩，调用即 `SandboxUnavailableError`。每次调用整包复制且独立清理；当前没有 Artifact 跨 Skill 传递、HITL/暂停恢复或真实 AgentBay，不能依赖共享目录跨 Skill 传文件。

## 如何新增能力

- **通用工具**：在 `agent/tools/builtin_tools.py` 写带类型注解 + docstring 的函数 → 加入 `build_builtin_tools()`（ADK 自动转 FunctionTool；`native_loop` 由 `agent/engine/native_loop/tools.py::from_function` 自行生成 schema，只读工具记得加进该文件的 `_READ_ONLY_TOOLS` 才能进并发批次）。
- **skill-center 技能**：在 `skillcenter/skills.py` 的 `SKILL_DEFS` 加定义 + 在 `execute_sync`/`execute_streaming` 加分支。
- **SKILL 沙箱技能**：在 `agent/claude_skill/skills_data/<id>/SKILL.md` 写 `name`、`description`、`parallel_safe`、`exclusive_resources` frontmatter + 指令体；`scripts/`、`references/` 和资源文件放在同一技能包目录并随调用整包复制。`parallel_safe` 默认 `false`，`exclusive_resources` 默认空列表。
- **A2A 子代理**：在 `a2a_service/agents.py` 定义 ADK `LlmAgent` 并 `to_a2a` 暴露；在 `skillcenter/a2a_api.py` 注册到 `/instance/list`。
