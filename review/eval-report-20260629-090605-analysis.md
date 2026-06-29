# 评测结果解读报告：20260629-090605

## 结论摘要

本次评测目录为 `eval/reports/20260629-090605`，模型为 `qwen3.7-plus`。整体结论是：系统主链路基本可靠，硬门全部通过，工具/技能路由能力较强；但 `agent_loop` 暴露了两个真实行为问题：知识问答偶发不检索直接回答，以及深度翻译算粒不足后绕路调用普通 `translate`。

总览指标：

| 引擎 | 样本数 | Pass 率 | 路由准确 | 首调命中 | 断言通过 | TTFT p50 | 总时延 p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `agent_loop` | 24 | 91.7% | 91.7% | 95.8% | 100% | 0.97s | 6.44s |
| `plan_execute` | 20 | 100% | 100% | 100% | 100% | 2.92s | 7.24s |

本次没有硬门违规，也没有闲聊/无须工具场景下的过度路由。

## 如何理解 Pass

评测 harness 的最终通过条件是：

```text
passed = route_ok && assert_ok && no hard_gate_violations
```

也就是说，LLM judge 给出的 faithfulness / honesty 低分不会直接导致 case fail，而是进入人工抽检清单。因此这次两个失败都属于路由行为问题，不是裁判主观扣分。

相关源码：

- `eval/harness/runner.py`：`_passed()` 定义最终通过条件。
- `eval/harness/scorers/routing_scorer.py`：判定漏调、错调、过度路由。
- `eval/harness/scorers/rule_scorer.py`：判定断言、引用准确性和硬门。

## 两个失败用例

### 1. `kq-rag-01 / agent_loop`：知识问题跳过检索

用例要求：

- 问题：`混合召回 RAG 是怎么把向量检索和全文检索两路结果合并的？`
- 预期必须调用：`knowledge_search`
- 期望引用：`kb-rag-pipeline`

实际行为：

- `agent_loop` 没有调用任何工具。
- 模型直接凭参数记忆回答了 RRF。
- 正文里出现了 `[1]`，但因为没有真实检索命中，最终没有 citation event。

这说明 `CitationInjector` 的保护逻辑是有效的：只有 `knowledge_search` 返回真实 hits 后，正文中的 `[n]` 才会被映射成 citation event；没有命中就不会编造引用块。但用户可见正文仍可能出现悬空 `[1]`，当前硬门没有捕捉这个体验问题。

相关源码：

- `eval/dataset/cases.jsonl`：`kq-rag-01` 要求必须调用 `knowledge_search`。
- `agent/citation/citation_injector.py`：只对真实检索命中的 `[n]` 生成 citation。
- `agent/engine/agent_loop/agent_loop_engine.py`：`agent_loop` 只用 prompt 约束“知识型问题前先检索”，没有 planner 强制步骤。

### 2. `rb-quota-01 / agent_loop`：算粒不足后绕路普通翻译

用例要求：

- 问题：`用深度翻译技能翻译这段文本：__quota__`
- 预期必须调用：`deep_translate`
- 不接受额外能力工具。

实际行为：

```text
tool_search -> deep_translate -> translate
```

`deep_translate` 对 `__quota__` 返回算粒不足，这是 skill-center 里的哨兵逻辑。随后 `agent_loop` 又调用了 deferred 普通翻译工具 `translate`，因此被判为 unexpected route。

这不是评分器误报，而是工具面差异导致的真实行为：`agent_loop` 额外注册了 `tool_search`、`translate`、`text_stats`、`researcher` 等动态能力；`plan_execute` 只使用 `ctx.tools`，没有 deferred `translate`，所以同一用例在 `plan_execute` 下通过。

相关源码：

- `skillcenter/skills.py`：`QUOTA_SENTINEL = "__quota__"`，触发算粒不足。
- `agent/engine/agent_loop/tool_search_tool.py`：定义 deferred `translate`。
- `agent/engine/agent_loop/agent_loop_engine.py`：把 `tool_search` 和 deferred tools 注册进 `agent_loop`。
- `agent/engine/plan_execute/execution_planner.py`：executor 只拿 `ctx.tools`。

## 两代引擎对比

`plan_execute` 本次看起来更稳，不是因为模型本身更强，而是因为编排结构更收敛：

- `decision_planner` 明确要求知识型/事实型问题第一步为“检索知识库”。
- `execution_planner` 明确要求不得凭记忆直接回答知识型问题。
- executor 工具面更窄，没有 `agent_loop` 的 deferred `translate` 绕路空间。

`agent_loop` 的优势是首字更快，TTFT p50 约 0.97s；`plan_execute` 多一轮规划 LLM 调用，TTFT p50 约 2.92s。这个结果很好地体现了项目里两代引擎的工程取舍：ReAct 单循环更灵活、更快，但方差更大；先规划再执行更慢，但可控性更强。

## 人工抽检清单怎么读

报告里的人工抽检主要指向“回答超出检索资料”的忠实性问题，而不是系统源码没有这些事实。

典型例子：

- judge 认为 `RRF 公式及 k=60` 对资料不忠实。
- 但源码 `arag/components/reranker.py` 确实实现了 `rrf_fuse(..., k=60)`。
- 这并不矛盾，因为评测口径测的是“知识库问答是否忠实于检索资料”，而不是“是否忠实于代码仓库”。

同理，`on_tool_error_callback` 在源码里真实存在，但样本知识库只写了 ToolErrorFeedback 会把异常封装成 `function_response`，没有写具体回调函数名；所以模型在知识库问答里说出函数名，会被判为超出资料。

## 报告元数据的一个坑

`summary.md` 中显示：

```text
能力预检：{'arag': False, 'skill_center': True, 'a2a': True}
```

这不能理解为整次评测都在 `arag` down 状态下执行。原因是：

- `results.jsonl` 是追加写入。
- `preflight.json` 每次 runner 调用都会覆盖。
- 本目录最后跑的是 `arag-down` 专门 pass，所以最终 `preflight.json` 显示 `arag=False`。

建议后续把 preflight 信息写入每条 result，或按 pass 保存为 `preflight.<engine>.<phase>.json`，避免复盘时误读。

## 与后续 repeat=3 报告的关系

`baseline-r3` / `improved-r3` 已经对本次 N=1 的部分判断做了校正：

- `agent_loop` 的 `kq-rag-01` 单次跳过检索更像方差暴露，不应仅凭一次判断为系统性缺陷。
- `rb-quota-01` 的绕路普通 `translate` 是更稳定的风险点。
- 对 `agent_loop` 加重“必须检索/严格忠实”prompt，在 repeat=3 中反而造成知识问答检索率下降，因此当前仓库选择回退 `agent_loop` 的强约束 prompt。
- 对 `plan_execute`，更强的检索与忠实约束是净增益，因此保留。

相关报告：`eval/reports/AB-prompt-v1.md`。

## 建议

1. 给悬空引用 marker 增加硬门：如果正文出现 `[n]` 但没有对应 citation event，应判为失败或至少进入 hard gate。
2. 明确 quota 场景策略：深度翻译算粒不足后，是否允许 fallback 到普通 `translate`。如果不允许，应在 `agent_loop` prompt 或工具元信息中明确“技能指定失败时只报告失败，不换路”。
3. 关键 suite 默认使用 `--repeat 3` 或更高，避免把 LLM 单次方差误读成架构问题。
4. 优化 preflight 记录方式，避免 `arag-down` 专门 pass 覆盖主 pass 的能力状态。
5. 对 `weather-card` 这类卡片直呈技能，断言应检查 `skill_event(CARD)`，而不是只检查最终文本。

