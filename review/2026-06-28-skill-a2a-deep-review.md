# sxw_optimization_demo 技能中心 / SKILL / A2A 深度评审

评审时间：2026-06-28 21:42 CST

评审范围：

- 新增 roadmap：`roadmap/06-skill-center-link.md`、`roadmap/07-skill-sandbox.md`、`roadmap/08-a2a.md`
- 运行手册：`RUNBOOK.md`
- demo 新增模块：`skillcenter/`、`agent/skills/`、`agent/claude_skill/`、`agent/a2a/`、`a2a_service/`、`common/skill_contract.py`
- 对照源码：
  - 原 skill-center：`/Users/shixiangweii/IdeaProjects/sxw_work/codes/2026_albert-skill-center_proj/albert-skill-center`
  - 原 agent-2 技能调用：`app/infra/boundary/albert_skill_center/*`、`app/lumi/tools/base_select_tool.py`
  - 原 agent-2 A2A/claude-skill：`app/core/agent/remote_agent/a2a/*`、`app/core/claude_skill/*`

## 总体结论

这次新增的三条链路整体方向是对的，且非常适合面试讲：

1. **skill-center 技能调用链路**：保留了“技能目录 -> MCP 风格 tools/schema -> ADK 工具包装 -> 构造执行上下文 -> execute-streaming -> NDJSON/SkillResultDTO 解析 -> skill_event 回流 -> 聚合结果回父 LLM”的主干。
2. **claude-skill 沙箱执行链路**：保留了“SKILL.md 技能包 -> provider 沙箱抽象 -> file/shell/code 工具集 -> 子 LlmAgent 在沙箱中执行 -> UI/LLM 双契约”的主干。
3. **A2A 远程子代理链路**：保留了“skill-center 注册表 -> agent-card -> RemoteA2aAgent -> AgentTool -> JSON-RPC 远程委派”的主干。

因此现在的 demo 已经从“Agent runtime + RAG”扩展成了更完整的“Agent runtime 编排多种下游能力”：知识检索、技能中心、沙箱技能、A2A 子代理。这个叙事很有含金量。

需要注意的是，它仍然是**生产主链路教学切片**，不是线上治理全集。快照一致性、MCP 真实握手、权限、计费扣减、A2A 出网管控、AgentBay 真实隔离、AGUI/browser/REAL/DQL 等复杂业务分支都被刻意裁剪。这个边界要继续在面试中讲清楚。

## P1 问题

### 1. LocalSandbox 路径逃逸检查可被同前缀 sibling 绕过

文件：`agent/claude_skill/sandbox/local_sandbox.py`

现状：

```python
resolved = (workdir / path).resolve()
if not str(resolved).startswith(str(workdir.resolve())):
    raise SandboxError(...)
```

问题：

- `startswith` 是字符串前缀判断，不是路径父子关系判断。
- 如果 workdir 是 `/tmp/sxw_sandbox_abcd`，绝对路径 `/tmp/sxw_sandbox_abcd_evil/leak.txt` 会通过检查。
- 我用本地探针复现：创建同前缀 sibling 目录后，`file_service().read(abs_path)` 能读到沙箱外文件。

影响：

- 虽然 `RUNBOOK` 已说明 LocalSandbox 非生产隔离，但代码仍声称“工作目录限制”。
- 这个问题面试时如果被问到沙箱安全边界，会比较危险。

建议：

- 用 `Path.relative_to` 或 `os.path.commonpath` 做父子路径校验。
- 同时明确 shell 本身也不是隔离环境，`run_shell` 仍可访问宿主系统；LocalSandbox 只能作为本地演示。

建议修法：

```python
root = workdir.resolve()
resolved = (root / path).resolve()
try:
    resolved.relative_to(root)
except ValueError:
    raise SandboxError(f"path escapes sandbox workdir: {path}")
```

面试说法：

> demo 的 LocalSandbox 用于展示 provider 抽象和工具协议，不声称生产隔离；生产要换 AgentBay 这类真实隔离沙箱。本地实现只做工作目录约束、超时和资源清理。

### 2. `skipSummarization` 只被返回给父 LLM，没有真正阻止父 LLM 总结

文件：`agent/skills/selected_skill_tool.py`、`agent/skills/result_parser.py`

现状：

- `skillcenter/skills.py` 的 `query_weather` 会产出 `CARD + skipSummarization=True`。
- `SelectedSkillTool.run_async` 会把 `skipSummarization` 放进 function response：

```python
out = {"skill": self._tool_name, "skipSummarization": skip_summarization}
```

问题：

- 目前没有引擎层逻辑消费这个字段。
- 卡片会通过 `skill_event` 直呈，但父 LLM 仍会收到工具结果并继续生成最终文本。
- roadmap 写的是“`skipSummarization=true` 直呈不总结”，当前实现更准确地说是“UI 已直呈，但没有硬禁止父 LLM 总结”。

影响：

- 对 demo 展示卡片影响不大，但和生产语义不完全一致。
- 如果用户问“为什么 skipSummarization 能跳过总结”，现在只能说是字段保留，未做强控制。

建议：

- 最小修复：当工具结果含 `skipSummarization=True` 时，插件或 event converter 可注入一个强提示，让父 LLM 不再复述卡片。
- 更强修复：在引擎层识别单技能直呈场景，直接收口，不再进入后续 summary。

面试说法：

> demo 保留了 skipSummarization 信号和 UI 直呈事件；线上完整语义还需要引擎层消费该信号，阻止父 LLM 二次总结。

### 3. skill-center 空流没有按 roadmap/生产逻辑兜底成错误帧

文件：`agent/skills/client.py`

现状：

- `execute_tool_by_sse` 逐行解析 NDJSON，遇到异常会 yield 错误帧。
- 但如果服务端返回 200 且流中没有任何有效行，函数会静默结束。
- 生产版 `AlbertSkillClient.execute_tool_by_sse` 有 `is_empty_line` 判断，空结果会 yield `SkillResultDTO(success=False, errorMsg="工具执行无结果")`。

影响：

- `SelectedSkillTool` 会把空聚合结果变成“技能执行完成。”，这会掩盖实际协议异常。
- roadmap 06 写了“空结果兜底”，当前没有完整实现。

建议：

- 在 `execute_tool_by_sse` 中维护 `seen_result = False`。
- 正常解析到任意有效 `SkillResultDTO` 后置 True。
- 流结束后仍为 False，则 yield `SkillResultDTO(success=False, errorMsg="工具执行无结果", eof=True, isPartial=False)`。

## P2 问题

### 4. skill 工具缺少生产里的参数 schema coercion

文件：`agent/skills/selected_skill_tool.py`

生产对照：

- 原 `app/lumi/tools/base_select_tool.py` 在执行前调用 `coerce_args_by_schema(args, self.tool_config.input_schema)`。
- `app/lumi/tools/args_coercion.py` 会修复 LLM 常见问题：把声明为 array/object 的参数错输出成 JSON 字符串。

现状：

- demo 把 `input_schema` 转成 Gemini schema 后只用于 tool declaration。
- 执行时直接把 `args` 发送给 skill-center。

影响：

- 当前两个演示技能只用 string 参数，问题不明显。
- 一旦新增 array/object 参数技能，模型输出如 `{"items": "[1,2,3]"}` 时，下游会收到字符串而非数组。
- 这类“工具调用边界的参数修复”很有生产工程含金量，值得保留。

建议：

- 保存 raw `input_schema`，在 `run_async` 中调用一个精简版 `coerce_args_by_schema`。
- 只支持 array/object JSON string 反序列化即可，不必复制全部生产逻辑。

面试说法：

> 生产里我会在工具执行边界做 schema-aware 参数修复，因为 LLM 经常把数组/对象参数输出成 JSON 字符串；这是把 MCP 工具跑稳的关键细节。

### 5. A2A 保留了协议调用，但没有保留生产级上下文/header 透传

文件：`agent/a2a/loader.py`、`a2a_service/*`

生产对照：

- 原 `StreamingRemoteA2aAgent` 会把 A2A execute context 放入 JSON-RPC metadata。
- 它还会透传 `X-Trace-Id`、`X-User-Payload`、Langfuse trace/span headers。
- 原 Java `A2AAgentRuntimeController` 会从 metadata 中 pop 出 `context`，映射成 `ExecuteContextVO`，再把简化后的 JSON-RPC body 转交 runtime。

现状：

- demo 使用公版 `RemoteA2aAgent(name, agent_card=cardUrl)`。
- 启动发现 `/instance/list` 时透传了 trace，但实际 RemoteA2aAgent 调 a2a_service 时没有注入 demo 的 `SkillToolExecuteContext`。
- `a2a_service` 是 `to_a2a` 生成的 ASGI app，也没有接入 `TraceMiddleware`。

影响：

- demo 可证明 agent-card 发现和 JSON-RPC 远程委派能跑通。
- 但不能证明生产里的“带用户/会话/agent 上下文的 A2A 透传”。

建议：

- 如果想增强面试亮点，可以使用 `RemoteA2aAgent` 的 `a2a_request_meta_provider` 参数，把 `SkillRequestContext` 映射成 metadata。
- 如需 trace 统一，可在 A2A client factory/http kwargs 层加 header，或写一个轻量 wrapper agent。

面试说法：

> demo 复刻的是 A2A 的发现和远程委派主链路；生产版还会把用户、会话、agent、trace 等上下文透传到 JSON-RPC metadata/header，用于权限、审计和链路追踪。

### 6. Plan-Execute 引擎没有接 `AgentInvocationPlugin`

文件：`agent/engine/plan_execute/execution_planner.py`

现状：

- Agent-Loop Runner 有 `plugins=[AgentInvocationPlugin(controller)]` 和 `max_llm_calls`。
- Plan-Execute Runner 只设置了 `RunConfig(streaming_mode=StreamingMode.SSE)`，没有 plugin。

影响：

- 常规技能工具大多 catch 异常并返回结构化错误，所以不一定暴露。
- 但新增的 `simulate_unstable_operation(should_fail=True)` 只会在 Agent-Loop 下验证 ToolErrorFeedback；Plan-Execute 下未捕获工具异常可能直接进入 `merge_runner_events` 的 error 事件。
- roadmap/RUNBOOK 说两代引擎共享工具链路，这没问题；但“工具异常喂回不中断”当前主要属于 Agent-Loop。

建议：

- 若希望两代引擎行为更一致，可给 Plan-Execute 的 Runner 也挂一个轻量 `AgentInvocationPlugin`。
- 或在文档中明确：Plan-Execute 是老范式演示，生产级循环加固重点在 Agent-Loop。

### 7. `SSEStreamProcessor.max_buffer_size` 参数未生效

文件：`agent/skills/stream_processor.py`

生产对照：

- 原 `SSEStreamProcessor` 会在 buffer 超过上限时强制处理，避免长时间无换行导致内存增长。

现状：

- demo 的 `max_buffer_size` 被保存，但 `process_chunk` 没有检查。

影响：

- 对演示技能无影响。
- 如果下游异常地长时间不换行，buffer 可能无限增长。

建议：

- 复刻生产版的简化保护：`if len(self.buffer) > self.max_buffer_size: force_process_buffer()`。

## P3 问题

### 8. request context / ui queue contextvar 没有 reset

文件：`agent/api/chat.py`、`agent/skills/request_context.py`、`agent/skills/ui_event_queue.py`

现状：

- 每个请求进入 generator 时调用 `set_request_context(...)`。
- `merge_runner_events` 调用 `set_ui_queue(queue)`。
- 没有保留 token，也没有在 finally 中 reset。

影响：

- ASGI 一般每个请求在独立 task 中执行，实际串号风险不高。
- 但从工程习惯看，contextvar 最好成对 set/reset，尤其是后续若引入后台任务或复用 task，会更稳。

建议：

- `set_request_context` / `set_ui_queue` 返回 token。
- 在 `finally` 中 reset。

### 9. `SkillResultDataType` 文档和实现口径略不一致

文件：`common/skill_contract.py`、`roadmap/06-skill-center-link.md`

现状：

- roadmap 06 的协议描述列了 `AGUI…`。
- demo enum 只保留 `TEXT/CARD/JSON/IMAGE/FILE/CHART`。
- roadmap 后文又说明 AGUI、browser-use、real 等已裁剪。

影响：

- 不影响代码运行。
- 但阅读文档时容易误以为 demo 支持 AGUI/REAL_META/WEB_PAGE。

建议：

- 在 roadmap 06 的协议表中标注“生产全集；demo enum 裁剪到 TEXT/CARD/JSON/IMAGE/FILE/CHART”。

## 保留得好的核心逻辑

### skill-center 链路

- `common/skill_contract.py` 用 camelCase alias 对齐真实 DTO。
- `skillcenter/api.py` 保留 `/list`、`/execute`、`/execute-streaming` 三类 runtime 入口。
- `skillcenter/skills.py` 保留 MCP 风格 `tools[] + inputSchema` 和同步 `content:[{type,text}]` 返回。
- `agent/skills/client.py` 保留 base64 `X-User-Payload`、`X-Trace-Id`、`X-Deap-AgentUUid` header。
- `agent/skills/stream_processor.py` 保留 NDJSON 字节流分行、跨 UTF-8 chunk 解码。
- `agent/skills/selected_skill_tool.py` 保留 `BaseTool._get_declaration`、执行上下文构造、流式解析、`skill_event` 回流、文本聚合回父 LLM。
- `agent/skills/stream_merge.py` 用队列合并 Runner 事件和技能 UI 事件，这个设计很好地体现了“工具执行中仍能实时输出 UI 帧”。

面试可讲：

> 技能中心不是普通 HTTP 工具调用，而是一个富协议：目录阶段给 LLM 暴露 MCP 风格 schema，执行阶段用 SkillResultDTO 流式返回 thinking/text/card/error/eof 等富事件，agent 同时维护“给用户看的流”和“给父 LLM 的 function_response”两份契约。

### claude-skill 链路

- `catalog.py` 加载 `SKILL.md` frontmatter + instruction。
- `sandbox/base.py` 抽象 provider 与 file/shell/code 三类服务。
- `local_sandbox.py` 能真实跑 Python/shell，适合演示。
- `agentbay_sandbox.py` 明确作为 provider 桩，边界诚实。
- `toolset.py` 把沙箱能力包装成 ADK tools。
- `skill_runner.py` 用子 `LlmAgent + InMemoryRunner` 执行技能，并把子代理 tool_call/tool_result/text 包成 `skill_event`。
- `claude_skill_tool.py` 保留父 LLM 契约 `{output: ...}` 和 UI 契约 `skill_event`。

面试可讲：

> claude-skill 的本质是“技能包驱动的沙箱子代理”：SKILL.md 决定子代理行为，沙箱工具集提供受控执行能力，父代理只看到一个工具，但 UI 可以实时看到子代理内部执行轨迹。

### A2A 链路

- `skillcenter/a2a_api.py` 保留 `/instance/list` 注册表。
- `a2a_service/main.py` 用 ADK `to_a2a` 暴露 agent-card + JSON-RPC runtime。
- `agent/a2a/loader.py` 发现 cardUrl 后构造 `RemoteA2aAgent` 并包成 `AgentTool`。
- A2A 作为工具注入两代引擎，无需改核心编排。

面试可讲：

> A2A 子代理和本地 sub-agent 的差异在于发现和调用边界：本地子代理是进程内 AgentTool，A2A 是 agent-card 描述能力，再通过 JSON-RPC 远程调用。主 Agent 统一把它们看成工具，从而实现本地/远程代理的一致编排。

## 已验证项

执行过的轻量验证：

1. 全量 `py_compile`：通过。
2. citation 拆 chunk 探针：`"答案 [" + "1] 内容"` 会在 `done` 前生成 `citation` 事件。
3. `SSEStreamProcessor` UTF-8 分块探针：中文字符跨 byte chunk 可正确还原完整 NDJSON 行。
4. LocalSandbox 基本能力：工作目录内读写正常，`../outside` 会被阻止。
5. LocalSandbox 同前缀 sibling 绕过探针：已复现可读到沙箱外文件，因此列为 P1。

## 建议修复优先级

1. 修 `LocalSandbox._safe_path`：用 `relative_to` 替换字符串 `startswith`。
2. 为 skill-center 空流补错误帧兜底。
3. 明确或实现 `skipSummarization` 的引擎层消费。
4. 给 `SelectedSkillTool` 加 schema-aware 参数 coercion。
5. 为 A2A 增加 metadata/header 上下文透传，或在文档中明确未复刻。
6. Plan-Execute 如需对齐 Agent-Loop，补 `AgentInvocationPlugin`。
7. `SSEStreamProcessor` 加 max buffer 保护。
8. contextvar set/reset 成对管理。

## 面试叙事建议

推荐表述：

> 我不是把所有内部基建搬出来，而是抽取了 Agent Runtime 最能体现工程能力的四类下游：RAG 检索、skill-center 技能、沙箱 SKILL、A2A 远程子代理。它们在主 Agent 中统一表现为工具，但背后分别对应 HTTP 检索、富事件技能协议、沙箱子代理、agent-card/JSON-RPC 远程代理。这个 demo 的价值是把生产系统的编排主链路和关键工程取舍复刻到一个可运行的小系统里。

避免过度表述：

- 不要说“完整复刻 skill-center 全功能”。应说“复刻 runtime 调用主链路和 SkillResultDTO 富信封”。
- 不要说“LocalSandbox 是安全沙箱”。应说“本地演示 provider，生产换 AgentBay”。
- 不要说“A2A 生产上下文完全透传”。应说“保留 agent-card 发现和 JSON-RPC 调用，生产上下文/header 透传可继续增强”。

---

## 修复记录（2026-06-29）

二次复核确认 9 个问题**全部存在**；7 项落代码修复，2 项为范围澄清（文档）。轻量探针验证（不写单测，与评审同口径）。

| # | 严重度 | 处置 | 改动文件 | 验证 |
|---|---|---|---|---|
| 1 | P1 | **代码修复**：`_safe_path` 用 `Path.relative_to` 取代字符串 `startswith` | `agent/claude_skill/sandbox/local_sandbox.py` | 探针：同前缀 sibling（`<workdir>_evil/leak.txt`）读取 → BLOCKED；`../outside` → BLOCKED；目录内读写正常 |
| 2 | P1 | **代码修复（软控制）**：`skipSummarization=True` 时 function_response 改为显式抑制提示，且不再把 card 回喂父 LLM（不依赖引擎层硬收口，诚实保留为软控制）| `agent/skills/selected_skill_tool.py` | 探针 CASE-B：返回抑制语 + `has_card_key=False` |
| 3 | P1 | **代码修复**：客户端 `seen_result` 跟踪，空流兜底 `errorMsg="工具执行无结果"`；`SelectedSkillTool` 增加错误帧上浮（不再伪装“技能执行完成”）| `agent/skills/client.py`、`agent/skills/selected_skill_tool.py` | 探针 CASE-A：空流 → `isError=True, content="工具执行无结果"` |
| 4 | P2 | **代码修复**：新增 `args_coercion.py`，执行前按 input_schema 把 array/object 的 JSON 字符串反序列化；保留 raw schema 并接入 `run_async` | `agent/skills/args_coercion.py`(新)、`agent/skills/selected_skill_tool.py` | 探针 CASE-D：`items:"[1,2,3]"` → `list`；malformed 保持字符串 |
| 5 | P2 | **文档澄清**：A2A JSON-RPC metadata/header 上下文透传为有意裁剪（公版 RemoteA2aAgent 仅复刻发现+委派），增强路径标注 `a2a_request_meta_provider` | `roadmap/08-a2a.md` | — |
| 6 | P2 | **代码修复**：`AgentInvocationPlugin` 的 controller 改可选（None→`before_model` no-op）；Plan-Execute 执行相挂同插件（ToolErrorFeedback 对齐）+ `max_llm_calls` 硬熔断 | `agent/plugins/agent_invocation_plugin.py`、`agent/engine/plan_execute/execution_planner.py` | py_compile 全绿；两代引擎现共享工具异常喂回 |
| 7 | P2 | **代码修复**：`process_chunk` 检查 `max_buffer_size`，超限无换行时强制 flush | `agent/skills/stream_processor.py` | 探针：40B 无换行（cap=16）→ 强制成 1 行、buffer 清空；正常换行路径不受影响 |
| 8 | P3 | **代码修复**：`set_request_context`/`set_ui_queue` 返回 Token，入口与 merge 处 `finally` 成对 reset | `agent/skills/request_context.py`、`agent/skills/ui_event_queue.py`、`agent/api/chat.py`、`agent/skills/stream_merge.py` | 探针：reset 后两个 contextvar 均回 None |
| 9 | P3 | **文档澄清**：`SkillResultDataType` 标注“生产全集含 AGUI/REAL/WEB_PAGE…；demo enum 裁剪到 TEXT/CARD/JSON/IMAGE/FILE/CHART” | `roadmap/06-skill-center-link.md` | — |

**整体校验**：`compileall agent arag skillcenter a2a_service common` 全绿；上述探针全部通过。
