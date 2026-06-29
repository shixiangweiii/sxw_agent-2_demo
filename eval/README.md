# sxw_optimization_demo · 评测方案（Evaluation Plan）

> **本目录是评测「方案」，不是单元测试，也尚未执行。** 目标是设计一次**真实的端到端评测**：
> 用真实 LLM（DashScope `qwen3.7-plus`）驱动整套服务，从**用户视角**发请求、解析 SSE，量化
> **Agent 问答效果**与**技能/工具路由准确性**等核心指标。执行阶段据本方案实现 `eval/harness/` 并出报告。
>
> ⚠️ **密钥纪律**：`DASHSCOPE_API_KEY` 只经环境变量注入，**任何文件（数据集 / 提示词 / 报告 / 代码）都不得写入 Key**；`.env` 已被 `.gitignore` 忽略。

---

## 0. 目录

- [1. 评测目标与原则](#1-评测目标与原则)
- [2. 被测系统与可路由能力面](#2-被测系统与可路由能力面)
- [3. 评测维度与指标](#3-评测维度与指标)
- [4. 数据集设计](#4-数据集设计)
- [5. 评测框架（harness）架构](#5-评测框架harness架构)
- [6. 评分方法与门限](#6-评分方法与门限)
- [7. 实验矩阵与执行编排](#7-实验矩阵与执行编排)
- [8. 报告产物](#8-报告产物)
- [9. 执行 Runbook（执行阶段照做）](#9-执行-runbook执行阶段照做)
- [10. 有效性威胁与边界](#10-有效性威胁与边界)
- [11. 交付物清单与状态](#11-交付物清单与状态)

---

## 1. 评测目标与原则

**主目标（两条主线）**

1. **技能/工具路由准确性**：给定一句话，Agent 是否**调用了正确的工具/技能/子代理**（不漏调、不错调、不过度调用）。
2. **Agent 问答效果**：答案是否**切题、忠实于检索资料、诚实不杜撰、引用正确**。

**次目标**：多模态作答、鲁棒性/降级（检索宕机 / 算粒不足 / 工具异常喂回）、两代引擎（agent_loop vs plan_execute）对比、端到端时延。

**原则**

| # | 原则 | 含义 |
|---|---|---|
| E1 | **黑盒、端到端** | 只经对外入口 `POST /api/v1/chat/{uuid}/stream`，解析 SSE；不 mock 内部、不调私有函数（区别于单测）。 |
| E2 | **信号取自真实事件流** | 路由 = `tool_call` 事件；引用 = `citation` 事件；技能流 = `skill_event`；答案 = `text` 增量聚合。 |
| E3 | **规则为主、LLM 裁判为辅** | 可判定项（数字对错/必调工具/禁止编造引用）走确定性规则；忠实度/相关性等主观项用 LLM-as-judge，二者交叉。 |
| E4 | **接地（grounded）** | QA 题目与「黄金引用」严格对齐已入库的 3 篇样本知识；路由标签对齐源码真实工具名。 |
| E5 | **消歧优先** | 对天然存在路由二义的能力（天气、翻译、数学）用**措辞消歧**得到确定标签；保留 1 组「可接受集」题专门考察自然选择。 |
| E6 | **诚实安全是硬门** | 「检索 miss 却编造引用/事实」一票否决（与产品风险一致），不被平均分稀释。 |

---

## 2. 被测系统与可路由能力面

入口：`POST /api/v1/chat/{agent_uuid}/stream`（multipart：`query` / `user_id` / `session_id` / `image`）→ **SSE**。
SSE 事件：`text` · `tool_call{name,args}` · `tool_result{name,response}` · `plan_step` · `citation` · `skill_event` · `done` · `error`。

**可路由能力面（路由标签空间，取自源码真实工具名）**

| 工具名（`tool_call.name`） | 能力 | agent_loop | plan_execute | 来源 |
|---|---|:--:|:--:|---|
| `knowledge_search` | 知识库检索（RAG） | ✓ | ✓ | `tools/knowledge_search.py` |
| `calculator` | 算术计算 | ✓ | ✓ | `tools/builtin_tools.py` |
| `get_weather` | 天气（纯文本） | ✓ | ✓ | `tools/builtin_tools.py` |
| `simulate_unstable_operation` | 故障注入（ToolErrorFeedback 演示） | ✓ | ✓ | `tools/builtin_tools.py` |
| `query_weather` | 天气**卡片**技能（CARD+skipSummarization） | ✓¹ | ✓¹ | skill-center `deep_translate`/`query_weather` |
| `deep_translate` | 翻译技能（流式思考+增量） | ✓¹ | ✓¹ | skill-center |
| `claude_skill_data_analysis` | 数据分析**沙箱子代理** | ✓ | ✓ | `claude_skill/`（skill_id=`data_analysis`） |
| `math_expert` | A2A 远程数学子代理 | ✓² | ✓² | `a2a/loader.py` + `a2a_service` |
| `translate` | 翻译（deferred 工具） | ✓ | ✗ | `agent_loop/tool_search_tool.py` |
| `text_stats` | 文本统计（deferred 工具） | ✓ | ✗ | `agent_loop/tool_search_tool.py` |
| `tool_search` | 工具发现（渐进披露） | ✓ | ✗ | `agent_loop/tool_search_tool.py` |
| `update_task_plan` | 计划登记（驱动 `plan_step`） | ✓ | ✗ | `agent_loop/task_plan_tool.py` |
| `researcher` | 本地子代理委派 | ✓ | ✗ | `agent_loop/sub_agent_tool.py` |

¹ 依赖 skill-center 在线（启动时拉目录）；² 依赖 skill-center + a2a_service 在线。

**关键结论（直接影响评测设计）**：

- **引擎工具面不对称**：`plan_execute` 只用 `ctx.tools`；`translate/text_stats/tool_search/update_task_plan/researcher` 为 **agent_loop 专属**。→ 每条 case 标注 `engines` 适配范围，引擎对比只在**共享子集**上比。
- **天然路由二义**：天气 `get_weather`↔`query_weather`、翻译 `deep_translate`↔`translate`、数学 `calculator`↔`math_expert`↔`claude_skill_data_analysis`。→ 用消歧措辞固定标签 + 1 组可接受集题。
- **能力依赖在线下游**：技能/A2A 路由取决于下游是否起。→ harness 先做**能力预检（preflight）**，缺失下游的 case 标 `N/A` 不计分。

---

## 3. 评测维度与指标

### D1 · 技能/工具路由准确性（主）

从 SSE 收集有序的 `tool_call.name` 列表 `actual`；与 case 的 `expected_route`（`must_call` / `acceptable` / `must_not_call`）比对。

| 指标 | 定义 |
|---|---|
| **Route Accuracy** | `must_call ⊆ actual` 且 `actual ∩ must_not_call = ∅` 的 case 占比（核心数） |
| **First-capability Hit** | 首个「能力型」工具调用 ∈ 期望集 的占比（惩罚「乱试」） |
| **Routing Precision** | 正确工具调用次数 / 工具调用总次数（惩罚冗余调用） |
| **Routing Recall** | 命中的 `must_call` 数 / `must_call` 总数（惩罚漏调） |
| **Over-routing Rate** | 负例（闲聊）中触发了 `must_not_call` 工具的占比（应为 0） |
| **Under-routing Rate** | 知识型问题**未**调 `knowledge_search` 直接凭记忆作答的占比（groundedness 风险） |
| **混淆矩阵** | 在易混对（`get_weather`↔`query_weather`、`deep_translate`↔`translate`）上的错配分布 |

> 「能力型工具」= 排除 `tool_search`/`update_task_plan` 等编排辅助工具后的实际能力调用。

### D2 · 问答效果（主）

| 子轨 | 指标 | 方法 |
|---|---|---|
| Grounded QA | **关键点覆盖**（contains_any/all 命中率） | 规则 |
| Grounded QA | **忠实度 faithfulness**（1-5 + ≥4 达标率） | LLM-judge（CONTEXT=检索 hits） |
| Grounded QA | **相关性 relevance**（1-5） | LLM-judge |
| 诚实性 | **No-Fabrication 通过率**（硬门，目标 1.0） | 规则（禁止「引用文档」块 + 禁编造）+ judge honesty |
| 工具增强 | **最终答案正确性**（如含「35」+英文数词） | 规则 |

### D3 · 引用准确性（主）

从 `citation` 事件取引用文档集 `cited`；与检索 `tool_result.hits` 的 `doc_id` 集 `retrieved`、case `gold_citations` 比对。

| 指标 | 定义 |
|---|---|
| **Citation Precision** | `cited ∩ gold / cited`（错引为负） |
| **Citation Recall** | `cited ∩ gold / gold`（漏引为负） |
| **No-Hallucinated-Citation**（硬门） | `cited ⊆ retrieved` 恒成立（绝不引未检索到的文档），目标 1.0 |
| **No-Spurious-Citation on Miss**（硬门） | 检索空/降级时 `cited = ∅`，目标 1.0 |

### D4 · 多模态（次）
图片输入作答关键点命中（狗/女孩）+ relevance；且**不应**误走 `knowledge_search`。

### D5 · 鲁棒性 / 降级（次）
- **arag 宕机**：知识题降级 chat-mode，**无 `error` 事件**、**无伪造引用**。
- **算粒不足**：`__quota__` 哨兵 → 友好提示（含「算粒/管理员」），**无 `error` 事件**、`done` 正常收口。
- **工具异常喂回**：`simulate_unstable_operation(should_fail=true)` → turn 不中断、产出恢复说明、**无 `error` 事件**；**两代引擎都跑**（验证 plan_execute 已挂 `AgentInvocationPlugin` 的修复）。

### D6 · 引擎对比（横切）
共享子集上对 `agent_loop` / `plan_execute` 各跑一遍，对比 D1/D2/D3 + 时延，给 delta。

### D7 · 时延 / 可观测（次，仅报告不设门）
`ttft_ms`（首个 `text` 事件）、`total_ms`、`tool_call` 次数、`skill_event` 数、`error` 数。报告 p50/p95（N 小，仅作趋势）。

---

## 4. 数据集设计

文件：[`dataset/cases.jsonl`](dataset/cases.jsonl)（每行一条 case）。当前 **24 条**（20 条两引擎共享 + 4 条 agent_loop 专属），覆盖 6 个 suite：

| suite | 条数 | 考察点 |
|---|---|---|
| `routing` | 10 | 各能力消歧路由 + 闲聊负例 + 渐进披露/子代理（agent_loop 专属） |
| `knowledge_qa` | 5 | 单/双文档 grounded QA + 引用 |
| `no_fabrication` | 2 | 检索 miss 诚实性硬门 |
| `tool_reasoning` | 2 | 多步：计算→翻译；计划+统计（agent_loop） |
| `multimodal` | 2 | 图片输入作答 + 知识文档配图保留 |
| `robustness` | 3 | 算粒不足 / 工具异常喂回（两引擎）/ arag 宕机 |

**Case schema（字段说明）**

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | str | 唯一标识 |
| `suite` | str | 所属套件（上表） |
| `engines` | str[] | 适配引擎；引擎对比只在 `["agent_loop","plan_execute"]` 都含的 case 上比 |
| `query` | str | 用户输入 |
| `image` | str\|null | 多模态图片相对路径（`assets/...`），由 harness 上传 |
| `preconditions` | obj | 能力前置：`arag/skill_center/a2a ∈ {up,down,any}`；preflight 不满足则该 case `N/A` |
| `expected_route` | obj | `must_call[]`（必调）/ `acceptable[]`（可接受任一）/ `must_not_call[]`（禁调） |
| `assertions` | obj | 规则断言：`contains_all[]` / `contains_any[]` / `not_contains[]` / `regex[]`（对最终答案） |
| `gold_citations` | str[] | 期望引用的 `doc_id`（空=不应有引用） |
| `judge` | obj | `dims[]`（faithfulness/relevance/honesty）+ `context_from`（retrieval/image/none） |
| `notes` | str | 设计意图说明 |

**接地依据**：知识库 3 文档（`arag/sample_data.py`）—— `kb-adk-overview`（ADK 概念/LiteLlm）、`kb-rag-pipeline`（混合召回/RRF/改写）、`kb-agent-loop`（ReAct/ToolErrorFeedback，含一张配图）。QA 题与 `gold_citations` 均据此。

> **数据集是评测设计的核心产物**，执行前应人工评审一遍（题目是否公允、消歧是否到位、黄金引用是否准确）。

---

## 5. 评测框架（harness）架构

> 执行阶段在 `eval/harness/` 实现以下模块；本节是**实现规格**（含伪代码），当前不落可执行代码（遵守「先不执行」）。

```
eval/
├── README.md                  # 本方案
├── dataset/
│   ├── cases.jsonl            # 数据集（已就绪）
│   └── assets/                # 多模态资产（执行时下载 dog_and_girl.jpeg）
├── rubric/
│   └── judge-prompts.md       # LLM 裁判提示词（已就绪）
├── harness/                   # 【执行阶段实现】
│   ├── config.py              # 端点/引擎/裁判模型；Key 仅读 env
│   ├── preflight.py           # 能力预检：探活 arag/skill-center/a2a → 标 case N/A
│   ├── sse_client.py          # 发请求 + 解析 SSE → CollectedRun
│   ├── signals.py             # 从事件抽取路由/引用/答案/时延信号
│   ├── scorers/
│   │   ├── routing_scorer.py  # D1
│   │   ├── rule_scorer.py     # D2 规则 + D3 引用 + 硬门
│   │   └── judge_scorer.py    # D2 LLM 裁判（调 DashScope）
│   ├── runner.py              # 编排：cases × engine → 跑 → 评分 → 落盘
│   └── report.py              # 聚合 → summary.md / metrics.json / results.jsonl
├── run_eval.sh                # 【执行阶段】两引擎两 pass + 鲁棒性 pass
└── reports/<timestamp>/       # 输出
```

**`CollectedRun`（sse_client 产物，每次请求一份）**

```python
@dataclass
class CollectedRun:
    case_id: str
    engine: str
    text: str                       # 聚合后的最终答案
    tool_calls: list[tuple[str, dict]]   # [(name, args)]，有序
    tool_results: list[tuple[str, Any]]  # [(name, response)]
    citations: list[dict]           # citation 事件载荷（含 doc 列表）
    skill_events: list[dict]        # skill_event 载荷（dataType/isThinking...）
    had_error: bool                 # 是否出现 error 事件
    finished: bool                  # 是否收到 done
    ttft_ms: float                  # 首个 text 事件耗时
    total_ms: float                 # done 总耗时
    raw_events: list[dict]          # 全量事件（便于复盘）
```

**runner 主循环（伪代码）**

```python
caps = preflight(config)                      # {arag:up, skill_center:up, a2a:up}
for case in load_cases("dataset/cases.jsonl"):
    if config.engine not in case.engines:       continue
    if not preconditions_met(case, caps):       record(case, status="N/A"); continue
    run = sse_client.chat(config.base_url, case.query, image=case.image)  # 走当前引擎实例
    scores = {}
    scores |= routing_scorer.score(case, run)        # D1
    scores |= rule_scorer.score(case, run)           # D2 规则 + D3 引用 + 硬门
    if case.judge.dims:
        ctx = pick_context(case, run)                # retrieval hits / image 占位 / none
        scores |= judge_scorer.score(case, run, ctx) # D2 裁判
    record(case, run, scores, passed=gate(scores, case))
report.aggregate(records) -> reports/<ts>/
```

**信号抽取要点（signals.py）**

- 路由：`[name for (name,_) in run.tool_calls]`；能力型 = 过滤 `tool_search/update_task_plan`。
- 检索上下文：从 `run.tool_results` 取 `name=="knowledge_search"` 的 `response.hits`（即模型当时所见资料，喂给 faithfulness 裁判）。
- 引用集：`citations` 事件里的 doc 标识 → 与 hits 的 `doc_id` 对齐（按标题/doc_id 映射）。
- CARD/技能流：`skill_events` 里 `dataType==CARD` 判定 `query_weather` 是否真出卡片。

---

## 6. 评分方法与门限

**规则评分（确定性）**：`contains_all/any`、`not_contains`、`regex`、数字正确性、`must_call/acceptable/must_not_call`、`gold_citations` 比对。逐项布尔，聚合成 case 级 `rule_pass`。

**LLM 裁判**：见 [`rubric/judge-prompts.md`](rubric/judge-prompts.md)。`temperature=0`、JSON 输出、与规则交叉。

**硬门（任一不过 → case 直接判 FAIL，且单列"安全/诚实违规"清单，不被均分稀释）**

| 硬门 | 判定 |
|---|---|
| No-Hallucinated-Citation | `cited ⊆ retrieved` |
| No-Spurious-Citation on Miss | 检索空/降级 → `cited == ∅` 且答案无「引用文档」块 |
| No-Fabrication | `no_fabrication`/`arag-down` case：`not_contains` 命中 + judge honesty ≥ 4 |
| No-Crash | 鲁棒性 case：`had_error == False` 且 `finished == True` |
| No-Over-routing | 闲聊负例：`actual ∩ must_not_call == ∅` |

**参考门限（目标值，N 小不作刚性卡口，仅判健康度）**

| 指标 | 目标 |
|---|---|
| Route Accuracy（消歧题，共享工具） | ≥ 0.90 |
| First-capability Hit | ≥ 0.85 |
| Over-routing Rate（闲聊） | = 0 |
| Faithfulness 均分 / ≥4 达标率 | ≥ 4.0 / ≥ 0.8 |
| Citation Precision / Recall | ≥ 0.9 / ≥ 0.8 |
| 所有硬门 | 1.0（无违规） |

---

## 7. 实验矩阵与执行编排

**矩阵** = `引擎 {agent_loop, plan_execute}` × `case（按 engines 适配）`。

**引擎切换难点**：`ENGINE` 是服务端启动配置，单请求不可切。采用**双实例并行**（推荐，免重启、可同时跑）：

| 实例 | 命令（env） | 端口 |
|---|---|---|
| agent_loop | `ENGINE=agent_loop AGENT_PORT=8000 uvicorn agent.main:app --port 8000` | 8000 |
| plan_execute | `ENGINE=plan_execute AGENT_PORT=8001 uvicorn agent.main:app --port 8001` | 8001 |

harness `--engine agent_loop|plan_execute` 选对应端口，只跑 `engines` 含该引擎的 case。

**鲁棒性 `arag-down` pass**：单独一轮——**停掉 arag** 后只跑 `preconditions.arag=down` 的 case（其余 case 在 arag 在线的主 pass 跑）。

---

## 8. 报告产物

`reports/<timestamp>/`：

- `summary.md` —— 人读总表：各 suite × 引擎的 D1–D7 指标 + 硬门违规清单 + 引擎 delta + 人工抽检清单（judge 自报风险点）。
- `metrics.json` —— 机读聚合（便于趋势追踪/回归对比）。
- `results.jsonl` —— 每条 case 一行：`{id, engine, status, tool_calls, citations, answer, scores{...}, passed, hard_gate_violations[]}`。
- `runs/<case_id>.<engine>.json` —— 原始事件流（复盘用）。

`summary.md` 模板骨架：

```
# Eval Report <timestamp>  (model=qwen3.7-plus)
## Capability preflight: arag=up skill_center=up a2a=up
## D1 Routing      | engine | acc | first-hit | precision | recall | over-route |
## D2 QA           | engine | keypoint | faithfulness(avg/≥4) | relevance |
## D3 Citation     | engine | precision | recall | halluc=0? | spurious=0? |
## D5 Robustness   | case | no_error | finished | verdict |
## Hard-gate violations:  (空 = 全部通过)
## Engine delta (agent_loop − plan_execute): ...
## Manual spot-check queue: ...
```

---

## 9. 执行 Runbook（执行阶段照做）

```bash
cd sxw_optimization_demo
export DASHSCOPE_API_KEY=sk-***          # 仅 env，切勿写入任何文件

# 1) 起下游 + 双引擎实例 + 入库样本知识
bash scripts/run_all.sh                   # 起 a2a_service/skill-center/arag/agent(8000=agent_loop) + seed
ENGINE=plan_execute AGENT_PORT=8001 \
  env_sxw_demo/bin/python -m uvicorn agent.main:app --port 8001 &   # 第二实例

# 2) 准备多模态资产（知识样本里引用的 OSS 图）
curl -L -o eval/dataset/assets/dog_and_girl.jpeg \
  https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg

# 3) 主 pass（arag 在线）：两引擎各跑一遍
env_sxw_demo/bin/python -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000
env_sxw_demo/bin/python -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001

# 4) 鲁棒性 arag-down pass：停 arag，只跑 arag=down 子集
#    （kill arag 进程后）
env_sxw_demo/bin/python -m eval.harness.runner --engine agent_loop --suite robustness --only-arag-down

# 5) 出报告
env_sxw_demo/bin/python -m eval.harness.report --latest
open eval/reports/<timestamp>/summary.md
```

（`run_eval.sh` 在执行阶段封装上述 3–5 步。）

---

## 10. 有效性威胁与边界

- **样本小、单次跑方差大**：qwen 工具路由有随机性；缓解——裁判 `temperature=0`；建议关键 suite **跑 N=3 取多数/均值**（执行阶段加 `--repeat 3`）。报告标注 N。
- **同家族模型自评偏差**：裁判与 SUT 同为 qwen（独立调用）。缓解——硬门/数字/路由全用规则；裁判仅供「问答效果」参考分，且 judge 自报风险点入人工抽检。
- **下游可用性**：技能/A2A 路由依赖下游在线；preflight 标 `N/A`，不混入分母。
- **引擎工具面不对称**：引擎对比仅在共享子集；专属工具 case 单列。
- **多模态判定较软**：仅判答案是否答到点，不像素级核验。
- **非目标**：不评测吞吐/压测、不评测内部基建（Nacos/HSF/SLS 等已裁剪）、不做安全红队（仅诚实性/拒绝杜撰一项）。

---

## 11. 交付物清单与状态

| 产物 | 路径 | 状态 |
|---|---|---|
| 评测方案（本文档） | `eval/README.md` | ✅ 已就绪 |
| 数据集（24 case / 6 suite，接地真实工具名与知识库） | `eval/dataset/cases.jsonl` | ✅ 已就绪 |
| LLM 裁判提示词（faithfulness/relevance/honesty） | `eval/rubric/judge-prompts.md` | ✅ 已就绪 |
| 评测框架 harness（含 scorers/runner/report/rescore） | `eval/harness/*` | ✅ 已实现 |
| 一键执行脚本 | `eval/run_eval.sh` | ✅ 已实现 |
| 评测报告 | `eval/reports/20260629-090605/` | ✅ **首版已产出** |

> **首版报告**：`eval/reports/20260629-090605/`（`summary.md` + `ANALYSIS.md` + `metrics.json` + `results.jsonl` + `runs/`）。44 次运行、0 硬门违规、0 过度路由。
>
> **A/B 回归（§6 prompt 改进，repeat=3）**：`eval/reports/baseline-r3/`（原 prompt）vs `eval/reports/improved-r3/`（改进 prompt），对比解读见 **[`eval/reports/AB-prompt-v1.md`](reports/AB-prompt-v1.md)**。
> 关键结论：**同一改动对两代引擎效果相反**——plan_execute 净增益（faithfulness 4.5→5.0、降级 honesty 2.33→5.0、citation↑），agent_loop 净回退（KQ 15/15→9/15、faith 4.5→3.5）。据此**选择性上线**：plan_execute 采用改进、agent_loop 回退基线（最终仓库状态见 AB 文档 §4）。
>
> ⚠️ **执行纪律**：repeat≥2 用 `--repeat N`；**每次重启 arag 后必须 `POST /v1/index/sample` 重新入库**（本地存储为内存态，重启即清空——首跑曾因漏 reseed 命中空索引，详见 AB 文档 §0）。
