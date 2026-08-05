# 运行手册（RUNBOOK）

> 面向新手：照本手册可在本机快速启动全部服务并跑通各项能力；含全部环境变量、功能特性开关与开发指南。
> 项目定位与能力总览见 [`README.md`](README.md)，Agent 开发约定见 [`AGENTS.md`](AGENTS.md)，评测方案见 [`eval/README.md`](eval/README.md)。

---

## 1. 项目定位、运行边界与服务端口

这是从公司生产项目核心链路中抽取、简化的个人学习与面试项目，重点验证和展示 Agent 运行时推理引擎、混合召回 RAG、技能/A2A 扩展以及生产级容错与可观测。它是可独立运行的工程样板，不是线上系统，也不承担历史兼容、存量技术债兼容或灰度发布要求。

开发时可以优先采用当前更先进、更清晰的方案，并直接调整旧接口或过时实现；但交付结果仍须满足以下底线：

- 四服务主链路可以按本文档启动，核心能力有与风险相称的验证。
- 下游故障遵循 best-effort 降级，不因可选能力不可用而中断基本对话。
- 配置、README、RUNBOOK、Agent 开发约定和评测用例与代码保持同步。
- 密钥不得进入文件或 Git；演示桩、provider 限制和非生产隔离能力必须明确说明。

系统由 **4 个 Python 服务**组成（均 FastAPI/uvicorn，基于 **Google ADK 2.3**）：

| 服务 | 默认端口 | 作用 | 启动模块 |
|---|---|---|---|
| **agent** | 8000 | Agent 运行时（两代引擎 / 工具 / SSE 入口）。**对外只需访问它** | `agent.main:app` |
| **arag** | 8100 | 混合召回 RAG（知识检索） | `arag.main:app` |
| **skill-center** | 8200 | 技能中心（MCP 风格技能执行 + A2A 注册表） | `skillcenter.main:app` |
| **a2a_service** | 8300 | A2A 远程子代理运行时（ADK `to_a2a` 暴露 math_expert） | `a2a_service.main:app` |

调用链：`用户 → agent(:8000)`；agent 按需 `→ arag(:8100)`（知识）/ `→ skill-center(:8200)`（技能、A2A 发现）/ `→ a2a_service(:8300)`（A2A 委派）。
下游不可用时 **best-effort 跳过**（不阻断 agent 启动），对应能力降级。

---

## 2. 前置条件

- macOS / Linux，**Python 3.12**（已用 3.12.10 验证）。
- 一个 **OpenAI 兼容**的大模型 API Key（默认走阿里云 **DashScope**，模型 `qwen3.7-plus` + 嵌入 `text-embedding-v3`）。
- 已存在虚拟环境 `.venv/`（兼容旧路径 `env_sxw_demo/`；若没有，见下方"重建虚拟环境"）。

### 重建虚拟环境（仅当 `.venv/` 与 `env_sxw_demo/` 都不存在）
```bash
cd sxw_agent-2_demo
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 网络受限可加清华镜像：
# .venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
```
> `requirements.txt` 已 pin `a2a-sdk>=0.3.4,<0.4`（与 `google-adk 2.3.0` 对齐；**勿装 a2a-sdk 1.x，不兼容**）。

---

## 3. 快速开始（一键启动）

```bash
cd sxw_agent-2_demo

# 1) 配置：从样例复制 .env，填入你的真实 DASHSCOPE_API_KEY（其余可保持默认）
cp .env.example .env
#   编辑 .env，把 DASHSCOPE_API_KEY=sk-*** 改成真实 key

# 2) 一键启动 a2a_service + skill-center + arag + agent（自动等待健康检查 + 入库样本知识库）
bash scripts/run_all.sh
```
看到 `[run_all] seeded` 与 4 行 `... ready` 即全部就绪。**Ctrl-C 一并退出**。

浏览器界面：`http://127.0.0.1:8000/chat-ui/`。

另开终端验证：
```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=什么是混合召回？' -F user_id=u1 -F session_id=s1
```

---

## 4. 单独启动各服务（开发调试用）

`run_all.sh` 之外，也可分别起（注意：`agent` 启动时会去拉 skill-center 技能目录、发现 A2A，故**先起下游再起 agent**）。
所有服务从同目录的 `.env` 读配置；`DASHSCOPE_API_KEY` 也可用环境变量覆盖。手动运行时先按规则选择解释器：若 `env_sxw_demo/bin/python` 存在则优先使用，否则使用 `.venv/bin/python`。

```bash
cd sxw_agent-2_demo
if [ -x env_sxw_demo/bin/python ]; then
  PY=env_sxw_demo/bin/python
else
  PY=.venv/bin/python
fi

# 下游
$PY -m uvicorn a2a_service.main:app   --port 8300   # A2A（需 key）
$PY -m uvicorn skillcenter.main:app   --port 8200   # 技能中心
$PY -m uvicorn arag.main:app          --port 8100   # RAG（需 key）

# 入口（最后起）
$PY -m uvicorn agent.main:app         --port 8000   # Agent 运行时（需 key）

# 入库样本知识库（首次运行或清空 local_storage/embedding 后执行）
curl -X POST http://127.0.0.1:8100/v1/index/sample
```
健康检查：`GET /healthz`（agent/arag/skill-center）；a2a_service 用 `GET /.well-known/agent-card.json`。

---

## 5. 环境变量全表

> 机制：每个服务用 `pydantic-settings` 读取**同目录 `.env`**（字段名大写即环境变量名）；真实环境变量优先级高于 `.env` 文件。
> **密钥 `DASHSCOPE_API_KEY` 切勿提交**（`.gitignore` 已忽略 `.env`）。

### 5.1 公共 / LLM（agent · arag · a2a_service 都用）
| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | `sk-***` | **必填**：大模型 API Key |
| `LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点（换厂商改这里） |
| `LLM_MODEL` | `qwen3.7-plus` | 推理 + 视觉模型 |
| `EMBEDDING_MODEL` | `text-embedding-v3` | 嵌入模型（agent/arag） |
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR） |

### 5.2 agent（:8000）
| 变量 | 默认 | 说明 |
|---|---|---|
| `ENGINE` | `agent_loop` | **推理引擎**：`agent_loop`（ReAct 单循环）\| `plan_execute`（先规划后执行） |
| `MAX_LOOP_ITERS` | `8` | agent-loop 软收尾轮次（硬熔断 = 该值 + 2；同值 +2 也作 plan_execute 执行相硬熔断） |
| `AGENT_PORT` | `8000` | 端口 |
| `AGENT_UUID` | `demo-agent` | 智能体标识（技能/A2A 调用上下文 tenantId） |
| `ARAG_BASE_URL` | `http://127.0.0.1:8100` | arag 地址 |
| `ARAG_TIMEOUT_MS` | `8000` | 检索超时（超时→降级 chat-mode） |
| `SKILL_CENTER_BASE_URL` | `http://127.0.0.1:8200` | skill-center 地址（技能目录 + A2A 注册表） |
| `SKILL_CENTER_TIMEOUT_MS` | `8000` | 技能同步/目录超时 |
| `SKILL_CENTER_STREAM_TIMEOUT_MS` | `60000` | 技能流式执行超时 |
| `SANDBOX_PROVIDER` | `local` | **SKILL 沙箱**：`local`（可跑）\| `agentbay`（云桩，不可跑） |

### 5.3 arag（:8100）
| 变量 | 默认 | 说明 |
|---|---|---|
| `ARAG_PORT` | `8100` | 端口 |
| `VECTOR_BACKEND` | `local` | 向量库：`local`(numpy 余弦 + 本地持久化) \| `pgvector`…（仅 local 已实现） |
| `EMBEDDING_STORAGE_DIR` | `local_storage/embedding` | local 向量与 chunk 元数据目录（`manifest.json`/`chunks.json`/`vectors.npy`） |
| `FULLTEXT_BACKEND` | `local` | 全文：`local`(BM25+jieba) \| `es`…（仅 local 已实现） |
| `GRAPH_BACKEND` | `local` | 图库：`local`(内存) \| `neo4j`…（**仅端口占位，未接检索流**） |

### 5.4 skill-center（:8200）
| 变量 | 默认 | 说明 |
|---|---|---|
| `SKILL_CENTER_PORT` | `8200` | 端口 |
| `A2A_SERVICE_BASE_URL` | `http://127.0.0.1:8300` | A2A 运行时地址（注册表 `/instance/list` 指向其 agent-card） |

### 5.5 a2a_service（:8300）
| 变量 | 默认 | 说明 |
|---|---|---|
| `A2A_SERVICE_HOST` | `127.0.0.1` | 主机（写进 agent-card 的 url） |
| `A2A_SERVICE_PORT` | `8300` | 端口（写进 agent-card 的 url） |

---

## 6. 功能特性配置（怎么按需开关）

改 `.env` 后**重启对应服务**生效。

### 6.1 走哪种推理引擎 —— `ENGINE`
- `ENGINE=agent_loop`（默认）：**ReAct 单循环**，模型迭代调工具直到产出；带计划续推、工具异常喂回、force-summary、硬熔断、子代理/动态工具发现。
- `ENGINE=plan_execute`：**先规划后执行**，decision planner 出计划 → execution planner 逐步执行；执行相同样带**工具异常喂回（ToolErrorFeedback）+ 框架硬熔断**，但**无**计划续推 / force-summary（那是 agent-loop 专属）。
```bash
PY=env_sxw_demo/bin/python
[ -x "$PY" ] || PY=.venv/bin/python
ENGINE=plan_execute "$PY" -m uvicorn agent.main:app --port 8000
```
> 两引擎共享同一套工具/检索/技能/citation 下游，以及 ToolErrorFeedback 插件与 LiteLlm 加固；切换只影响"如何编排"。

### 6.2 模型与 google-adk 接入 —— `LLM_*` / `DASHSCOPE_API_KEY`
- 运行时基于 **Google ADK**（`Runner` + `LlmAgent`）；模型经 ADK **LiteLlm** 适配 → 任意 **OpenAI 兼容**端点。
- 默认走 DashScope `qwen3.7-plus`。**换模型/换厂商**只改三项：`LLM_MODEL` / `LLM_BASE_URL` / `DASHSCOPE_API_KEY`（agent 内部用 `openai/<LLM_MODEL>` 让 litellm 走 openai 兼容 provider）。
- 推理模型的"思考过程"已通过 `extra_body={"enable_thinking": false}` 关闭，避免污染流式输出（代码内置，无需配）。
- 模型返回顶层数组、标量或损坏的工具参数时，LiteLlm 适配层会转换为安全 sentinel，Plugin 在真实工具分发前返回 `ToolArgumentsParseError`；正常对象参数仍走 ADK 原路径。该适配依赖精确锁定的 `google-adk==2.3.0`。

### 6.3 SKILL 沙箱 provider —— `SANDBOX_PROVIDER`
- `SANDBOX_PROVIDER=local`（默认）：本地沙箱，真实跑 `run_python`/`run_shell`（子进程 + 超时 + 工作目录限制）。
- `SANDBOX_PROVIDER=agentbay`：AgentBay 云沙箱**桩**，调用即返回 `SandboxUnavailableError`（演示 provider 抽象；生产换 wuying-agentbay-sdk）。
> ⚠️ LocalSandbox 仅演示用，**非生产隔离**。

### 6.4 RAG 存储后端 —— `VECTOR_BACKEND` / `EMBEDDING_STORAGE_DIR` / `FULLTEXT_BACKEND` / `GRAPH_BACKEND`
- 端口-适配器设计；当前仅 `local` 实现。向量与 chunk 元数据持久化在 `EMBEDDING_STORAGE_DIR`（默认 `local_storage/embedding`），arag 重启后会自动加载并用 chunks 重建 BM25；`pgvector`/`es`/`neo4j` 为预留端口（生产可零改业务接入）。
- `local_storage/` 是本地运行态数据目录，已被 `.gitignore` 忽略。重复入库同一 `chunk_id` 会覆盖旧 chunk 和向量，避免重复 seed 导致索引膨胀。

### 6.5 agent-loop 熔断 —— `MAX_LOOP_ITERS`
- 软收尾在第 `MAX_LOOP_ITERS` 轮注入 force-summary；框架级硬熔断 = `MAX_LOOP_ITERS + 2`（`RunConfig.max_llm_calls`）。调小可观察熔断行为。
- `plan_execute` 的执行相同样以 `MAX_LOOP_ITERS + 2` 作框架硬熔断（无 force-summary 软收尾）。

### 6.6 只跑部分能力（按需省服务）
- 不起 `arag` → 知识问答自动降级纯对话（`[QaRetrieve] degraded`）。
- 不起 `skill-center` → skill-center 技能 + A2A 发现跳过（`[SkillCatalog]/[A2ALoad] load failed, skip`）。
- 不起 `a2a_service` → A2A 工具在调用时报错喂回（不影响其它）。
- SKILL 沙箱是**本地**能力，不依赖任何下游（代码目录为 `agent/claude_skill/`）。

### 6.7 技能流与 A2A 调用约束
- skill-center 的 NDJSON 流必须以若干 `eof=false` 数据帧加一个 `eof=true, data=null` 独立结束帧收口；缺失 EOF、损坏帧或 `data+eof` 合帧会返回结构化协议错误，部分文本/卡片不会覆盖终止错误。
- 技能错误采用 failure-sticky：首个失败帧的 `errorCode/errorMsg` 保留到模型可见的 function response，后续成功或失败帧不覆盖。框架错误码包括 `SKILL_HTTP_ERROR`、`SKILL_TRANSPORT_ERROR`、`SKILL_STREAM_EMPTY`、`SKILL_STREAM_INCOMPLETE`、`SKILL_PROTOCOL_ERROR`；上游失败未给错误码时使用 `SKILL_EXECUTION_ERROR`，相同错误码写入 `[SkillInvoke]` 结构化日志。
- Claude SKILL 使用独立 `InMemoryRunner`，其沙箱工具也启用轻量 ToolArgsGuard。
- A2A `AgentTool` 每次调用都会为远端创建新会话，不继承当前父对话；委派 request 必须展开“它/上面的内容”等指代，并包含目标、范围、约束、输入和必要上下文。

---

## 7. 调用示例（各能力一条）

入口统一：`POST /api/v1/chat/{agent_uuid}/stream`，**multipart 表单**字段：`query`(必填) / `user_id` / `session_id` / `image`(可选图片文件)。返回 **SSE 流**。

```bash
A=http://127.0.0.1:8000/api/v1/chat/demo/stream

# 知识问答（带 [n] 引用；需先 /v1/index/sample）
curl -N -X POST $A -F 'query=什么是混合召回？RRF 是什么？' -F user_id=u1 -F session_id=s1

# 通用工具 + 多步（计算器）
curl -N -X POST $A -F 'query=用工具计算 (3+4)*5，再把结果翻译成英文' -F user_id=u1 -F session_id=s1

# 多模态（带图）
curl -N -X POST $A -F 'query=这张图里有什么？' -F user_id=u1 -F session_id=s1 -F 'image=@/path/to/pic.jpg'

# skill-center 技能（流式 skill_event：思考帧/卡片）
curl -N -X POST $A -F 'query=用天气卡片技能 query_weather 查询杭州天气' -F user_id=u1 -F session_id=s1

# SKILL 沙箱执行（子代理在沙箱跑 numpy）
curl -N -X POST $A -F 'query=用数据分析技能算 12,7,9,20 的均值和方差' -F user_id=u1 -F session_id=s1

# A2A 远程子代理（agent-card 发现 + JSON-RPC 委派）
curl -N -X POST $A -F 'query=用A2A数学专家精确计算 23*47' -F user_id=u1 -F session_id=s1
```

### SSE 事件类型
`text`(增量正文) · `tool_call` · `tool_result` · `plan_step`(计划步骤) · `citation`(引用块) · `skill_event`(技能/沙箱子代理流式帧) · `done` · `error`。

---

## 8. 端点速查

| 服务 | 方法 路径 | 说明 |
|---|---|---|
| agent | `POST /api/v1/chat/{agent_uuid}/stream` | SSE 对话入口 |
| agent | `GET /chat-ui/` | 浏览器 Web Chat 界面 |
| agent | `POST /api/v1/documents/index` | 文档入库代理（Web UI → agent → arag） |
| agent | `GET /healthz` | 存活 + 当前 engine/model |
| arag | `POST /v1/index/sample` · `POST /v1/index` | 入库样本 / 自定义文档 |
| arag | `POST /v1/retrieve` · `POST /v1/rag` | 混合召回 / 端到端问答 |
| skill-center | `POST /api/v1/skills/runtime/list` | 技能目录（snapshotTag） |
| skill-center | `POST /api/v1/skills/runtime/execute` · `/execute-streaming` | 同步 / NDJSON 流式执行 |
| skill-center | `POST /api/v1/a2a-agents/instance/list` | A2A 子代理注册表 |
| a2a_service | `GET /.well-known/agent-card.json` + JSON-RPC | A2A agent-card 发现 + message/send·stream |

---

## 9. 开发指南

### 目录速览
```
agent/      引擎(engine/{plan_execute,agent_loop}) · 插件 · LLM · 工具(tools) · 引用(citation)
            技能链路(skills→skill-center) · 沙箱技能(claude_skill) · A2A(a2a) · 会话/多模态/可观测
arag/       RAG：components/* + processor/* + store/*（存储端口）
skillcenter/ 技能中心 + A2A 注册表
a2a_service/ A2A 运行时（ADK to_a2a）
common/     obs.py(trace+JSON日志) · skill_contract.py(技能线协议)
eval/       真实 LLM 黑盒评测、数据集、评分器与历史报告
web/        内置浏览器 Chat UI（静态资源，无构建步骤）
```

### 常用开发动作
- **编译校验（必做，替代单测）**：
  ```bash
  PY=env_sxw_demo/bin/python
  [ -x "$PY" ] || PY=.venv/bin/python
  find agent arag common skillcenter a2a_service -name '*.py' | xargs "$PY" -m py_compile
  ```
- **加一个通用工具**：在 `agent/tools/builtin_tools.py` 写带类型注解 + docstring 的函数 → 加入 `build_builtin_tools()`（ADK 自动转 FunctionTool）。
- **加一个 skill-center 技能**：在 `skillcenter/skills.py` 的 `SKILL_DEFS` 加定义 + 在 `execute_sync`/`execute_streaming` 加分支。
- **加一个 SKILL 沙箱技能**：在 `agent/claude_skill/skills_data/<id>/SKILL.md` 写 frontmatter(name/description) + 指令体，自动被加载。
- **加一个 A2A 子代理**：在 `a2a_service/agents.py` 定义 ADK `LlmAgent` 并 `to_a2a` 暴露；在 `skillcenter/a2a_api.py` 注册到 `/instance/list`。
- **可观测性**：日志为结构化 JSON，含 `trace_id`（跨服务透传）；按 `[Tag]` 前缀检索，如 `[QaRetrieve]` `[SkillInvoke]` `[ClaudeSkill]` `[A2ALoad]` `[LoopControl]`。

---

## 10. 故障排查

| 现象 | 排查 |
|---|---|
| 启动报缺 `.env` / key | `cp .env.example .env` 并填 `DASHSCOPE_API_KEY` |
| 调用返回 `error` / 模型不应答 | 确认 key 有效、`LLM_MODEL`/`LLM_BASE_URL` 正确；看 agent 日志 |
| 知识问答无引用/答不上 | 首次运行或清空 `local_storage/embedding` 后先 `curl -X POST :8100/v1/index/sample` 入库；arag 未起则降级（日志 `[QaRetrieve] degraded`） |
| 端口被占用 | 改 `.env` 对应 `*_PORT`，或 `lsof -i:8000` 杀进程 |
| `a2a` 相关导入/调用失败 | 确认 `a2a-sdk` 为 0.3.x（`pip show a2a-sdk`）；**1.x 与 adk 2.3.0 不兼容** |
| 用 `agentbay` 沙箱报错 | 预期行为（云桩未实现）；用 `SANDBOX_PROVIDER=local` |
| A2A 调用报错 | 确认 `a2a_service`(:8300) 已起且 `/.well-known/agent-card.json` 可访问 |
| 改了 `.env` 不生效 | 重启对应服务（配置在启动时读取） |

> ADK 的 A2A 仍标注 EXPERIMENTAL，导入时有 `UserWarning` 属正常。
