# 代码评审：全仓库整体评审（ADK 2.6.2 升级后基线）

- 评审时间：2026-08-05
- 评审对象：`main` @ `68b026b`（升级依赖版本）全量代码，约 6.9k 行 Python + 1k 行 Web
- 评审范围：`agent/`、`arag/`、`skillcenter/`、`a2a_service/`、`common/`、`eval/harness/`、`web/`、`scripts/`
- 对照文档：`README.md`、`RUNBOOK.md`、`CLAUDE.md`、`AGENTS.md`、`sxw_aicoding/changelog/*`、`sxw_aicoding/项目背景说明.txt`
- 与既有评审的关系：`20260805_agent2_skillcenter_latest_changes_sync_review.md`（提交 `3995ad3`）与
  `20260805_unified_tool_use_agent_loop_agent_as_tool_review.md`（提交 `bf28531`）已深度覆盖
  Claude SKILL 运行时、技能流协议、工具参数 shim 与 A2A 委派。本次为**全仓库整体评审**，
  重点放在两类此前未被系统性覆盖的区域：
  1. 从未被评审过的模块：`arag/` 检索与存储、`citation/`、`llm/` 加固层、两代引擎编排、`eval/harness/`、`web/`；
  2. 最近一次 ADK 2.3.0 → 2.6.2 升级引入的私有契约与公开 API 变更。
- 评审方式：逐文件审读 + 用项目虚拟环境 `.venv` 对**每一条中等级别结论做可运行验证**
  （ADK 私有符号签名核对、工具声明转换、消息裁剪配对、citation 编号、检索阈值、
  `.env.example` 解析、密钥与产物扫描），并复跑 `py_compile` 门。

---

## 1. 总体结论

**通过。** 这份代码在架构清晰度、边界诚实度和失败语义上的水准，明显高于一般的"面试样板工程"：
两代引擎共用同一 `ReasoningEngine` 端口而只在"如何编排"上分叉；生产加固严格落在 ADK 的
Plugin 与 LiteLlm 两个官方扩展点上而非侵入框架内部；Claude SKILL 的取消/清理链路、
并发门控与结构化错误码经得起实测。ADK 2.6.2 升级也做得干净——**升级中风险最高的两处
（工具参数 shim 的私有符号、`_to_gemini_schema` 私有 API 下线）经实测均已正确收口**。

本次发现 **3 个中等问题 + 9 个低优先级问题**，无阻断性缺陷。三个中等问题有一个共同特征：
**它们都在"降级/兜底路径"上，而这些路径恰恰是本项目对外主打的卖点**——
上下文超长重试、多跳检索的引用正确性、"无资料不编造"。主链路（happy path）没有问题，
但兜底路径的实现强度落后于文档对它们的描述。这是我建议优先处理的方向。

---

## 2. 分模块验证记录

### 2.1 ADK 2.6.2 升级（提交 `68b026b`）—— 重点复核，结论：干净

升级同时动了「精确 pin 的私有契约」和「已下线的私有 API」，是本次评审优先级最高的部分。逐项实测：

| 升级点 | 验证方式 | 结果 |
|---|---|---|
| `tool_args_normalizer` 依赖的三个 LiteLlm 私有符号 | 实际调用 `install_adk_tool_args_normalizer()` + `inspect.signature` | ✅ 三个符号均在，签名与 shim 假设一致，幂等标记生效 |
| `_message_to_generate_content_response` 签名变化 | 同上 | ✅ `(message, *, is_partial, model_version, thought_parts)`，shim 的 `*args/**kwargs` 透传兼容 |
| `_to_gemini_schema` → `parameters_json_schema` | 构造 `SelectedSkillTool` 两种形态，过 `_function_declaration_to_tool_param` | ✅ 有 schema：原样透传为合法 JSON Schema；**无 schema（`input_schema=None`）：ADK 兜底为 `{"type":"object","properties":{}}`，未抛异常** |
| `_detect_error_in_response` 是否仍被动态调用 | grep ADK 源码 | ✅ `flows/llm_flows/functions.py:106` 仍用 `getattr(tool, '_detect_error_in_response', None)`——**前次评审的 M1「死代码」确为误判，`015899c` 的处理（保留 + 补注释）正确，本次注释版本号也已同步为 2.6.2** |
| 版本 pin 一致性 | `pip show` | ✅ `google-adk==2.6.2` / `a2a-sdk==1.1.2` / `litellm==1.95.0` / `google-genai==2.16.0`，与 `requirements.txt` 完全一致 |

`parameters_json_schema` 这个改动实际上比升级前更正确：skill-center 下发的本来就是标准 JSON Schema，
过去经 `_to_gemini_schema` 转换反而有信息损失风险，现在是直通。

### 2.2 两代引擎与循环控制

- `ReasoningEngine` 端口抽象干净，`build_engine()` 选型集中；两代引擎共享 `ctx.tools`、
  `AgentInvocationPlugin`、`merge_runner_events`、citation 包裹层，"切换只改编排"的说法成立。
- `agent_loop`：软收尾（`LoopController` 在 `iter >= max_iters` 注入 force-summary）+ 框架硬熔断
  （`max_llm_calls = max_iters + 2`）两层设计合理，`_HARD_CAP_MARGIN=2` 给 force-summary 留了生效窗口。
- `plan_execute`：`AgentInvocationPlugin()` 不传 controller → `before_model` 为 no-op，
  只保留 ToolErrorFeedback。这是**有意的**（代码注释已说明），但由此产生的失败形态差异未被记录，见 L1。
- `LoopController._inject` 把系统提醒追加到 `llm_request.contents`（每次模型调用的临时视图），
  不落 session、不跨轮累积，正确。
- `MessageBudget` 的原子配对裁剪实测有效：31 条含工具轮次的历史裁到 2 条，**零孤立 call/response**。

### 2.3 技能与 Claude SKILL 链路（复核既有结论）

前两轮评审已做过 70+ 项功能级验证，本次只做一致性复核，未发现回归：

- 技能流稳定错误码（`SKILL_HTTP_ERROR` / `TRANSPORT` / `STREAM_EMPTY` / `STREAM_INCOMPLETE` /
  `PROTOCOL_ERROR` / `EXECUTION_ERROR`）齐备；failure-sticky 首错保留、错误优先于卡片与
  `skipSummarization` 的逻辑在 `selected_skill_tool.py:99-137` 完整保留。
- Claude SKILL 五个稳定错误码 + `retryable` 语义、`SkillCallResult` envelope、
  `SKILL_RESULT_MAX_CHARS` 截断标记均未变。
- 取消链路（`await_with_deferred_cancellation` 统一原语 → Runner → 进程组 TERM/KILL → 临时目录）
  在 `stream_merge`、`skill_runner`、`execution_coordinator`、`local_sandbox` 四处一致复用，
  `CancelledError` 全链路不吞不降级。
- `SkillExecutionCoordinator` 获取顺序固定为 gate → 排序后资源锁 → 全局 semaphore，无顺序死锁；
  子 Agent 只拿到沙箱工具、拿不到 Claude Skill 工具，不存在嵌套 acquire 自锁。
- `build_sandbox()` 只构造不建会话，`agentbay` 的 unavailable 在 `try_create()` 抛出并落到
  `run_async` 的 `except SandboxUnavailableError` 分支 → 返回结构化 `SKILL_SANDBOX_UNAVAILABLE`，
  不会退化成通用工具异常。边界正确。

### 2.4 arag 检索与存储

- 混合召回编排（rewrite → 向量 + BM25 双路 → RRF → 过滤）结构清晰，`rrf_fuse` 实现与注释一致
  （`1/(k+rank+1)`，k=60），`_dedup_keep_best` 在融合前按 chunk_id 保留最高分，正确。
- `LocalVectorStore` 的持久化用 `tmp → replace` 原子换名，schema_version 校验、
  chunks/vectors 数量一致性校验、部分文件缺失时的"忽略并空启动"都做了，比预期扎实。
- `all_chunks()` 重建 BM25 的启动流程（`arag/context.py:43-52`）与文档描述一致。
- **但检索链路整体缺少相关性阈值**，见 M3；`_persist` 的同步 I/O 与并发保护见 L8。

### 2.5 可观测性、配置与安全

- `common/obs.py` 的 trace_id contextvar + `TraceMiddleware` 在 `dispatch` 内设置，
  starlette 的 `call_next` 子任务复制调用方 context，跨服务 header 透传（`x-trace-id`）在
  `knowledge_search`、`documents`、`skills/catalog`、`a2a/loader` 四处都带上了，链路完整。
- 密钥治理实测通过：全仓库（排除 `.venv`/`.git`）**无 `sk-` 形态疑似密钥**；
  `.env` 从未进入过 git 历史；`local_storage/` 零文件入库。
- `.env.example` 的行内中文注释能被 python-dotenv 正确剥离——实测 `cp .env.example .env` 后
  `ENGINE='agent_loop'`、`SANDBOX_PROVIDER='local'` 解析正常，`SANDBOX_PROVIDER` 的 `Literal` 校验不会误伤。
- `scripts/run_all.sh` 的启动顺序（下游先起 → 健康检查 → agent → 样本入库）与
  `trap cleanup INT TERM EXIT` 收口正确。

### 2.6 验证门

```bash
find agent arag common skillcenter a2a_service -name '*.py' | xargs .venv/bin/python -m py_compile
```
退出码 `0`，无输出。工作区 clean，与 `origin/main` 同步。

---

## 3. 值得肯定的设计点

1. **加固点选得准**：所有生产加固都落在 ADK 的两个官方切面（Plugin / LiteLlm 子类）上，
   没有 fork 框架、没有猴补 flow。唯一的私有依赖（工具参数 shim）带版本 pin + 启动期符号校验 +
   不匹配即 fail-fast，是处理"必须用私有 API"这类问题的正确姿势。
2. **失败要响亮，降级要静默，两者分得清**：配置类错误（SKILL 包非法、sandbox provider 非法）阻断启动；
   下游不可用（arag / skill-center / a2a）best-effort 跳过且日志可检索。这条边界在代码里贯彻得很一致。
3. **取消语义统一**：`await_with_deferred_cancellation` 把"反复取消打断清理"这个 asyncio 老问题
   收敛成一个原语，四处复用而不是四份 `shield` 补丁。
4. **诚实边界**：PromptCache no-op、AgentBay 桩、GraphStore 未接流、LocalSandbox 非生产隔离，
   在代码注释与四份文档中口径一致，没有把桩写成能力。
5. **评测方法论意识**：`eval/reports/AB-prompt-v1.md` 里主动记录了"忘记重新入库导致空索引"
   这个过程错误，并把"同一 prompt 改动对两代引擎效果相反"作为决定性结论固化下来——
   这比报告里的分数本身更有价值。

---

## 4. 问题与建议

### 4.1 中等

#### M1. ContextOverflow 反应式截断会切断工具调用配对，导致重试请求本身非法

**位置**：`agent/llm/hardened_litellm.py:31-38`（`_truncate_contents`）

`_truncate_contents` 用 `contents[-10:]` 做朴素尾切，不考虑 `function_call` / `function_response`
的配对关系。而主动侧的 `MessageBudget` 在 `bf28531` 中已经专门做了原子区间裁剪——**两条裁剪路径的
强度不一致，反应式这条没跟上**。

**实测**（`.venv`，构造 agent_loop 真实形态：每轮「模型说明文本 → function_call → function_response」）：

```
[反应式截断 _truncate_contents] 31 → 10 条
  孤立 id = ['c6']
  首条保留 = 孤立 function_response(c6)

[主动 MessageBudget]            31 → 2 条
  孤立 id = []
```

该孤立帧经 ADK 转换后是：

```python
{'role': 'tool', 'tool_call_id': 'c6', 'content': '{"hits": []}'}
```

即消息序列以一条没有前置 `assistant.tool_calls` 的 `tool` 消息开头。OpenAI 兼容端点
（含 DashScope compatible-mode）对此返回 400。

**失败场景**：多轮工具调用后上下文超长 → 触发 `CONTEXT_OVERFLOW` 分支 → 截断后重试 →
**重试请求因消息序列非法再次失败**，且第二次调用在 `try` 之外（`hardened_litellm.py:73-74`），
异常不再被捕获 → 直接上抛 → `merge_runner_events` 转成 `error` SSE 事件，整轮对话中断。
也就是说：这个"救命"重试，在最需要它的多轮工具场景下反而大概率失效。

**建议**：复用 `agent/engine/agent_loop/message_budget.py` 的 `_atomic_content_ranges()`
计算保留起点；最低成本的修法是从尾部向前找到第一个"不以 `function_response` 开头"的 content 作为切点。
顺带建议给第二次 `generate_content_async` 也加异常兜底，避免重试失败时的裸抛。

#### M2. 多次 `knowledge_search` 时引用编号串号，citation 张冠李戴

**位置**：`agent/tools/knowledge_search.py:38-42` + `agent/citation/citation_injector.py:24-30`

`knowledge_search` 每次调用都把命中重新编号为 `n = 1..k`；`CitationInjector.register_hits`
按 `n` 写入 `_id_to_doc`，后一次检索**直接覆盖**前一次的映射。多跳检索（agent_loop 的典型形态、
以及 `kq-multidoc` 类用例）中，模型看到的是两份都从 `[1]` 开始的资料列表，正文里的 `[1]`
因此指代不同文档，而程序端只保留最后一次的映射。

**实测**（模拟两次检索 + 正文两处 `[1]`）：

```
最终 citation: {'refs': [{'n': 1, 'title': 'ADK Plugin 扩展点', 'doc_id': 'doc-adk'}]}
```

正文第一个 `[1]` 本应指向「RRF 融合原理」，但被静默错配到「ADK Plugin 扩展点」，
且第一次检索的文档完全从引用块中消失。

**失败场景**：跨文档问答中给出的引用块与正文标注不对应——**引用看起来存在、实则错误**，
比不给引用更有害。同时 `eval/harness` 的 `citation_precision` / `citation_recall`
也会因此失真（`cited_doc_ids` 取自 citation 事件）。

**建议**：编号必须在一次对话内全局单调。两个可选实现：
(a) `CitationInjector` 维护 offset，`register_hits` 时按 doc_id 去重、已出现过的文档复用旧编号；
(b) 更彻底——把编号权收到 `knowledge_search` 工具侧，用 `tool_context.state` 保存累计计数，
保证模型看到的序号本身就是全局唯一的（这也更贴近生产 ID 协议 CitationInjector 的做法）。

#### M3. 检索链路没有相关性阈值，`hits: []` 分支实际不可达

**位置**：`arag/store/vector_store.py:189-195`（无 min-score）+ `arag/components/filter.py:9-13`
（`min_score=0.0` 作用在恒为正的 RRF 分上）

向量分支 `argsort(-scores)[:top_k]` 无任何相似度下限，RRF 融合后的分数恒 > 0，
`low_value_filter` 的分数门形同虚设，`min_chars=2` 只挡空片段。结果是：**只要索引非空，
任何 query 都会拿到 top_k 条"资料"**。

**实测**（构造与库内内容正交、余弦相似度 0.0 的查询向量）：

```
正交查询向量的向量召回: [('c0', 0.0), ('c1', 0.0), ('c2', 0.0)]
RRF + low_value_filter 之后仍保留: [('c0', 0.0164), ('c1', 0.0161), ('c2', 0.0159)]
→ knowledge_search 返回 count=3，hits 非空，模型据此认为"检索到资料"
```

**为什么算问题**：`knowledge_search` 里那段精心写的"知识库未检索到相关资料，请明确告知未找到……"
的 note（`knowledge_search.py:45-47`），以及 README 宣称的"无命中不编造"，
在索引非空时**永远走不到**——它只在空库或 arag 宕机时才生效。相关性判断被完全下推给了 LLM。

值得说明的是：`eval/harness/scorers/rule_scorer.py:47-52` 的注释已经明确记录了这个现象
（"top-k 检索对任意 query 都会返回这 3 篇样本文档，故……不设硬门"）——**但这是在评测口径上
让步，检索层本身没有任何对应措施**。这条让步还削弱了 `no_fabrication` 套件的判别力。

**建议**：给向量分支加余弦下限（样本语料下 0.2~0.3 是合理起点），或在 `knowledge_search`
侧按分数带阈过滤；过滤后为空时走既有的降级 note 分支。改完后需要重跑 `no_fabrication` 和
`knowledge_qa` 两个套件，**并按项目既有纪律分别验证两代引擎**。

### 4.2 低

**L1. `plan_execute` 没有软收尾，熔断时以 `error` 事件收场。**
`execution_planner.py:56` 的 `hard_cap` 与 agent_loop 同值（`max_loop_iters + 2`），
但不挂 `LoopController`（`before_model` 为 no-op），因此没有 force-summary。触顶时
`LlmCallsLimitExceededError`（实测确为 `Exception` 子类）被 `merge_runner_events` 的 pump
捕获并转成 `error` SSE 事件——用户拿不到任何最终答案，而 agent_loop 在同样情况下会先被
force-summary 兜住。这是有意的设计取舍，但 `CLAUDE.md`/`RUNBOOK.md` 只写了"plan_execute
执行相也用同值作硬熔断"，没有点明失败形态的差异。建议补一句文档，或给 plan_execute 也接一个
只做 force-summary 的轻量 controller。

**L2. `ENGINE` 缺少启动期校验。**
`agent/config.py:21` 是裸 `str`，实测 `AgentSettings(engine='bogus_engine')` 可以构造成功，
直到请求期 `build_engine()` 才抛 `ValueError` → HTTP 500。对照 `sandbox_provider`
用了 `Literal["local","agentbay"]`、四个 `SKILL_*` 用了 `Field(gt=0)`，都是启动期 fail-fast。
建议统一为 `Literal["plan_execute", "agent_loop"]`。

**L3. NDJSON 流用了 `text/event-stream` 的 content-type。**
`skillcenter/api.py:46` 的 `/execute-streaming` 返回的是 NDJSON（每行一个 `SkillResultDTO`），
但声明为 `media_type="text/event-stream"`。客户端按行解析不受影响，但对外描述与实际契约不符，
中间代理/浏览器按 SSE 语义处理时行为也可能不同。建议改为 `application/x-ndjson`。

**L4. `SSEStreamProcessor` 的字节缓冲在损坏流下只增不减。**
`stream_processor.py:32-37`：`UnicodeDecodeError` 且 `exc.start == 0` 时直接返回空串且不消费
`byte_buffer`；`max_buffer_size` 只保护 str 层的 `self.buffer`。生产者发出真正非法（而非跨 chunk 截断）
的 UTF-8 时，`byte_buffer` 会持续增长。当前生产者受控，记录知悉即可；如要加固可给 byte 层也加上限。

**L5. eval 的 `_passed()` 不看 `had_error` / `transport_error` / `finished`。**
`eval/harness/runner.py:34-35` 只看 `route_ok + assert_ok + 无硬门`。只有 `robustness` 套件
在 `rule_scorer.py:53-57` 里对 `had_error`/`finished` 设了门。因此其他套件中
"流中途传输中断、但已产出的部分文本恰好命中断言"的用例会被记为 PASS。建议把
`transport_error` 和 `finished` 提升为全局硬门（这类中断本来就应该显式暴露）。

**L6. eval 的 TTFT 统计混入了 `0.0`。**
`sse_client.py:37-38` 只在首个 `text` 事件设置 `ttft_ms`；无文本输出的 run
（卡片直呈 `skipSummarization`、错误用例）保持 `0.0`，而 `report.py:87-88` 直接把它们
喂给 `_pct` → p50/p95 被系统性拉低。建议只对有文本的 run 统计 TTFT，并单独报告
"无文本 run" 的计数。

**L7. `toolset.read_file` 有一处死分支。**
`agent/claude_skill/toolset.py:79`：`return state.guard_instruction_first("read_file") or {}`。
进入该行的前提已经是 `not state.instruction_ready`，此时 `guard_instruction_first` 必返回 dict，
`or {}` 永远不会命中。纯整洁问题，删掉更清楚。

**L8. `LocalVectorStore` 在事件循环里做同步全量落盘，且 `add()` 无并发保护。**
`vector_store.py:133-159` 的 `_persist()` 每次 `add` 都全量重写 `chunks.json` + `vectors.npy`，
且是同步 I/O。当前之所以不出并发问题，靠的是 `add()` 内部没有 `await`（隐式串行）——
这是个脆弱的不变量，一旦后续在 `add` 里加入任何 await 就会出现交错写。
语料变大时同步落盘也会阻塞 arag 的其他请求（包括正在进行的检索）。
建议：`asyncio.to_thread` 包落盘 + 一把 `asyncio.Lock` 保护 `add`。

**L9. `Chunker` 对超长单句不做硬切。**
`arag/components/chunker.py:47-51`：段落超过 `max_chars` 时按句拆，但单句本身超过预算时
`buf` 会无限增长，形成远超 500 字符的 chunk（无标点的长文本、代码块、表格都可能触发），
上游 embedding 接口有 token 上限时会直接报错。建议在句级再加一层按 `max_chars` 的硬切兜底。

### 4.3 复核为「非问题」的几项（避免后续重复排查）

以下几处在审读时被列为可疑，实测后确认无问题，记录以免重复投入：

- **`calculator` 的 `**` 幂运算不存在大整数 CPU 放大**：`_safe_eval` 把所有常量转为 `float`，
  `9**9**9` 立即触发 `OverflowError` 并被捕获为结构化错误，实测耗时 0.0s。
- **`SelectedSkillTool` 无 `input_schema` 时不会崩**：`parameters_json_schema=None` 经
  ADK 兜底为 `{"type":"object","properties":{}}`（见 §2.1）。
- **Web UI 无 XSS 面**：`web/app.js` 全程使用 `textContent` / `createElement`，
  无 `innerHTML` / `insertAdjacentHTML`，模型可控文本不会被当作 HTML 解析。
- **`.env.example` 的行内注释不会污染配置**：python-dotenv 正确剥离，
  `cp .env.example .env` 后可直接启动。
- **`_detect_error_in_response` 不是死代码**：ADK 2.6.2 仍通过 `getattr` 动态调用。

---

## 5. 后续建议

1. **优先级排序**：M1（截断配对）与 M2（引用串号）是纯代码缺陷、改动面小、收益直接，建议先修；
   M3（检索阈值）涉及召回口径变化，改完必须重跑评测，建议单独一轮。
2. **修 M3 后需重跑评测并分别验证两代引擎**——这是仓库已固化的纪律
   （`eval/reports/AB-prompt-v1.md` 证明同一改动可能让两代引擎收益相反），
   且记得先 `POST /v1/index/sample` 再跑。
3. **M2 修复后建议给 `eval/dataset` 补一个显式的多跳检索用例**（两次 `knowledge_search` +
   跨文档引用），把这个回归锁进评测，否则它只在真实多跳场景偶发暴露。
4. 前两轮评审遗留的两个真实评测场景仍未执行，建议择机人工补跑：
   **真实 SSE 客户端断开的全链路收口**、**真实 LLM 下两引擎运行相同 Claude Skill 用例的一致性**。
5. 若后续新增 Claude SKILL 技能包，建议补一个「`parallel_safe: false` + 声明 `exclusive_resources`」
   的样例，让串并行治理语义在演示层可见（沿用上一轮评审的建议）。

---

## 6. 本次评审的边界

- 未启动四个服务，未进行真实 LLM 调用与真实网络往返；所有实测均为离线的组件级/函数级验证。
- 未新增单元测试（遵循仓库既有约定，以 `py_compile` 为编译门）。
- 未修改任何代码；本文件是唯一产出。
- `eval/` 的数据集设计与评分口径只做了与本次发现直接相关的抽查（L5/L6），未做系统性评审。
