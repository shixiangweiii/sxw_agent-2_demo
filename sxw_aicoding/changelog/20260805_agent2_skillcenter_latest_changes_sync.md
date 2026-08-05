# agent-2 / skill-center 最新变更同步记录

- 生成时间：2026-08-05
- 目标项目：`sxw_agent-2_demo`
- 参考项目：`albert-agent-2`、`albert-skill-center`

## 1. 变更背景

本项目从公司生产项目的核心链路中抽取、简化而来，用于个人学习、技术方案验证和面试准备。项目不承担线上流量，也不要求兼容历史接口和存量技术债，因此本次工作的目标不是机械追平生产源码，而是从参考项目的最新提交中筛选能够增强 Agent Runtime、技能/A2A 和可靠性主线的设计，再以适合当前四服务架构的方式反向同步。

本次梳理时确认的参考仓库最新提交基线如下：

| 参考项目 | 截至日期 | 最新提交 |
|---|---|---|
| `albert-agent-2` | 2026-08-05 | `4f22ff66`（提交日期 2026-08-04） |
| `albert-skill-center` | 2026-08-05 | `5f66a207`（提交日期 2026-08-04） |

重点分析了以下生产变更：

- agent-2 `fb9a7174e` 至 `d12e0c4c7`：工具参数解析、非对象参数和计划参数鲁棒性。
- agent-2 `ae9427e76`：A2A 委派请求需要自包含，不能依赖父 Agent 会话上下文。
- skill-center 最新流式执行约束：业务数据帧与 EOF 控制帧分离，消费端必须识别异常结束。

## 2. 梳理结论与同步边界

### 2.1 决定同步

1. **工具参数异常兜底**
   - ADK 2.3.0 在构造 `FunctionCall` 时要求 `args` 是对象。
   - 模型偶发返回顶层数组、标量或损坏 JSON 时，异常发生在 Plugin 回调之前，原有 `on_tool_error_callback` 无法接住，可能直接中断 turn。
   - `update_task_plan` 还存在一种可恢复的生产形态：模型返回步骤数组，而不是公开 schema 规定的 `{steps, current_step}` 对象。

2. **A2A 自包含委派**
   - ADK `AgentTool` 调远程 Agent 时创建新的内存会话，只把 `request` 文本发给子代理，不携带父 Agent 的消息历史。
   - 如果主模型生成“让它计算上面的数字”之类依赖指代的请求，远程代理无法获得必要上下文。

3. **技能流 EOF 协议闭环**
   - 原客户端把 HTTP 流自然结束也当作正常结束，无法区分“正常 EOF”和“连接截断”。
   - 原结果汇总优先返回卡片、聚合文本或 `skipSummarization`，可能让前半段结果掩盖后续协议错误。

### 2.2 本次不计划同步

- AGUI/iPaaS 工作流 terminal-card 修复：当前项目没有对应入口和事件模型。
- 请求级 thinking 隔离：当前项目固定使用 `enable_thinking=false`，没有生产项目中的共享可变请求配置。
- SSRF 治理：当前 skill-center 没有通用外部 URL 请求能力；A2A 注册表当前有意只指向本地演示服务。
- 数据集 NULL 修复、MCP memory、自定义凭据治理：不属于当前项目的核心链路。

## 3. 实施计划

### 3.1 工具参数规范化与分发前拦截

- 增加一个 ADK 2.3.0 专用、幂等安装的消息转换 shim。
- 继续复用 ADK 自带的 JSON、Python literal 和未加引号对象键修复逻辑；正常对象参数完全走原路径，不重新序列化。
- 对可恢复的 `update_task_plan` 顶层数组生成标准 `{steps, current_step}`：
  - 全字符串数组去除步骤首尾空白，默认从第 1 步开始。
  - 全对象数组依次从 `content/tasks/task/description` 提取标题。
  - 优先选择 `in_progress/running` 步骤；否则选择首个非 `completed/done` 步骤；全部完成时使用 `len(steps)+1`。
  - 空步骤、混合类型或无法识别的数组不做猜测，统一进入错误 sentinel。
- 其他工具的数组、标量或损坏参数转换为版本化 sentinel，且不在日志中记录原始参数。
- 在主 Runner 的 `AgentInvocationPlugin` 和 Claude SKILL 独立 Runner 中增加分发前 guard；命中 sentinel 时返回结构化 `ToolArgumentsParseError`，不调用真实工具。
- 任务计划工具、计划状态和 `plan_step` 事件探测共用同一份参数规范化规则。

### 3.2 A2A 自包含委派

- 继承 ADK `AgentTool`，保留父类生成工具声明、响应 schema 和执行逻辑。
- 只增强 `request` 字段描述，明确远端调用是无父消息历史的新会话。
- 要求主模型在 request 中展开代词和省略信息，并完整给出目标、范围、约束、输入数据和必要上下文。
- 同时兼容 ADK 的 `parameters_json_schema` 与 `parameters` 两种声明表示；结构不符合预期时明确失败。

### 3.3 技能流协议闭环

- 在共享 `SkillResultDTO` 契约中禁止 `eof=true` 与非空 `data` 同帧出现。
- 客户端收到合法 EOF 后立即结束，不再处理 EOF 后面的内容。
- 将异常终止分类为：
  - `SKILL_HTTP_ERROR`：skill-center 返回非 200。
  - `SKILL_TRANSPORT_ERROR`：收到有效帧前发生连接、超时或读取异常。
  - `SKILL_STREAM_EMPTY`：HTTP 200，但没有任何有效数据帧和 EOF。
  - `SKILL_STREAM_INCOMPLETE`：已经收到数据帧，但连接结束或异常中断时没有 EOF。
  - `SKILL_PROTOCOL_ERROR`：NDJSON 无法解析或 DTO 违反协议。
  - `SKILL_EXECUTION_ERROR`：上游明确失败但未提供错误码。
- 任意显式失败帧都会使整体调用失败；首个错误码和错误信息保持 sticky，后续成功或失败帧均不覆盖。最终错误优先于此前收到的卡片、文本和 `skipSummarization`。
- 不改变对外 HTTP 路由、Agent SSE 事件类型和已有 skill-center 正常生产者的数据内容。

### 3.4 文档与验证

- 同步 `README.md`、`RUNBOOK.md`、`AGENTS.md` 和 `CLAUDE.md`。
- 不新增或运行单元测试、端到端评测，不启动四个服务。
- 仅使用项目指定虚拟环境执行 Python 语法编译，并运行 `git diff --check`。

## 4. 最终实际改动

### 4.1 工具参数主链路

新增文件：

- `agent/tool_args_contract.py`
  - 定义版本化 sentinel：`__sxw_agent_tool_args_parse_error_v1__`。
  - 提供计划参数统一规范化、步骤标题别名识别、状态推断和范围校验。
- `agent/llm/tool_args_normalizer.py`
  - 在 ADK 2.3.0 的 `_message_to_generate_content_response` 切面安装幂等 shim。
  - 正常对象参数直接调用原函数；仅异常消息执行深拷贝和定点参数替换。
  - 保留 inline-text tool call、流式文本、usage、finish reason 和 MAX_TOKENS 原有行为。
  - 运行时校验 `google-adk==2.3.0`；版本或私有符号不匹配时启动失败，避免静默失效。
- `agent/plugins/tool_args_guard_plugin.py`
  - 提供共享的 sentinel 识别和 `ToolArgumentsParseError` function response。
  - 提供给独立子 Runner 使用的轻量 `ToolArgsGuardPlugin`。

修改文件与行为：

- `agent/llm/hardened_litellm.py`：`build_llm()` 构造模型前安装 shim。
- `agent/plugins/agent_invocation_plugin.py`：真实工具调用和调用日志之前检查 sentinel。
- `agent/claude_skill/skill_runner.py`：独立 `InMemoryRunner` 注册轻量 guard。
- `agent/engine/agent_loop/task_plan_tool.py`：写入 state 前校验并规范化计划；防御性处理异常 state。
- `agent/engine/agent_loop/plan_event_detector.py`：非法参数不再打断 SSE；只有规范化成功才生成 `plan_step`。

最终调用链：

```text
模型工具参数
  -> ADK 原解析能力
  -> 正常对象：完全走 ADK 原转换
  -> update_task_plan 顶层数组：恢复成标准计划对象
  -> 其他异常形态：转换成安全 sentinel
  -> before_tool_callback 检查 sentinel
  -> 命中：返回 ToolArgumentsParseError，不分发真实工具
  -> 未命中：按原路径执行工具
```

### 4.2 A2A 主链路

- `agent/a2a/loader.py` 新增 `SelfContainedA2AAgentTool`。
- 子类调用 `super()._get_declaration()` 后只补充 `request` 描述，不手写整份 FunctionDeclaration。
- 两代引擎从共享 `ctx.tools` 获取 A2A 工具，因此无需分别修改 Agent-Loop 和 Plan-Execute。
- A2A 服务端、agent-card、JSON-RPC 和 skill-center 注册表均未修改。

### 4.3 技能流主链路

- `common/skill_contract.py`
  - 增加 Pydantic `model_validator`，拒绝 `eof=true,data!=null`。
- `agent/skills/client.py`
  - EOF 成为必须的成功终止信号。
  - 增加 HTTP、传输、空流、截断流、协议错误和无上游错误码兜底的稳定分类。
  - 合法 EOF 到达后立即返回，忽略尾随帧。
  - 结构化日志记录稳定错误码和安全诊断字段，不记录响应正文或原始坏帧。
- `agent/skills/result_parser.py`
  - 增加统一错误提取；EOF 错误帧不再被提前忽略。
  - 错误帧不参与 UI 展示和正文聚合。
- `agent/skills/selected_skill_tool.py`
  - 按首个失败 sticky 聚合 `errorCode/errorMsg`，后续帧不覆盖根因。
  - 最终错误优先级高于 `skipSummarization`、卡片和聚合文本，并返回 `isError=true,errorCode`。

最终流协议：

```text
0..N 个数据帧：eof=false，可携带 text/card/thinking/data
1 个结束帧：eof=true，data=null，可携带 errorCode/errorMsg/customMetadata

缺 EOF                  -> SKILL_STREAM_EMPTY / SKILL_STREAM_INCOMPLETE
坏 JSON 或 data+eof 合帧 -> SKILL_PROTOCOL_ERROR
合法 EOF                -> 立即结束消费
```

当前 `translate`、`weather` 和未知技能错误生产者已经符合该协议，因此没有修改 `skillcenter/skills.py`。

### 4.4 文档同步

- `README.md`：核心能力、架构图、面试 talking points 和工具失败模型。
- `RUNBOOK.md`：模型参数防护、技能流协议、Claude SKILL guard 和 A2A 调用约束。
- `AGENTS.md`、`CLAUDE.md`：工程约定、ADK 私有 shim 升级风险和三类扩展智能体的最新边界。
- 没有修改 `eval/` 数据、评分器、报告或评测说明，也没有宣称新增测试覆盖。

## 5. 计划与实际落地对照

| 计划项 | 落地结果 |
|---|---|
| 非对象工具参数不再在 ADK 转换阶段中断 turn | 已通过 ADK shim 转成标准计划对象或错误 sentinel |
| sentinel 在真实工具执行前短路 | 主 Runner 与 Claude SKILL 子 Runner 均已覆盖 |
| 正常对象参数和文本流行为不变 | 正常对象直接调用 ADK 原转换函数；不接管文本流 |
| 任务计划恢复、写状态、SSE 共用规则 | 已统一到 `normalize_task_plan_args()` |
| A2A request 必须自包含 | 已通过 `SelfContainedA2AAgentTool` 增强工具声明 |
| EOF 与数据帧分离 | 已在共享 DTO 层强校验 |
| 缺 EOF、坏帧不能伪装成功 | 已分类为稳定协议错误，且错误优先返回 |
| 技能错误码端到端可观测 | 首个失败的错误码保留到 function response，并写入结构化日志 |
| 外部传输接口保持不变 | 工具名称、HTTP 路由和 Agent SSE 类型未调整；错误 function response 新增 `errorCode` |
| 文档同步 | 四份项目主文档已更新 |

## 6. 验证结果与边界

使用指定虚拟环境：

```text
/Users/shixiangweii/PycharmProjects/run_proj/sxw_agent-2_demo/.venv
```

已执行：

```bash
find agent arag common skillcenter a2a_service -name '*.py' -print0 \
  | xargs -0 /Users/shixiangweii/PycharmProjects/run_proj/sxw_agent-2_demo/.venv/bin/python -m py_compile

git diff --check
```

结果：两条命令退出码均为 `0`，无输出。

按本次约定未执行：

- 未新增或运行单元测试。
- 未运行真实 LLM 黑盒评测。
- 未启动 agent、arag、skill-center、a2a_service。
- 未进行真实 A2A 和技能流网络调用。

## 7. 建议的实际评测场景

后续人工运行与评测时建议重点覆盖：

1. 正常对象工具参数，确认行为与改动前一致。
2. `update_task_plan` 顶层字符串数组，确认恢复为第 1 步并产生 `plan_step`。
3. 带 `tasks/status` 的计划对象数组，确认 current step 推断正确。
4. 普通工具收到数组、标量和损坏 JSON，确认真实工具不执行、模型收到结构化反馈后继续对话。
5. Claude SKILL 子代理生成异常沙箱工具参数，确认子 Runner 同样能短路。
6. A2A 请求包含完整数字、目标和约束，不依赖“它/上面的内容”等父会话指代。
7. 技能正常数据帧 + EOF、空流、收到部分数据后断流、`data+eof` 合帧和损坏 NDJSON。
8. 部分卡片或文本之后出现终止错误，确认最终 function response 为错误而不是成功内容。

## 8. 后续维护注意事项

- 工具参数 shim 有意依赖 ADK 2.3.0 的私有转换函数。升级 `google-adk` 前必须重新检查函数签名、流式聚合位置和 FunctionCall 构造逻辑。
- 新增 skill-center 流式技能时，生产者必须始终发送独立 EOF；不要把最后一块业务数据和 EOF 合并。
- 新增 A2A 子代理时无需复制自包含描述逻辑，应继续通过 `SelfContainedA2AAgentTool` 包装。
- 若未来引入请求级 thinking、外部 URL 技能或通用 A2A 注册来源，再单独评估请求隔离、SSRF 和凭据治理，不在当前实现上预留空兼容层。
