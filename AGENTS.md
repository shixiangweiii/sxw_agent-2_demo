# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

> 详尽的能力总览见 `README.md`，逐步运行/环境变量/排障见 `RUNBOOK.md`，设计依据见 `roadmap/`，评测方案见 `eval/README.md`。本文件只记录跨多文件、不易从单文件读出的「大局」与项目特定约定。

## 这是什么

基于 **Google ADK 2.3** 精简复刻的生产级 AI Agent 系统，由 **4 个独立的 FastAPI/uvicorn 服务**经 HTTP 协作。核心卖点是「**Agent 运行时的规划/推理引擎**」与混合召回 RAG。代码与文档以中文为主。

## 常用命令

所有 Python 命令都走仓库自带虚拟环境 `env_sxw_demo/`（**不要用系统 python**）：

```bash
PY=env_sxw_demo/bin/python

# 一键启动全部 4 服务（先下游后 agent，自动健康检查 + 入库样本知识库），Ctrl-C 一并退出
bash scripts/run_all.sh

# 单独起某个服务（注意 agent 须最后起：它启动时会拉技能目录 + 发现 A2A）
$PY -m uvicorn a2a_service.main:app --port 8300
$PY -m uvicorn skillcenter.main:app --port 8200
$PY -m uvicorn arag.main:app        --port 8100
$PY -m uvicorn agent.main:app       --port 8000

# 入库样本知识库（知识问答前必须执行一次；本地存储为内存态，每次重启 arag 后都要重跑）
curl -X POST http://127.0.0.1:8100/v1/index/sample

# 重建虚拟环境（仅当 env_sxw_demo/ 不存在）
python3.12 -m venv env_sxw_demo && env_sxw_demo/bin/pip install -r requirements.txt
```

**本仓库没有单元测试**。`py_compile` 是约定的编译校验门（替代单测）：

```bash
find agent arag common skillcenter a2a_service -name '*.py' | xargs env_sxw_demo/bin/python -m py_compile
```

端到端评测（真实 LLM 黑盒、解析 SSE）走 `eval/` harness；它依赖**两个 agent 实例**（8000=agent_loop / 8001=plan_execute，因 `ENGINE` 是启动期配置不可单请求切换）：

```bash
export DASHSCOPE_API_KEY=sk-***   # 仅环境变量，切勿写入任何文件
$PY -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000 --out eval/reports/<ts>
$PY -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001 --out eval/reports/<ts>
$PY -m eval.harness.report --out eval/reports/<ts>      # 聚合出 summary.md
# 单 suite：--suite routing；arag-down 鲁棒性子集：先停 arag 再加 --only-arag-down
```

## 架构大局

### 服务拓扑与调用链
```
用户 → agent(:8000) ──┬─→ arag(:8100)         知识检索（混合召回 RAG）
                      ├─→ skill-center(:8200)  技能目录/执行 + A2A 注册表
                      └─→ a2a_service(:8300)   A2A 远程子代理（ADK to_a2a 暴露 math_expert）
```
**对外只需访问 agent**。下游不可用时一律 **best-effort 降级**（不阻断 agent 启动 / 不中断对话），对应能力静默跳过——排障时按 `[QaRetrieve] degraded`、`[SkillCatalog] ... skip`、`[A2ALoad] ... skip` 等日志定位。

### 两代推理引擎（核心抽象）
`agent/engine/base.py` 定义统一端口 `ReasoningEngine.run_stream(ctx) -> AsyncIterator[StreamEvent]`，由 `build_engine()` 按 `ENGINE` 配置选型：
- **`plan_execute`**（Gen1）：`decision_planner` 出计划 → `execution_planner` 逐步执行。前置规划、可控可解释。
- **`agent_loop`**（Gen2，默认）：ReAct 单循环，模型迭代调工具直到产出。带计划续推、force-summary、动态工具发现、子代理委派。

**两引擎共享**同一套工具/检索/技能/citation 下游、ToolErrorFeedback 插件、LiteLlm 加固；切换只改「如何编排」。**但工具面不对称**：`translate`/`text_stats`/`tool_search`/`update_task_plan`/`researcher` 是 `agent_loop` 专属（见 `agent/engine/agent_loop/`），`plan_execute` 只用 `ctx.tools`。改动涉及工具集时务必意识到这点（评测也只在共享子集上对比两引擎）。

### 生产级加固落在两个 ADK 扩展点（面试主线）
1. **ADK Plugin** `agent/plugins/agent_invocation_plugin.py`：`on_tool_error_callback` 把框架级工具异常封装成 `function_response` 喂回模型、**不中断 turn**；`before_model_callback` 做计划续推 / 消息预算 / force-summary。
2. **LiteLlm 子类** `agent/llm/hardened_litellm.py`：上下文超长截断重试、异常分类、PromptCache 缓存断点（provider-aware）。

> ⚠️ 诚实声明：PromptCache 显式缓存断点为 Anthropic 专属，本 demo 默认 provider（DashScope/Qwen）下为 no-op。

### 三种「扩展智能体」机制（不要混淆）
- **skill-center 技能**（`agent/skills/` → `skillcenter/`）：远程 MCP 风格执行网关；启动拉技能目录(快照)包装成工具；NDJSON `SkillResultDTO` 流经 UI 队列合并为 `skill_event`。
- **Codex-skill 沙箱**（`agent/claude_skill/`）：技能包 `SKILL.md` 作为**子代理在沙箱中执行**；沙箱 provider 抽象（`LocalSandbox` 可跑 / `AgentBay` 云桩）。纯本地能力，不依赖任何下游。
- **A2A 远程子代理**（`agent/a2a/loader.py` + `a2a_service/`）：ADK 原生 A2A，经 agent-card 发现 + JSON-RPC 委派；skill-center 作注册表。

### RAG（arag）
`query → rewrite → 向量(numpy 余弦)+全文(BM25/jieba) 双路 → RRF 互惠排名融合 → 低价值过滤`。入库链路 `parse → image caption(视觉) → chunk → embed → store`。存储是**端口-适配器**设计（`arag/store/`：VectorStore / FullTextIndex / GraphStore），当前仅 `local` 内存实现——**重启即清空**，所以每次重启 arag 都要重新 `POST /v1/index/sample`。

### 流式与可观测
统一 SSE 事件：`text · tool_call · tool_result · plan_step · citation · skill_event · done · error`。`common/obs.py` 提供 `trace_id` 全链路透传 + 结构化 JSON 日志；日志按 `[Tag]` 前缀检索（`[QaRetrieve]` `[SkillInvoke]` `[ClaudeSkill]` `[A2ALoad]` `[LoopControl]` …）。`common/skill_contract.py` 是技能线协议契约。

## 项目特定约定与坑

- **配置**：每个服务用 `pydantic-settings` 读取**同目录 `.env`**（字段名大写即环境变量名，真实环境变量优先）。改 `.env` 后须**重启对应服务**才生效。密钥 `DASHSCOPE_API_KEY` 切勿提交。
- **换模型/换厂商**只改 `LLM_MODEL` / `LLM_BASE_URL` / `DASHSCOPE_API_KEY`（内部用 `openai/<LLM_MODEL>` 走 litellm 的 openai 兼容 provider）。
- **`a2a-sdk` 必须 pin 0.3.x**（`>=0.3.4,<0.4`，与 google-adk 2.3.0 对齐）；**1.x 不兼容**。ADK 的 A2A 仍标 EXPERIMENTAL，导入有 `UserWarning` 属正常。
- **熔断**：`MAX_LOOP_ITERS`（默认 8）是 agent-loop 软收尾轮次；框架硬熔断 = 该值 + 2（`RunConfig.max_llm_calls`），plan_execute 执行相也用同值作硬熔断。
- **沙箱**：`SANDBOX_PROVIDER=local` 才真实可跑（子进程，**非生产隔离**）；`agentbay` 是云桩，调用即 `SandboxUnavailableError`。

## 如何新增能力

- **通用工具**：在 `agent/tools/builtin_tools.py` 写带类型注解 + docstring 的函数 → 加入 `build_builtin_tools()`（ADK 自动转 FunctionTool）。
- **skill-center 技能**：在 `skillcenter/skills.py` 的 `SKILL_DEFS` 加定义 + 在 `execute_sync`/`execute_streaming` 加分支。
- **Codex-skill（沙箱）**：在 `agent/claude_skill/skills_data/<id>/SKILL.md` 写 frontmatter(name/description) + 指令体，自动被加载。
- **A2A 子代理**：在 `a2a_service/agents.py` 定义 ADK `LlmAgent` 并 `to_a2a` 暴露；在 `skillcenter/a2a_api.py` 注册到 `/instance/list`。
