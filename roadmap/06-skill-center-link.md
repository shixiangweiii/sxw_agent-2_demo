# 06 · agent → skill-center 技能调用链路（精简复刻）

## Context

demo 已有 agent → arag（知识检索）一条下游链路。本篇新增第二条核心下游链路：
**agent-2 调用「技能中心（skill-center）」执行技能工具**——这是 agent runtime「技能调用」最具代表性的生产链路：
技能目录加载（含快照）→ 把技能包装成可被 LLM 调用的工具 → 构造执行上下文 + 请求 → 调技能中心
`execute-streaming`（NDJSON 流）→ 流式解析 `SkillResultDTO`（思考/卡片/文本/增量/结束/算粒错误）→ 回流到 agent。

### 真实源码映射（复刻对象）

| demo 复刻点 | 真实源码 |
|---|---|
| skill-center 服务（执行网关） | Java `albert-skill-center`：`SkillRuntimeController` → `/api/v1/skills/runtime/{execute, execute-streaming(SSE), list}`；技能为 **MCP 风格**（tools[]+inputSchema），结果用 MCP `content:[{type:text,text}]` |
| 技能调用客户端 | `app/infra/boundary/albert_skill_center/client.py` `AlbertSkillClient.execute_tool` / `execute_tool_by_sse` |
| 流式协议 + 解析 | `.../stream_processor.py` `SSEStreamProcessor`（NDJSON 字节→行）+ `.../models.py` `SkillResultDTO` |
| 技能→工具包装 + 结果解析 | `app/lumi/tools/base_select_tool.py` `BaseSelectedTool`（`_get_declaration` / `build_skill_execute_context` / `run_async` / `_parse_result`）|
| 技能目录（快照） | `client.py` `list_user_skills_v2`（snapshotTag PUBLISHED/DRAFT）|

---

## 1. 架构（新增第 3 个服务）

```
agent (:8000) ──┬── knowledge_search ──httpx──▶ arag (:8100)            [已有]
                └── <动态技能工具>     ──httpx──▶ skill-center (:8200)    [新增]
```

启动时 agent 从 skill-center 拉技能目录（PUBLISHED 快照）→ 每个技能工具包装成 ADK 工具加入工具集；
LLM 选中并调用某技能工具 → 客户端流式调 skill-center → `SkillResultDTO` 流回 → 经 UI 事件队列以 `skill_event` 汇入 agent SSE，并聚合文本返回给 LLM。

---

## 2. 线协议契约（与真实一致）

### 端点（skill-center）
- `POST /api/v1/skills/runtime/list` — 技能目录（每技能含 tools[]：name/description/inputSchema）+ `snapshotTag`
- `POST /api/v1/skills/runtime/execute` — 同步执行 → `{success, result: SkillToolExecuteResultDTO}`
- `POST /api/v1/skills/runtime/execute-streaming` — **NDJSON 流**：每行一个 `SkillResultDTO`
- header：`X-User-Payload`(base64 用户)、`X-Trace-Id`、`X-Deap-AgentUUid`

### 请求 `SkillToolExecuteRequestDTO`
`tenantId`(智能体id) / `skillId` / `toolName` / `arguments`(dict) / `meta` / `context`(`SkillToolExecuteContext`: source + attributes{sessionInfo,userInfo,inputInfo,agentInfo,invocationInfo,bizContext})

### 流式结果 `SkillResultDTO`（核心富信封）
`success` / `requestId` / `errorMsg` / `errorCode` / `seq`(流序号) / `dataType`（生产全集含 AGUI/REAL/WEB_PAGE…；**demo enum 裁剪到 TEXT/CARD/JSON/IMAGE/FILE/CHART**，见 `common/skill_contract.py:SkillResultDataType`） / `eof`(结束) / `data`(技能结果) / `isDisplay`(是否展示) / `isThinking`(技能思考) / `isPartial`(增量/全量) / `skipSummarization`(单技能跳过总结直呈) / `isAppendSession`(是否入上下文) / `customMetadata`(引用映射等)

### 同步结果 `SkillToolExecuteResultDTO`
`content`(MCP `[{type,text}]`) / `structuredContent` / `isError` / `errorMessage`

---

## 3. 内置演示技能（skill-center 托管）

| 技能 | tool | 演示点 |
|---|---|---|
| `deep_translate` | `translate(text, target_lang)` | **流式**：先 `isThinking` 思考帧 → 多个 `isPartial=true` TEXT 增量 → `eof`；末帧 `customMetadata` |
| `weather_card` | `query_weather(city)` | **CARD**：`dataType=CARD` 结构化卡片，`isDisplay=true` + `skipSummarization=true`（直呈不总结）|
| （注入式）算粒错误 | translate 传 `__quota__` 哨兵 | 末帧 `errorCode=BENEFIT_NOT_ENOUGH` → 客户端抛 `SkillInvokeException(QUOTA_EXCEEDED)` → agent 友好提示 |

---

## 4. agent 侧链路组件（`agent/skills/`）

| 文件 | 职责 | 复刻自 |
|---|---|---|
| `models.py` | DTO：`SkillToolExecuteRequestDTO/ResultDTO`、`SkillResultDTO`、`SkillResultDataType`、`SkillToolExecuteContext/Attributes`、目录 `SkillVO`(含 tools) | `boundary/.../models.py` |
| `stream_processor.py` | `SSEStreamProcessor`：NDJSON 字节→行，处理跨 chunk 半个 UTF-8 / 半行 | `.../stream_processor.py` |
| `client.py` | `SkillCenterClient`：`execute_tool`(同步) + `execute_tool_by_sse`(流)；建 header(base64 用户+trace) + DTO；解析流、`eof`、算粒 errorCode、空结果兜底 | `AlbertSkillClient` |
| `selected_skill_tool.py` | `SelectedSkillTool(BaseTool)`：`_get_declaration`(input schema) + `run_async`(建上下文+DTO→流式调用→`skill_event` 入队 + 聚合返回 LLM；执行前按 schema coerce 参数；空流/错误帧上浮 isError；skipSummarization 软抑制总结) | `BaseSelectedTool` |
| `args_coercion.py` | `coerce_args_by_schema`：执行前按 input_schema 把 array/object 的 JSON 字符串反序列化（修 LLM 常见参数输出问题）| `app/lumi/tools/args_coercion.py`（精简）|
| `result_parser.py` | 解析 `SkillResultDTO`：取 `content[0].text`、按 `dataType` 分流、`isThinking/isPartial/skipSummarization` | `_parse_result`（精简，去技能特化）|
| `catalog.py` | 启动时拉目录(快照)→ 构造 `SelectedSkillTool` 列表 | `list_user_skills_v2` + 工具装配 |
| `request_context.py` | 每请求 contextvar：user/session/agent_uuid/text（供 run_async 建执行上下文）| `build_skill_execute_context` 的入参来源 |
| `ui_event_queue.py` | 每请求 `asyncio.Queue`(contextvar)：技能流式展示帧汇入 agent SSE | `single_loop/ui_event_queue.py` |

### 引擎集成
- `context.py`：启动 `load_skill_tools(settings)` 拉目录 → 加入 `ctx.tools`（两代引擎共享；skill-center 不可用则跳过+告警，不阻断启动）。
- `api/chat.py`：入口设 `request_context` contextvar + 建 `ui_event_queue`。
- 引擎（两代）：消费 Runner 流的同时**并发 drain `ui_event_queue`**（合并两个异步源）→ emit `skill_event` SSE。新增 SSE 事件类型 `skill_event`（载 `skill/dataType/data/isDisplay/isThinking/isPartial`）。

---

## 5. 实施里程碑  ✅ S0–S3 全部完成

> **实施记录**：81 文件 `py_compile` 全绿；三服务 E2E 通过——
> `deep_translate` 技能：LLM 调用 → `skill_event` 实时流（思考帧 → 增量 TEXT）→ 聚合答复；
> `query_weather` 技能：`skill_event(dataType=CARD)` → 卡片渲染；两代引擎(agent_loop/plan_execute)均贯通；
> 算粒哨兵 `__quota__` → `[QuotaDeny] BENEFIT_NOT_ENOUGH` → 友好降级不崩；
> 技能目录启动加载 `[SkillCatalog] loaded count=2`；trace_id 跨服务串联。
> 命名冲突修复：deep_translate 技能工具名由 `translate` 改为 `deep_translate`（避开 M3 deferred 的 `translate`）。
> 文件：`common/skill_contract.py`、`skillcenter/{config,skills,api,main}.py`、
> `agent/skills/{stream_processor,request_context,ui_event_queue,result_parser,client,selected_skill_tool,catalog,stream_merge}.py`；
> 接线：`agent/{config,context,main,api/chat}.py` + 两代引擎 runner 循环改用 `merge_runner_events`。

- **S0** skill-center 服务骨架：FastAPI(:8200) + `models.py` + `/list` 目录(snapshotTag) + `/execute` 同步 + 2 个演示技能(同步) + healthz。
- **S1** skill-center `/execute-streaming`：NDJSON `SkillResultDTO` 流 + deep_translate(思考+增量) / weather_card(CARD) 流式产出 + 注入式算粒错误。
- **S2** agent 侧链路：models + SSEStreamProcessor + SkillCenterClient(同步+流) + result_parser + catalog 加载 → SelectedSkillTool 注入 `ctx.tools` + request_context。
- **S3** 流式集成：ui_event_queue + 引擎合并 → `skill_event` SSE；算粒错误处理；两代引擎贯通；E2E（LLM 调技能→流式卡片/文本→聚合答复）。

---

## 6. 刻意裁剪（范围声明）

精简保留「调用链路形状 + 关键工程取舍」，**有意裁剪**真实里的技能业务特化与重治理：
- 技能特化分支：DQL/问数、全网搜索、browser-use、real 长轮询、AGUI 事件、dingtalk MCP、post-handler（图片/视频异步轮询）。
- 真实 MCP 协议握手（demo 用本地函数模拟技能 tool 执行，产出 MCP 形态 `content:[{text}]`）。
- 双快照一致性治理（仅保留 `snapshotTag` 参数与 PUBLISHED 运行态；DRAFT 仅占位）。
- HSF/SPI、计费扣减真实链路、ToolConfig 全量字段。

> 定性：复刻「技能目录→工具→流式执行→结果解析→回流」主链路与 `SkillResultDTO` 富信封语义，非技能业务全集。

---

## 7. 验证（真实 LLM E2E + py_compile，不写单测）

1. 全量 `py_compile` 绿。
2. skill-center 独立：`curl /api/v1/skills/runtime/list` 见 2 技能；`/execute` 同步返回 content；`/execute-streaming` 见 NDJSON 多帧 + eof。
3. 目录加载：agent 启动日志见 `[SkillCatalog] loaded N skills`；`ctx.tools` 含技能工具。
4. E2E：`ENGINE=agent_loop` 问「帮我把『你好世界』翻译成英文」→ LLM 调 `deep_translate` → 流式 `skill_event`(thinking→partial text) → 聚合答复；问「查一下杭州天气」→ `weather_card` → `skill_event(dataType=CARD)`。
5. 算粒错误：触发 `__quota__` → agent 收 `error`/友好提示，不崩。
6. 降级：skill-center 宕机 → 技能工具调用返回结构化错误，turn 不中断。
