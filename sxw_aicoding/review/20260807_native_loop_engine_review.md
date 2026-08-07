# 代码评审：native_loop 自研 Tool-Use 循环引擎（Gen3）

- 评审时间：2026-08-07
- 评审对象：`main` @ `23590a2`「添加 cc 风格的 tool_use agent-loop」，28 文件 / +2820 −133
- 对照文档：`sxw_aicoding/changelog/20260807_native_loop_自研ToolUse循环引擎.md`
- 评审范围：
  1. 新增 `agent/engine/native_loop/`（10 个模块，约 2170 行）
  2. 新增 `agent/engine/loop_tools/`（共享工具面提取）、`agent/claude_skill/skill_drivers.py`
  3. 改动 `agent/config.py`、`agent/engine/base.py`、`agent_loop_engine.py`、`loop_processor.py`、
     `skill_runner.py`、`claude_skill_tool.py`、`eval/harness/runner.py`
  4. `scripts/probe_dashscope_tool_stream.py`、四份文档
- 评审方式：逐文件审读 +**每一条中等级别结论都用 `.venv` 写脚本实跑复现**（历史裁剪、压缩决策、
  SSE 收口契约、异常分类、累积器就绪判定、工具面对齐、四路合成、分批保序），并复跑 `py_compile` 门。
- 与既有评审的关系：`20260805_full_codebase_review.md` 覆盖了 ADK 2.6.2 基线；本次只评本笔提交
  引入/改动的部分，未回归复核 arag / skillcenter / a2a_service。

---

## 1. 总体结论

**通过，但有 4 个中等问题需要在跑评测之前修掉。**

这次改动的质量明显高于「再写一个引擎」的平均水准。最值得肯定的是**它没有把对比轴弄脏**：
`LOOP_INSTRUCTION` 被提取成单份常量、两代引擎 import 的是同一个对象（`is` 判定为真）；
工具面实测逐个一致（9 个工具，名称、描述、JSON Schema 全对齐，`tool_context` 未泄漏进模型可见参数）。
这意味着后续三代引擎的评测数字确实能归因到「循环归谁驱动」，而不是 prompt 或工具集的差异——
这是整个项目最核心的论点，也是最容易被实现细节悄悄破坏的地方。

`adk_bridge.py` 这条「方案写错了、实读 ADK 源码后改正」的记录尤其可信：`AgentTool.run_async`
硬依赖 `tool_context._invocation_context` 确实是鸭子类型 shim 兜不住的，而且失败形态是**静默的**
（被 executor 兜成 `is_error`，表现为"该工具永远失败"）。发现它并补上自建子 Runner，
比方案本身更有价值。

四个中等问题有一个共同特征，和上次全量评审的结论惊人一致：**它们全都在恢复/兜底路径上，
而恢复路径恰恰是本次改动对外主打的差异化能力**（CC 不变量第 7 条"恢复优先于失败"）。
主链路（模型→工具→再模型→出答案）实测干净；出问题的是"上下文超长怎么恢复"和"失败怎么收口"。

另外，变更文档的**诚实边界写得准确**，没有把桩写成能力：`native_loop` 未评测、compact 阈值在
无 usage 时是字符估算、ADK 仍是库依赖、A2A 不随 `SUB_AGENT_ENGINE` 切换——逐条与代码对得上。
提交里没有密钥泄漏，探针脚本只从环境变量读且不打印密钥。

---

## 2. 正面结论（已实跑验证）

| 验证项 | 方式 | 结果 |
|---|---|---|
| 两代引擎系统指令同源 | `agent_loop._LOOP_INSTRUCTION is loop_tools.LOOP_INSTRUCTION` | ✅ True，prompt 无法漂移 |
| 工具面逐个一致 | 离线构造 `AgentContext`，分别取 `_collect_tools`+`build_registry` 与 `build_loop_agent().tools` | ✅ 两侧均为同样 9 个工具；`ENGINE=agent_loop` / `native_loop` 两种解析下都一致 |
| Schema 生成正确性 | 打印 `knowledge_search` / `update_task_plan` / `researcher` 的 parameters | ✅ 类型、`required`、docstring Args 描述全部正确；`tool_context` 未出现在 properties |
| `_READ_ONLY_TOOLS` 真的命中 | 打印 registry 的 concurrency_safe 分组 | ✅ 并发组 = calculator/get_weather/knowledge_search/text_stats/tool_search/translate；`update_task_plan`、`researcher`、`simulate_unstable_operation` 串行 |
| 四条合成路径 | 逐条构造工具不存在 / 顶层数组 / 坏 JSON / 工具抛异常 / 取消 | ✅ 5 种全部产出 `role=tool` 且 `tool_call_id` 配对，`execute_one` 未抛异常 |
| 分批与保序 | `partition` + `run_calls` 跑 `[只读,只读,写状态,只读]` | ✅ 批次为 `[并发2] [串行1] [并发1]`，产出顺序仍为 1→2→3→4 |
| preserved tail 落在单元边界 | `atomic_units` + `_preserved_start` 对含 call/response 的历史取各档位 | ✅ 起点始终是单元 start，不会留下孤立 tool |
| `NativeToolContext` 覆盖既有 getattr | 比对 `call_identity.py`（`function_call_id`/`invocation_id`）与 `task_plan_tool`（`.state`） | ✅ 三处需求全覆盖，现有工具零改动 |
| 编译门 | `find agent arag common skillcenter a2a_service -name '*.py' \| xargs py_compile` | ✅ 通过 |
| 密钥扫描 | `git show HEAD \| grep -E 'sk-[A-Za-z0-9]{10,}'` | ✅ 无命中 |
| `git mv` 是否被识别为纯改名 | `git show --stat` | ✅ `{agent_loop => loop_tools}/*.py \| 0`，三个文件零 diff |

`skill_runner.py` 的策略外壳/内核拆分也做得干净：沙箱装载、两次 attempt、instruction-first 合规判定、
错误码分流、双层取消清理全部留在外壳且逐行未动；两个 driver 共用同一失败信号
（都抛 ADK 的 `LlmCallsLimitExceededError`），所以外壳不需要区分内核类型。这个抽象边界选得对。

---

## 3. 中等问题（建议在跑 `native_loop` 评测之前处理）

### M1　`HistoryStore` 的 400 条上限裁剪会切开 call/response，导致会话**永久性** 400

`agent/engine/native_loop/history.py:40-42`：

```python
if len(stored) > self._max_messages:
    dropped = len(stored) - self._max_messages
    stored = stored[dropped:]          # ← 纯按条数切，不看原子单元
```

整个 native_loop 对「不能从 call/response 中间切开」这条协议约束是极其克制的——`compact` 走
`atomic_units`、`apply_tool_result_budget` 刻意只改正文不删消息、`_fill_missing_results` 在取消时
补配对。唯独这最后一道兜底闸是裸切片。

**实跑复现**（`max_messages_per_session=4`，历史为 `user / assistant(tool_calls) / tool / user / assistant(tool_calls) / tool`）：

```
裁剪后前 2 条 role: ['tool', 'user']
首条是孤立 tool 消息: True
同一段历史的原子单元: [(0,0), (1,2), (3,3), (4,5)]
compact 的 preserved 起点（preserve_units=2）: 3   ← 对照：落在单元边界
```

**后果**：`messages_after_boundary` 在无摘要时返回全量，请求首条即 `role=tool` 而其前没有
`tool_calls`，OpenAI 兼容上游直接判 400。而且这不是一次性失败——被裁剪的历史已经写回 store，
**该会话之后每一轮都会失败**，且 `classify_llm_error` 会把它归为 `other`（不是 overflow），
反应式压缩救不回来。

**可达性**：需要在不触发压缩的前提下累积到 400 条。阈值是 115k token ≈ 172k 字符，
400 条小消息（计算器/天气这类）平均 430 字符以下就够不着阈值，长会话演示中可达。
与 M2 叠加时更容易：M2 会把 `compact_failures` 置 1，把本请求的主动压缩永久关掉。

**建议**：`replace` 里改用 `atomic_units` 找到第一个 `start >= dropped` 的单元起点再切，
与 `compact._preserved_start` 复用同一套逻辑。

---

### M2　反应式压缩后 `last_usage` 未失效，紧接着触发一次**冗余的二次压缩**

`agent/engine/native_loop/loop.py:282-307` 反应式压缩成功后 `state.iters -= 1; continue`，
回到循环顶部第一件事就是 `_maybe_proactive_compact(state)`（`loop.py:140`）。
但 `state.last_usage` 仍然是**压缩前那次请求**的 `prompt_tokens`，而
`compact.estimate_tokens` 取的是 `max(usage.prompt_tokens, 字符估算)`（`compact.py:76-82`）——
压缩把字符估算打下去了，旧 usage 却把它顶了回来。

**实跑复现**（12 个原子单元、`preserve_units=6`、上一次 usage `prompt_tokens=120000`）：

```
压缩前 decide: should=True  tokens=120000 threshold=115000 estimated=False
压缩后消息数: 7（摘要 1 + 保留尾部 6）
压缩后仍用旧 usage 的 decide: should=True  tokens=120000   ← 立刻又要压一次
同一份历史、清空 usage 后的 decide: should=False tokens=3256
第二次压缩结果: 又摘要了一次，摘要被二次摘要，尾部细节丢失
摘要 LLM 调用次数: 2
```

**后果**（按发生顺序）：
1. 多花一次摘要 LLM 调用（`max_tokens=4096`，不便宜）；
2. **摘要被二次摘要**——第一次的 boundary 消息成了第二次的 `to_summarize`，早期信息经过两轮有损压缩；
3. 若压缩后单元数 ≤ `preserve_units`，第二次 `compact()` 返回 `None` → `compact_failures += 1`
   → `_maybe_proactive_compact` 的 `state.compact_failures > 0` 守卫把**本请求剩余全部主动压缩永久关闭**。
   刚从超长里恢复出来就把压缩关掉，方向正好反了。

**建议**：`_maybe_proactive_compact` / `_reactive_compact` 压缩成功后 `state.last_usage = None`
（下一次模型调用的 TurnEnd 会立刻填回真值）。一行改动，两条路径都覆盖。
顺带建议把 `compact_failures` 的语义从"永久关闭"改成"跳过 N 轮"，否则一次偶发失败会
让长会话在剩余时间里彻底失去压缩能力。

---

### M3　error 收口路径不发 `done`，与另外两代引擎的 SSE 契约不一致

`loop.py` 的三条失败收口（`hard_cap` / `ContextOverflowError` 不可恢复 / `NativeLlmError`）
都是 `yield StreamEvent("error", ...)` 后直接 `return`，`engine.py:128-135` 之后也不补。
而 `agent_loop_engine.py:117` 和 `plan_execute_engine.py:40` 都是**无条件** `yield done`——
`agent_loop` 甚至专门靠 `merge_runner_events` 把 Runner 异常转成 error 事件，就是为了保证仍能走到那行 done。

**实跑复现**（假模型驱动 `NativeLoop`）：

```
① 正常收口 : ['tool_call', 'tool_result', 'text', 'done']  stop_reason=completed
② 硬熔断   : [..., 'tool_result', 'error']                  stop_reason=hard_cap   ← 无 done
③ 模型报错 : ['error']                                       stop_reason=model_error ← 无 done
```

**后果**：
- `citation_injector.with_citations` 只在收到 `done` 时扫描正文并注入 citation
  （`citation_injector.py:70-78`）。硬熔断场景下模型往往**已经产出了带 `[n]` 的正文**
  （force-summary 就是逼它先答再停），此时引用块会整块丢失。
- 评测 harness 只在 `done` 时置 `finished=True`（`sse_client.py:50`），`runner.py:92` 会把
  `native_loop` 的熔断样本记成 `finished=false`。等第三个实例（:8002）跑基线时，
  这会直接影响与 `agent_loop` 的可比性——同样的熔断，两代引擎在报告里的字段不一样。

**建议**：三条失败路径在 `yield error` 之后补 `yield StreamEvent("done", {"finish_reason": <transition>})`，
或者更干净的做法——把 `_finish()` 改成同时负责发 `done`，让所有出口只有一条收口路径。

---

### M4　`classify_llm_error` 在 native 路径下退化为纯英文子串匹配，「恢复优先于失败」可能在默认 provider 上根本不生效

`native_loop` 直接用 `openai` SDK，抛出的是 `openai.*Error`；而 `agent/llm/exceptions.py`
的前两个分支是 `isinstance(exc, litellm.ContextWindowExceededError)` / `litellm.RateLimitError`。
litellm 的异常类**继承自** openai 的，反向不成立，所以这两个 isinstance 分支在 native 路径下恒为假，
判定完全落到 `_OVERFLOW_KEYS` 这组英文子串上。

**实跑复现**：

```
openai.BadRequestError 是 litellm.ContextWindowExceededError 的实例: False
classify（OpenAI 风格 "maximum context length is 128000 tokens"）: context_overflow  ✅
classify（DashScope 风格 "Range of input length should be [1, 129024]"）: other      ❌
```

第二条文案不含任何一个 `_OVERFLOW_KEYS` 关键词，会被归为 `other` → 走
`except NativeLlmError` → 直接 `yield error` 收口，**反应式压缩 / 413 恢复整条链路一次都不会被触发**。
CC 不变量第 7 条、`T_REACTIVE_COMPACT` transition、`_reactive_compact` 的单次守卫——
在默认 provider（DashScope/Qwen）上可能全是死代码。

需要说明：DashScope 超长时的**确切**错误文案我无法在无密钥环境下确认，上面第二条是常见形态的示例，
不是实测报文。但**不确定本身就是问题**——这条恢复链路是本次改动的四大卖点之一，
它的生效与否目前完全押在一组未经该 provider 验证的英文关键词上。

**建议**：
1. 在 P0 探针里加一个 case：故意发一段超长 prompt，把上游 400 的原始报文打出来（这正是探针该做的事，
   成本几乎为零）；
2. 在此之前，先补一条 provider 无关的兜底判据——例如 `openai.BadRequestError` +
   请求消息字符数已超过 `context_window_tokens * _CHARS_PER_TOKEN` 的一定比例时，
   直接按 overflow 处理；
3. 变更文档「413 恢复：超长 → 压缩 → 重来一轮」的已验证条目建议注明"用注入异常验证，
   真实 provider 的错误文案未确认"。

---

## 4. 低优先级问题

**L1　`drive_native` 每次 Claude SKILL 调用都新建一个不会被关闭的 `NativeLlmClient`**
（`skill_drivers.py:136-138`）。主引擎把它放在 `NativeRuntime` 进程级单例里正是因为
"这些重对象不能跟着请求一起重建"（`engine.py:6-8` 自己的注释），子 Runner 这里破了例。
`openai.AsyncOpenAI` 内含 httpx 连接池，没有 `__del__`，不显式 `close()` 就不归还。
建议复用主引擎的单例，或至少在 `drive_native` 里 `async with`。

**L2　`NativeLlmClient._consume` 未显式关闭 openai 流**（`llm_client.py:230`）。
实测 `openai.AsyncStream` 有 `__aenter__` / `close()` 但**没有 `__del__`**，
客户端断开导致 `CancelledError` 穿过 `_consume` 时，底层 HTTP 响应不会被归还给连接池。
建议改 `async with await self._client.chat.completions.create(**payload) as stream:`。

**L3　`loop.py` 自己重写了 `_has_open_steps` 和 `TASK_PLAN_KEY`**
（`loop.py:50-51, 420-431`），而 `loop_tools/task_plan_tool.py:10, 32-41` 已经导出了同名同逻辑的版本，
且 native_loop 本来就 import 了这个模块（`engine.py:17`）。实测两者结果一致但是**两个独立函数对象**。
本次提取 `loop_tools/` 的动机就是"杜绝漂移"，这里等于在同一个包里又开了一份副本。建议直接 import。

**L4　每轮的最后一个 tool_call 永不提前投递，单工具调用轮里"流式工具执行"是 no-op。**
`_ToolCallAccumulator.take_ready` 要求 `index < self._max_index`（`llm_client.py:138`），
实测：

```
单个 tool_call、参数已完整时 take_ready: []        → 要等 take_remaining
出现更高 index 后 take_ready: ['knowledge_search'] → 首个被提前投递
```

这个设计本身是对的（半截 JSON 不能投递，"出现更高 index"是唯一可靠的完整性信号），
但 README/RUNBOOK 里"tool_call 一到就开跑，不等模型流结束"的口径需要收紧为
"一轮内除最后一个之外的 tool_call 可提前开跑"——单工具调用轮是绝大多数场景，那里没有任何延迟收益。

**L5　`.env.example` 未同步。** 仍是 `ENGINE=agent_loop  # plan_execute | agent_loop`，
7 个新变量（`SUB_AGENT_ENGINE` / `NATIVE_*` / `CONTEXT_WINDOW_TOKENS` / `COMPACT_*`）一个都没有。
RUNBOOK 5.2 的表格已经写全了，两处对不上。CLAUDE.md「跨文件保持一致」明确要求同步配置。

**L6　三处注释/文档与代码不符：**
- `agent/engine/base.py:3` 模块 docstring 仍写"两代实现：plan_execute / agent_loop"，
  而 `build_engine` 里已经有三个分支；`ReasoningEngine.run_stream` 上方注释也仍写"两代引擎"。
- `agent/context.py:29` 写 `chat: AgentChatClient  # 轻量单轮补全，仅供 plan_execute 规划相使用`，
  现在 compact 摘要（`engine.py:109`）和原生 researcher（`sub_agent.py:36`）都在用它。
- `messages.py:181-182` 写"边界之前的原始历史不再进入模型请求（但保留在本地数组里，便于排障）"，
  但 `compact.compact` 返回的是 `[boundary, *preserved]`，早期原文**已被丢弃**——
  `compact.py:223-224` 自己的注释是诚实的，两处矛盾。

**L7　`skill_runner.py:41` 的 `_APP = "claude-skill"` 重构后已是死代码**
（唯一使用者 `InMemoryRunner(app_name=...)` 已迁到 `skill_drivers.py`，那边另有一份同名常量）。

**L8　token 估算的两个方向偏差都存在，`estimated=true` 的口径可以再说清。**
`compact._char_tokens` 只统计 `state.messages`，不含 system 指令与 9~15 个工具的 JSON Schema
（后者按当前工具面就有数千 token）→ 系统性**低估**；同时 `_build_request` 的
`apply_tool_result_budget` 只作用于副本，历史里存的仍是全长 tool_result → 又**高估**。
两者不会互相抵消到可控范围。13k 的 buffer 大概率兜得住，但既然文档专门就"估算 vs 精确"
做了诚实声明，把这两个偏差来源也写进去会更完整。

**L9　`GeneratorExit` 路径不走 `except asyncio.CancelledError`，`early_tasks` 不被取消。**
若消费方是 `aclose()` 而非 cancel，`loop.run()` 在 `yield` 处收到的是 `GeneratorExit`（`BaseException`），
两个 `except asyncio.CancelledError` 都接不住 → `_cancel_tasks` / `_fill_missing_results` 均不执行，
提前投递的工具任务成为游离 task 继续跑（可能是技能沙箱子进程或 skill-center HTTP 调用）。
**实际链路下这条路走不到**：`merge_runner_events` 的队列是无界的，`await queue.put(se)` 不会挂起，
所以 pump 几乎总是挂在 `loop.run()` 内部的 await 上，`task.cancel()` 投递的是 `CancelledError`。
但依赖"队列恰好无界"来保证清理正确性是脆的，建议把 `except asyncio.CancelledError`
改成 `except BaseException` + 重新抛出，或者用 `try/finally` 收敛 `early_tasks` 的生命周期。

---

## 5. 与变更文档的对照

变更文档的自述与代码**基本逐条对得上**，这里只记需要调整的三处：

| 文档表述 | 实际情况 | 建议 |
|---|---|---|
| 「已验证 · 413 恢复：超长 → 压缩 → 重来一轮」 | 逻辑存在且用注入异常可跑通，但触发它的前提（`classify_llm_error` 判定为 overflow）在默认 provider 上未验证，见 M4 | 注明"用注入异常验证；真实 provider 的 400 文案未确认" |
| 「已验证 · SSE 事件类型覆盖 ADK 侧全集」 | 事件**类型**确实齐全，但**收口序列**与另外两代不同：error 路径无 `done`，见 M3 | 收口契约单列一条，修完再标已验证 |
| 「流式工具执行——tool_call 一到就开跑，不等模型流结束」（同样出现在 README / RUNBOOK / CLAUDE.md） | 一轮内最后一个 tool_call 必须等流结束，单工具轮无收益，见 L4 | 口径收紧为"一轮内除最后一个之外的 tool_call 可提前开跑" |

其余自述均属实：新增行数、`git mv` 纯改名、`adk_bridge` 的偏离说明、`SUB_AGENT_ENGINE`
不影响 A2A、`native_loop` 尚未评测、PromptCache/AgentBay/GraphStore 等既有边界未被夸大。

---

## 6. 建议处理顺序

1. **M2**（一行：压缩成功后清空 `last_usage`）—— 成本最低、直接影响压缩链路正确性。
2. **M3**（三条失败路径补 `done`，或把收口收敛进 `_finish`）—— 影响评测可比性，跑 :8002 基线**之前**必须修。
3. **M4**（先在 P0 探针里加超长 case 拿到真实报文，再决定兜底判据）—— 决定第 4 条卖点是否真的成立。
4. **M1**（`replace` 改用原子单元裁剪）—— 触发条件较窄但后果是会话永久损坏。
5. L1 / L2（两处资源未关闭）、L3（重复实现）、L5 / L6 / L7（文档与死代码）可合并成一次清理提交。
6. L4 / L8 是口径问题，建议和 M4 的探针结果一起改文档，一次改到位。

修完 M1~M4 之后，再按变更文档「后续可做」第 3 条起第三个实例跑基线——届时三代引擎的对比
才是在同一张考卷、同一套收口契约上做的。
