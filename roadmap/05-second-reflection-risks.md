# 05 · 二次反思：验证结论与风险

## 1. 能力验证结论（已用真实凭据实测，凭据未入库）

| # | 验证项 | 模型 / 端点 | 结果 | 对设计的影响 |
|---|---|---|---|---|
| V1 | 文本推理 | `qwen3.7-plus` @ compatible-mode | ✅ 200，返回 "pong" | 模型名真实可用；推理走它 |
| V2 | 多模态视觉 | `qwen3.7-plus` + `image_url` | ✅ 200，识别 "Dog" | **单模型覆盖文本+视觉**，无需独立 VL 模型，多模态配置简化 |
| V3 | 文本嵌入 | `text-embedding-v3` | ✅ 200，**1024 维** | RAG 嵌入用它，无需本地重模型 |
| V4 | Function-calling | `qwen3.7-plus` + `tools` | ✅ 返回 `tool_calls: get_weather({city:Hangzhou})` | **agent-loop 命脉成立**，ReAct 工具循环可行 |

> 验证方式：最小 `curl`（"ping" 级 prompt），API Key 仅以 shell 环境变量临时注入，未写入任何文件、用完 `unset`。

---

## 2. 风险与取舍（二次反思新增/确认）

### R1 · PromptCache 在 Qwen 上不生效 —— 已转为 provider-aware 设计
- **事实**：Anthropic `cache_control` 缓存断点是厂商专属；DashScope/Qwen OpenAI 兼容端点不支持显式断点。
- **决策**：PromptCache 实现为 provider 感知抽象——支持的 provider 才注入断点，Qwen 下 **no-op + 日志**。
- **诚实声明**：面试讲"做了缓存断点抽象"，并说明在当前 demo provider 上是 no-op，**不谎称已启用**。

### R2 · 公版 `google-adk` 的类路径可能与内网 fork 不同
- **事实**：原项目可能用阿里内网 ADK fork；demo 用公网 `google-adk`。
- **决策**：M0 第一件事——校验 `BasePlugin`/`BaseLlmRequestProcessor`/`LiteLlm`/`AgentTool`/`BaseSessionService`/`BaseArtifactService` 的公版可用性；如路径/签名不同，按公版 API 适配（demo 干净重写，不绑内网实现）。
- **兜底**：万一 `BaseLlmRequestProcessor` 在公版不可直接继承，agent-loop 改用「ADK Runner + 自管循环外壳」等价实现续推，不影响对外行为。

### R3 · litellm × DashScope 兼容性细节
- **事实**：litellm 走 `openai/` 前缀 + `api_base` 可接任意 OpenAI 兼容端点；流式/工具调用均支持。
- **决策**：`model="openai/qwen3.7-plus"`，`api_base`/`api_key` 由 env 提供；M0 冒烟确认流式 + 工具调用经 litellm 透传正常。

### R4 · 嵌入维度固定 1024（text-embedding-v3）
- **决策**：`LocalVectorStore` 维度从配置/首条向量推断，不硬编码；换嵌入模型只改 env。

### R5 · GraphStore 仅端口、不接检索流（按用户拍板）
- **决策**：定义 `GraphStore` ABC + `LocalGraphStore`（内存邻接表）极简实现，**不接入混合召回**，留 `# TODO: wire GraphRAG when Neo4j backend ready`。避免引入实体抽取/建图/图遍历的重逻辑。

### R6 · 检索降级语义
- **决策**：arag 超时/失败时，`knowledge_search` 返回结构化"无结果/降级"而非抛错；引擎据此走 chat-mode，**不中断会话**。对齐原项目 `No knowledge retrieved, falling back to chat mode`。

### R7 · 不写单测但要可验证
- **决策**：每里程碑 `py_compile` 全绿 + 关键里程碑真实 LLM 手动 E2E 冒烟（M0/M3/M4/M5/M6）。单测留到阶段二。

### R9 · Agent-Loop 加固落在 Plugin 而非自研 RequestProcessor（M3 架构决策）
- **背景**：原项目用 `_SingleLoopRequestProcessor(BaseLlmRequestProcessor)` 驱动续推；该类在 ADK 2.3.0 仅存于私有路径 `_base_llm_processor`，且要侵入 flow 才能注入。
- **决策**：改用 ADK 公版 **Plugin** 扩展点等价实现——`before_model_callback`（每轮模型调用前改 `llm_request`）承担"续推/消息预算/force-summary"，`on_tool_error_callback` 承担 ToolErrorFeedback；循环本体复用 ADK Runner 原生工具循环。语义等价、更稳、更地道。
- **影响**：`loop_processor.py` 由"RequestProcessor 子类"变为"被插件 `before_model_callback` 委托的 LoopController"；对外行为（plan 续推 / force-summary / 工具异常喂回）一致。

### R8 · qwen3.7-plus 是推理模型，会输出 thinking 文本（M0 实测发现）
- **事实**：M0 E2E 冒烟时模型在最终答案前输出了大段 "Thinking Process"；DashScope 推理模型把思考经 `reasoning_content` 返回，litellm/ADK 可能把它并进 text。
- **决策**：M2 在 `event_converters` 区分 `reasoning_content`（可选作 `thinking` 事件或丢弃）与正文 `content`，避免思考过程污染对外 SSE 文本；必要时在 litellm 调用侧关闭/收敛 thinking。

---

## 3. 安全与凭据纪律

- API Key **绝不入库**：仓库内一律 `sk-***` 占位；真实 key 运行时经 `DASHSCOPE_API_KEY` 注入。
- `.gitignore` 必含 `.env`；roadmap/代码/示例中不得出现真实 key。
- 端点 URL 与模型名（`qwen3.7-plus` / `text-embedding-v3`）非密，可写入文档与 `.env.example`。

---

## 4. 开放项（实现中遇到再定，不阻塞开工）

| # | 开放项 | 当前默认 | 触发重审条件 |
|---|---|---|---|
| O1 | rerank 用规则还是 rerank 模型 | 先 LLM/规则打分，预留模型接口 | 若 DashScope 有可用 rerank 模型且效果差距大 |
| O2 | sub-agent / tool-search 演示深度 | 各 1 个最小可跑示例 | 若想加面试亮点可扩展 |
| O3 | 样本知识库内容 | 内置一份小中文文档（含图） | 想贴近某面试场景可替换 |
| O4 | 前端 | 仅 `curl`/SSE 文本演示，不做 UI | 若要可视化再加极简页面 |

---

## 5. 二次反思结论

- 设计**无致命阻塞**：四项核心能力（推理/视觉/嵌入/工具调用）已实测通过，两代引擎与混合召回 RAG 均可在 qwen3.7-plus 上落地。
- 唯一需"诚实降级"的点是 **PromptCache（R1）**，已转为 provider-aware 抽象处理。
- 公版 ADK 类可用性（R2）是唯一需在 **M0 即时校验**的工程风险，已设兜底方案。
- **可以按 `04` 的 M0→M6 开工。**

---

## 6. 评审修复（R10，2026-06-28 M0–M6 完成后二次反思）

针对 `review/code-review-summary.md`，逐条核验后采纳 5 项修复：

| # | 评审问题 | 核验 | 修复 |
|---|---|---|---|
| R10.1 | Agent-Loop 缺硬上限 | 部分属实（`RunConfig.max_llm_calls` 默认 500，未对齐业务轮次） | `agent_loop_engine.py` 设 `max_llm_calls=max_loop_iters+2`：软 force-summary + 框架硬熔断两层 |
| R10.2 | citation 跨 chunk marker 漏识别 | 属实（逐 delta 扫描） | `citation_injector.py` 改为累积全文、`done` 时一次性扫描 |
| R10.3 | trace_id 未跨服务透传 | 属实 | `knowledge_search.py` httpx 带 `headers={"x-trace-id": get_trace_id()}` |
| R10.4 | roadmap 把 Agent-Loop 写成 BaseLlmRequestProcessor 驱动 | 属实（文档与实现不一致） | 统一口径"原生产 RequestProcessor / demo 用 Plugin.before_model"；并修正 03 中错误文件名 `tool_error_feedback.py`→`agent_invocation_plugin.py`、`tiktoken`→字符预算 |
| R10.5 | ToolErrorFeedback 缺自然触发路径 | 属实（工具自捕获） | 新增受控演示工具 `simulate_unstable_operation(should_fail)`，should_fail=True 抛未捕获异常触发 on_tool_error 喂回 |

"刻意裁剪项"评审判断正确——属范围选择而非缺陷；已在 `README.md` 增"范围与边界"段诚实声明。
