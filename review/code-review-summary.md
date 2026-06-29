# sxw_optimization_demo 代码评审问题汇总

评审日期：2026-06-28

## 总体结论

当前精简结果符合预期定位：它不是把线上生产系统逐行缩小，而是抽取了最适合面试讲清楚的主链路，包括：

- Agent runtime 双引擎：Plan-Execute 与 Agent-Loop
- ADK Runner / Plugin / LiteLlm 扩展点
- SSE 流式输出、session、artifact、多模态输入
- agent -> arag 的 HTTP 微服务边界
- RAG 的 query rewrite、向量 + BM25 混合召回、RRF 融合、低价值过滤
- knowledge_search 超时降级、citation happy path、多模态图片 caption 入库

因此面试叙事建议定性为：

> 我基于生产系统抽取并复刻了一条可独立运行的核心 Agent Runtime + RAG 主链路，用来展示生产级系统的关键架构原语；内部基建、灰度、A2A、SLS、复杂 citation/placeholder、匿名问答等线上治理逻辑是有意识裁剪。

不要表述为“完整保留所有生产级处理逻辑”。更准确的是“保留主链路形状和关键工程取舍”。

## P1 问题

### 1. Agent-Loop 最大轮次不是硬上限

现状：

- `agent/engine/agent_loop/loop_processor.py` 在达到 `max_iters` 后只是追加 force-summary 提示。
- `agent/engine/agent_loop/agent_loop_engine.py` 创建 `RunConfig` 时只设置了 `streaming_mode=StreamingMode.SSE`，没有接 ADK 的 `max_llm_calls`。

影响：

- 当前“避免无限循环”依赖模型遵循提示，是软约束。
- 面试中如果讲成“生产级硬熔断”，会被追问时露出破绽。

建议：

- 在 `RunConfig` 中设置 `max_llm_calls=rc.settings.max_loop_iters` 或设置一个略高于业务轮次的硬上限。
- 保留 force-summary 作为软收尾提示，两者组合：硬上限负责安全，force-summary 负责体验。

面试说法：

> 这里我做了两层循环控制：业务层 force-summary 让模型主动收尾，框架层可以通过 ADK `max_llm_calls` 做硬上限，避免工具循环失控。

### 2. Citation 不支持跨 chunk marker

现状：

- `agent/citation/citation_injector.py` 的 `with_citations` 对每个 `text.delta` 单独正则扫描 `[n]`。
- 本地探针验证：如果流式输出被拆成 `"答案 ["` + `"1] 内容"`，不会产生 `citation` 事件。
- 原生产版 `CitationInjector` 有 `feed/flush`、unfinished buffer、标题识别、display id remap 等流式处理能力。

影响：

- demo 的 citation 能覆盖 happy path，但没有保留生产流处理的完整鲁棒性。
- 如果线上式流式 chunk 边界不稳定，引用事件可能丢失。

建议：

- 给 demo 版 `CitationInjector` 增加最小缓冲：保留末尾未闭合的 `[` 片段，下个 chunk 拼接后再扫描。
- 或在文档中明确标注“citation 为 ID 协议精简版，仅覆盖常规 `[n]` marker 演示”。

面试说法：

> demo 中我保留了 ID citation 的核心思想：LLM 只输出 `[n]`，文档标题和来源由程序生成。线上完整实现还会处理跨 chunk、标题吞吐、多语言标题、历史引用清洗等复杂流式边界。

## P2 问题

### 3. trace_id 尚未真正全链路透传

现状：

- `common/obs.py` 的 `TraceMiddleware` 能为单个服务生成或读取 `x-trace-id`。
- `agent/tools/knowledge_search.py` 调 arag `/v1/retrieve` 时没有透传 `x-trace-id` header。

影响：

- agent 和 arag 各自有 trace，但跨服务日志不能用同一个 trace_id 串起来。
- README 中“trace_id 全链路”表述偏满。

建议：

- 在 `knowledge_search` 调 httpx 时带上 `headers={"x-trace-id": get_trace_id()}`。
- arag 侧已具备读取该 header 的能力，无需大改。

面试说法：

> demo 已经有结构化日志和 trace middleware，跨服务只需要在 agent 调 arag 时把 `x-trace-id` 透传过去，就能形成完整链路追踪。

### 4. Roadmap 对 Agent-Loop 扩展点的表述不一致

现状：

- `roadmap/03-engine-deep-dive.md` 和部分总览仍写 Agent-Loop 由 `BaseLlmRequestProcessor` 驱动。
- `roadmap/05-second-reflection-risks.md` 又说明实际因公版 ADK 2.3 适配，改用 Plugin 的 `before_model_callback` 承接续推、预算、force-summary。

影响：

- 面试前自我复盘时容易混乱。
- 如果面试官看代码，会发现文档与实现口径不完全一致。

建议：

- 统一文档口径：原项目是 `BaseLlmRequestProcessor`，demo 因公版 ADK API 改用 `BasePlugin.before_model_callback` 实现同等语义。
- `03-engine-deep-dive.md` 中“扩展点 B”可以改成“原生产扩展点 / demo 适配落点”。

面试说法：

> 原项目是在 RequestProcessor 层注入每轮模型请求前的上下文；demo 使用公版 ADK 2.3，为了降低对内部 flow 的侵入，改用 Plugin 的 before_model hook 承接同样的续推和预算逻辑。

## P3 问题

### 5. ToolErrorFeedback 已接入框架，但缺少自然触发路径

现状：

- `agent/plugins/agent_invocation_plugin.py` 的 `on_tool_error_callback` 签名与 ADK 2.3 `BasePlugin` 对齐，框架接入是成立的。
- 但 demo 里的主要工具大多内部 catch 异常并返回结构化错误：
  - `calculator` catch 后返回 `{"error": ...}`
  - `knowledge_search` catch 后返回降级结果

影响：

- Plugin 能力存在，但常规 demo 请求不容易触发 `on_tool_error_callback`。
- 面试演示时如果只跑 calculator/knowledge_search，未必能证明“工具异常喂回不中断 turn”。

建议：

- 增加一个仅用于演示或测试的 `unstable_tool`，收到特定参数时抛异常。
- 或补一个最小测试，直接调用 plugin 的 `on_tool_error_callback`，验证返回错误喂回结构。

面试说法：

> 我把工具失败分成两类：业务可预期失败由工具返回结构化错误，框架级异常由 Plugin 捕获并作为 function_response 喂回模型，避免整个 turn 中断。

## 刻意裁剪项

以下不是当前 demo 的问题，而是范围选择。面试时建议主动说明，避免被理解成遗漏。

### 1. 内部基建裁剪

已裁剪：

- Nacos 灰度
- A2A 信封
- HSF 用户体系
- SLS / langfuse / rocketmq
- MaaS 内部网关
- 计费、限流、租户权限等治理逻辑

合理性：

- 这些是公司内部生产治理能力，不是 AI Agent 开发岗位面试最核心的展示面。
- demo 用 `.env`、FastAPI、结构化 JSON 日志、HTTP 边界替代，足够讲清架构思想。

### 2. 复杂 citation / placeholder 裁剪

已裁剪：

- 老路径 `ReferenceRewriter`
- 生产版 `CitationInjector` 的完整 feed/flush 状态机
- 多语言 citation title whitelist
- 历史引用清洗
- 图片 `__IMG__...` placeholder 流式替换
- LLM 前缀幻觉修复

合理性：

- demo 保留了 ID citation 的核心思想：LLM 输出 `[n]`，程序生成引用来源。
- placeholder 是线上知识库图片展示的复杂治理问题，和“Agent runtime + RAG 主链路”不是同一层级。

### 3. 匿名问答、群聊、客户端差异链路裁剪

已裁剪：

- 匿名游客问答 v2
- 群聊 @ / 群感知入口差异
- Java 侧字段透传
- deviceType、dialogType、openConversationId 等 A2A 上下文字段

合理性：

- 这些属于钉钉业务形态和生产入口治理。
- demo 的单入口 `POST /api/v1/chat/{agent_uuid}/stream` 更适合面试演示端到端流程。

## 建议修复优先级

1. 补 Agent-Loop 硬上限：接 `RunConfig(max_llm_calls=...)`。
2. 补 citation 最小跨 chunk buffer，至少覆盖 `[1]` 被拆开的情况。
3. 补 `x-trace-id` 跨服务透传。
4. 统一 roadmap 对 RequestProcessor / Plugin 的表述。
5. 增加一个 ToolErrorFeedback 演示工具或测试。

## 已验证项

- 使用本地 `env_sxw_demo` 执行 `py_compile`，66 个 Python 文件编译通过。
- introspection 验证 `AgentInvocationPlugin` 的 ADK Plugin 钩子签名与 `google-adk==2.3.0` 对齐。
- 本地 citation 探针确认了跨 chunk `[n]` 当前不会生成 citation 事件，属于真实行为缺口。
