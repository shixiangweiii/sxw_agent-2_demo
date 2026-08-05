# 代码评审：agent-2 / skill-center 最新变更同步

- 评审时间：2026-08-05
- 评审对象：提交 `3995ad3`（同步实际生产项目最新的更改，18 文件，+676 / -49）
- 对照文档：`sxw_aicoding/changelog/20260805_agent2_skillcenter_latest_changes_sync.md`
- 评审方式：逐文件 diff 审读 + 对照本机安装的 `google-adk==2.3.0` 源码核对私有切面假设 + 用项目虚拟环境（`.venv`）做了功能级冒烟验证（shim 14 个场景、本地 stub 技能服务 6 种流形态、guard/detector/task_plan 单元级验证、py_compile 门）

## 1. 总体结论

**通过（建议合并）**。改动范围收敛、与 changelog 声称一致，三条主线（工具参数防护、A2A 自包含委派、技能流 EOF 协议闭环）的核心行为均经实际运行验证符合文档描述；四条文档同步无虚假声明；编译门与 `git diff --check` 通过。未发现阻断性缺陷，发现 2 个中等建议项、4 个低优先级观察项，见第 4 节。

## 2. 分模块验证记录

### 2.1 工具参数规范化 shim（`agent/llm/tool_args_normalizer.py` 等）

对照 ADK 2.3.0 源码核实了三个关键前提：

1. **私有符号存在且语义匹配**：`_message_to_generate_content_response` / `_split_message_content_and_tool_calls` / `_parse_tool_call_arguments` 签名与 shim 假设一致（`tool_args_normalizer.py:38-48` 的安装期校验 + 版本 pin 是正确做法，满足 AGENTS.md「私有符号不匹配应启动失败而非静默降级」的约定）。
2. **无流式半截参数风险**：ADK 流式路径先把 tool call 分片累积进 `function_calls` buffer，只有完整累积后的消息和纯文本 partial chunk 会进入被包装函数；截断参数（finish_reason=length）在更上游已转成 MAX_TOKENS 错误。shim 不会把流式中间态误判为坏参数。
3. **inline-text fallback 处理正确**：`tool_args_normalizer.py:77-86` 深拷贝后实体化 split 结果（content=remainder、tool_calls=fallback 解析结果），避免 original 二次解析原文本丢失替换——实测含坏参 inline JSON 的文本消息能正确产出「文本 part + sentinel FunctionCall」。

实测 14 个场景全部符合预期：正常对象走原路径不重序列化；`update_task_plan` 顶层字符串数组恢复为 `{steps, current_step:1}`；对象数组按 `content/tasks/task/description` 别名取标题、按 `in_progress/running → 首个非 completed/done → len+1` 推断当前步；混合类型/空步骤/越界 → `TaskPlanArgumentsError` → sentinel；标量、非计划工具的数组、损坏 JSON → sentinel；Python literal 修复路径（`{'a': 1}`）不受影响；幂等安装标记生效。

### 2.2 分发前 guard（`tool_args_guard_plugin.py` / `agent_invocation_plugin.py` / `skill_runner.py`）

- 主 Runner 两代引擎共用 `AgentInvocationPlugin`（`agent_loop_engine.py:74`、`execution_planner.py:54`），Claude SKILL 独立 Runner 挂 `ToolArgsGuardPlugin`（`skill_runner.py:43`，ADK 2.3.0 `InMemoryRunner` 确有 `plugins` 参数），覆盖完整。
- 实测 sentinel 命中返回结构化 `ToolArgumentsParseError` + hint，未命中返回 None 不干扰原路径；日志只记工具名和 errorType，不记原始参数，符合「不记录原始坏参数」约定。

### 2.3 A2A 自包含委派（`agent/a2a/loader.py`）

- ADK 2.3.0 默认开启 `JSON_SCHEMA_FOR_FUNC_DECL`，走 `parameters_json_schema` 分支；实测 `SelfContainedA2AAgentTool._get_declaration()` 正确把自包含说明注入 `request.description`，且 legacy `types.Schema` 分支也有对应处理，两分支缺失 `request` 时显式 RuntimeError（符合 changelog「结构不符合预期时明确失败」）。
- 子类只改声明不改 `run_async`/响应 schema，委派执行链路零侵入，判断正确。

### 2.4 技能流协议闭环（`common/skill_contract.py` / `agent/skills/*`）

用本地 stub 服务实测 6 种流形态：

| 场景 | 实测结果 |
|---|---|
| 数据帧 + EOF（EOF 后还有尾随帧） | 正常聚合，尾随帧被忽略（EOF 即 return） |
| 200 空流 | `SKILL_STREAM_EMPTY` |
| 有数据帧无 EOF（截断） | `SKILL_STREAM_INCOMPLETE` |
| 坏 JSON 帧 | `SKILL_PROTOCOL_ERROR` |
| `eof=true` + 非空 data 合帧 | DTO validator 拒绝 → `SKILL_PROTOCOL_ERROR` |
| 卡片(skipSummarization) 后接错误 EOF | 最终 `isError=true`，`skipSummarization` 强制置 False，错误优先 |

- 现有 skillcenter 三个生产者（translate/weather/未知技能）帧形均已合规，未改 `skills.py` 的判断成立。
- `extract_text`/`to_skill_event` 对错误帧一律不参与聚合与 UI 展示，行为正确。
- `plan_event_detector` 与 `task_plan_tool` 共用 `normalize_task_plan_args()`，实测非法参数不再打断 SSE、损坏 state 下 `has_open_steps` 防御性返回 False，与 changelog 一致。

### 2.5 文档与验证声明

README/RUNBOOK/AGENTS/CLAUDE 四处更新与实现逐条核对无夸大（如未声称新增测试覆盖）；RUNBOOK 6.7 新节约束与代码行为一致。changelog §6 声称的 py_compile / git diff --check 已复跑，均通过。

## 3. 值得肯定的设计点

1. **正常路径零侵入**：shim 对正常对象参数直接透传原函数，不深拷贝、不重序列化，把回归风险压到最小。
2. **失败要响亮**：ADK 版本/私有符号不匹配时启动即失败，而非静默降级；与仓库 pin `google-adk==2.3.0` 呼应。
3. **规则单一来源**：计划参数的恢复、写 state、SSE 探测三处共用 `normalize_task_plan_args()`，消除了三套规则漂移的隐患。
4. **协议错误码稳定化**：`SKILL_STREAM_EMPTY/INCOMPLETE/PROTOCOL_ERROR` 为可观测性提供了可检索的稳定锚点。
5. **诚实边界**：未同步项（AGUI、thinking 隔离、SSRF 等）给出了与本项目形态匹配的理由，未预留空兼容层。

## 4. 问题与建议

### 4.1 中等

**M1. `extract_error` 把任意 `success=False` 中间帧也视为终止错误**（`agent/skills/result_parser.py:13-22` + `selected_skill_tool.py:80-82`）。
协议约定错误信息应由 EOF 帧携带，但实现上任何一帧 `success=False`（哪怕后续有合法 EOF）都会把整次调用标记为失败。当前生产者都受控、不会这样发，属于防御过严而非 bug；但与 changelog「EOF 错误帧必须先解析错误信息」的表述存在语义差。建议二选一：(a) 在 DTO validator 层补充「非 EOF 帧不允许 success=False + errorCode」的约束使协议自洽；(b) 在文档中明确「中间错误帧同样视为终止错误」是有意设计。

**M2. 连接级异常缺稳定错误码**（`agent/skills/client.py:154`）。
无帧时的通用异常兜底返回裸 `errorMsg="技能执行异常"`，不带 errorCode；而空流/截断/协议错误都有稳定码。排障时无法用 `[SkillInvoke]` + errorCode 统一检索连接失败这一类。建议补一个如 `SKILL_CONNECT_ERROR` 的稳定码（复用 `_stream_error()` 即可，一行改动）。

### 4.2 低

**L1.** `SelfContainedA2AAgentTool` 对「带自定义 input_schema 且无 request 属性」的 agent 会在**声明构造期**（每次模型调用组装工具表时）抛 RuntimeError，可能波及整个模型请求而非只跳过该工具。当前 `RemoteA2aAgent` 不设 input_schema，不可达；但若未来新增 A2A 子代理带 input_schema，需在 loader 阶段提前校验并给出可定位错误，建议补一句注释提示该前提。

**L2.** `plan_event_detector` 行为变更需知晓：旧实现对缺 `current_step` 的对象参数默认按 1 生成 `plan_step` 事件；新实现直接不发事件。由于工具本身强制要求 `current_step`（缺失时真实调用也会失败），新行为更一致，属有意收紧，仅提示评测/演示时不要以旧行为为基线。

**L3.** `tool_args_contract.py:104` `value.strip().isdigit()` 对全角数字（如 "１２"）返回 True，`int()` 也能解析，行为上无害；如追求严格可用 `str.isascii() and str.isdigit()`。可不改。

**L4.** `tool_args_normalizer.py:99` 只捕获 `(TypeError, ValueError)`。对 `ast.literal_eval` 极端输入理论上还有 `MemoryError/RecursionError` 逃逸可能（会中断 turn，即回到改动前行为）。概率极低，若追求完全兜底可放宽为 `Exception`，但会掩盖真实 bug，保持现状也合理——仅记录知悉。

## 5. 后续建议（与 changelog §7/§8 一致，补充两点）

1. changelog §7 列的 8 个评测场景建议尽快在真实 LLM 上跑一轮（本次评审已用 stub/单测覆盖了其中可离线验证的 1/2/3/4/7/8，场景 5/6 依赖真实模型行为）。
2. 升级 `google-adk` 前除重审 shim 外，注意本次评审确认过的两个隐式依赖：ADK 流式 tool-call 累积逻辑、`_split_message_content_and_tool_calls` 的 inline-text fallback 语义——二者变化都会影响 shim 的 deepcopy 实体化路径。
