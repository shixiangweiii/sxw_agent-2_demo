# 真实 LLM 黑盒行为评测

`eval/` 评估三代引擎在路由、问答、引用、多模态、工具推理和降级方面的**行为表现**。Harness 只通过公开 Runtime API：创建 Run、读取 committed SSE、查询 terminal status，不调用引擎私有函数。

可靠性门禁在 `tests/reliability/`，以 fake/scripted 依赖验证事务、幂等、fencing、checkpoint、Artifact 和故障恢复。行为 PASS 不能代替可靠性 PASS；真实模型有随机性、成本和外部依赖。

## 1. 当前协议

每个 case 执行：

```text
可选图片 → POST /api/v1/artifacts → attachment_ref
→ POST /api/v1/runs（Idempotency-Key + per-Run engine）
→ GET /api/v1/runs/{run_id}/events?after_seq=N
→ 断流时按 last_seq 自动重连
→ GET /api/v1/runs/{run_id}
→ 以 committed terminal status 判定是否完成
```

Harness 采集的公开投影：

```text
text_start · text · tool_call · tool_result · plan_step · citation · skill_event · terminal
```

`CollectedRun` 保存 `run_id/terminal_status/last_seq`、文本、ToolCall/Result、citation、skill frame、plan step、TTFT、总时延、transport error 和 raw events。seq 是 opaque cursor；可见 seq 允许跳号。

Native 的 `text_start` 标识新 generation；retry/recovery/reactive compact 时只重置当前回答正文，工具和 Skill 过程投影保留。Harness 最终以 committed assistant message/terminal 为权威，不把旧 generation partial delta 拼成最终答案。

SSE 断开不取消 Run。客户端以 committed cursor 重连；最终还会 GET Run status，避免把传输层关闭误判成业务成功。

## 2. 前置条件

1. Python 3.12 和仓库 `.venv`；
2. `requirements.txt` 已安装；
3. 四服务五进程已启动：Runtime API + Worker、ARAG、skill-center、A2A；
4. 样本 index jobs 已到 `ACTIVATED`；
5. `eval/dataset/assets/dog_and_girl.jpeg` 存在；
6. 有效 `DASHSCOPE_API_KEY` 仅经真实环境变量注入。

推荐：

```bash
bash scripts/run_all.sh
export DASHSCOPE_API_KEY=sk-***
```

Preflight 会检查 ARAG、skill-center、A2A。用例要求的下游不满足时记为 N/A，不伪装 PASS/FAIL。Runtime API 与 Worker 本身若不可用，会记录 transport/terminal 信息；行为评分与可靠性裁决仍是两套口径。

## 3. 数据集

主数据集：`eval/dataset/cases.jsonl`，当前 24 个 case：

| suite | 数量 | 目的 |
|---|---:|---|
| `routing` | 10 | 必调、可接受、禁调、首个能力命中、过度路由 |
| `knowledge_qa` | 5 | 样本知识问答、关键点、引用 |
| `no_fabrication` | 2 | 无知识依据时诚实性与伪引用 |
| `tool_reasoning` | 2 | 多步 Tool 组合与计划 |
| `multimodal` | 2 | 图片 Artifact、多模态 + KB |
| `robustness` | 3 | quota、Tool failure、ARAG down |

Case 主要字段：

```json
{
  "id": "r-calc-01",
  "suite": "routing",
  "engines": ["agent_loop", "plan_execute"],
  "query": "...",
  "image": "assets/example.jpg",
  "preconditions": {"arag": "up", "skill_center": "any", "a2a": "up"},
  "expected_route": {
    "must_call": ["calculator"],
    "acceptable": [],
    "must_not_call": ["knowledge_search"]
  },
  "assertions": {
    "contains_all": [],
    "contains_any": [],
    "not_contains": [],
    "regex": []
  },
  "gold_citations": [],
  "judge": {"dims": ["relevance"], "context_from": "none"}
}
```

当前主数据集列出 `agent_loop` 24 项、`plan_execute` 20 项，没有声明 `native_loop` case。因此 Runner 虽支持 `--engine native_loop`，直接对当前文件执行会跳过全部；不得把这解释成 native 已评测或零失败。要评测 native，先显式把经过审查的 case 加入其 `engines` 列表，并单独报告。默认生产可靠性口径固定为 `native_early_tool_dispatch=off`；若显式评测 `experimental_heuristic`，必须在报告中单独标记，不能与 `off` 结果合并。

## 4. 评分

### 4.1 Routing scorer（确定性）

- `must_call` 是否全部命中；
- `must_not_call` 是否出现；
- `acceptable` 是否属于可接受替代；
- 第一个 capability Tool 是否命中允许集合；
- 无需能力的负例是否过度路由。

`capability_calls` 会过滤框架噪声，`all_tool_calls` 保留完整序列供排查。

### 4.2 Rule scorer（确定性）

- `contains_all / contains_any / not_contains / regex`；
- citation precision/recall；
- cited document 是否属于实际 retrieval；
- retrieval 空时是否产生伪 citation；
- robustness case 是否出现非成功 terminal 或未完成。

硬门包括 hallucinated citation、miss 上伪 citation、显式伪造引用块，以及 robustness 的 terminal/error 违规。

### 4.3 LLM-as-judge

按 case 选择：

- `faithfulness`：答案事实是否受检索资料支持；
- `relevance`：是否切题、完整；
- `honesty`：知识缺失/不可用时是否坦诚且不编造。

默认 judge 使用同一 DashScope endpoint、`temperature=0`、关闭 thinking。judge 错误记 `score=0`，但不会中断其余 case。可用 `--no-judge` 跳过，做低成本确定性回归。

### 4.4 当前行为 PASS 公式

```text
route_ok AND assert_ok AND no hard_gate_violations
```

`finished/had_error/transport_error/terminal_status` 会被保存；robustness suite 把相关异常纳入硬门。其他 suite 的 PASS 仍是行为口径，不应被引用为 Runtime 可靠性证明。事务与恢复结论只看 `tests/reliability`。

## 5. 一键执行

```bash
export DASHSCOPE_API_KEY=sk-***
bash eval/run_eval.sh
```

默认使用一个 Runtime URL（`http://127.0.0.1:8000`），先跑 `agent_loop`，再跑 `plan_execute`，两者在 CreateRun JSON 中选择，无需多个 Agent API 实例。

覆盖地址：

```bash
RUNTIME_URL=http://127.0.0.1:8000 bash eval/run_eval.sh
```

输出目录：`eval/reports/<YYYYMMDD-HHMMSS>/`。

## 6. 分步执行

```bash
PY=.venv/bin/python
OUT=eval/reports/manual-run

$PY -m eval.harness.runner \
  --engine agent_loop \
  --base-url http://127.0.0.1:8000 \
  --out "$OUT"

$PY -m eval.harness.runner \
  --engine plan_execute \
  --base-url http://127.0.0.1:8000 \
  --out "$OUT"

$PY -m eval.harness.report --out "$OUT"
```

Runner 逐 case 追加 `results.jsonl`，中途退出时已完成结果仍保留；再次向同一目录运行会继续追加，不自动去重。因此实验重跑应使用新目录，除非明确要把另一 engine/特殊 pass 聚合进同一实验。

### 单 suite

```bash
$PY -m eval.harness.runner \
  --engine agent_loop \
  --suite routing \
  --base-url http://127.0.0.1:8000 \
  --out eval/reports/routing-only
```

### 不调用 judge

```bash
$PY -m eval.harness.runner \
  --engine agent_loop \
  --no-judge \
  --out eval/reports/rules-only
```

### 降低随机方差

```bash
$PY -m eval.harness.runner \
  --engine agent_loop \
  --repeat 3 \
  --out eval/reports/repeat-3
```

`--repeat 3` 为每个 case 建三个独立 Run，输出 `.r0/.r1/.r2`，报告列出 pass 或路由不一致项。

### 自定义数据集

```bash
$PY -m eval.harness.runner \
  --engine native_loop \
  --dataset /absolute/path/native-cases.jsonl \
  --out eval/reports/native-explicit
```

只有数据集明确声明该 engine 的 case 会执行。不同工具面或不同断言口径的结果不能直接横向比较。

## 7. ARAG-down pass

该用例的 precondition 要求 ARAG 确实不可用，必须单独停服务：

```bash
kill "$(lsof -ti tcp:8100)"

$PY -m eval.harness.runner \
  --engine agent_loop \
  --base-url http://127.0.0.1:8000 \
  --out "$OUT" \
  --only-arag-down

$PY -m eval.harness.runner \
  --engine plan_execute \
  --base-url http://127.0.0.1:8000 \
  --out "$OUT" \
  --only-arag-down

$PY -m eval.harness.report --out "$OUT"
```

完成后重启 ARAG，并确认 sample jobs 已 `ACTIVATED` 再跑正常 pass。

## 8. 输出产物

```text
eval/reports/<run>/
  preflight.json
  results.jsonl
  metrics.json
  summary.md
  runs/<case>.<engine>[.rN].json
  traces/<case>.<engine>[.rN].json
```

- `runs/`：`CollectedRun` 原始投影，包括 run_id、terminal、cursor 和 raw committed events；
- `results.jsonl`：逐次评分，可增量恢复；
- `metrics.json`：按 engine/suite 聚合、硬门、时延、稳定性和 failure labels；
- `summary.md`：人读报告；
- `traces/`：best-effort summary 轨迹，不含必须的恢复事实；评测不得自动切到或复制 `full` 原文轨迹。

新架构中 admission trace、Worker attempt trace、SSE subscription trace 是独立生命周期。Harness 继续用 `x-trace-id` 做 best-effort 联查，但 durable `run_id/activity_id` 才是跨生命周期主键。未取到轨迹时标记 `no_trace`，不能改变行为评分或 Runtime 恢复。

## 9. 不重跑模型的重评分

```bash
$PY -m eval.harness.rescore --out eval/reports/<run>
$PY -m eval.harness.report  --out eval/reports/<run>
```

`rescore` 从 `runs/*.json` 重跑 routing/rule/failure-label，保留原 judge 结果，不进行网络调用。评分器口径变化时优先使用；若输入协议、模型输出或工具面变化，必须新跑实验。

## 10. Failure labels 与 trace

报告从 summary trace 提取：

- turn 数、Tool 序列、finish reason、token、TTFT；
- retrieval degraded；
- loop hard cap / tool error / no trace；
- native compact 等补充信号。

只使用各 engine 共有字段做横向归因；engine 特有标签只作解释，不参与公平比较。Trace 可以关闭、采样或缺失，不得用于判定 Run terminal。

轨迹的根是 `runtime.engine_attempt`（kind=`engine`），不再是单进程时代的 `chat.request`：执行搬进 Worker 后，一个 Run 没有单一的进程内请求作用域，重试会跨 Activity、跨进程甚至跨 Worker 重启。`finish_reason`/`ttft_ms`/`event_counts`/`had_error` 由 `CommittedEventSink` 的事件旁路写在它上面，三代引擎共享同一出口因而对等。有多次 attempt 时取最后一次作结论，`attempts` 字段记录次数。人工排查可直接开 Trace Console（`/trace-ui/?trace_id=<id>`）。

## 11. 历史报告解释

`eval/reports/` 中已有报告来自其生成时的源码、Prompt、工具面和协议。有些历史报告早于 Canonical Runtime 切换；它们是行为实验档案，不证明当前版本可靠性或当前分数。

既有 A/B 结果显示，同一 Prompt 攡动可能让两个 engine 产生相反收益。因此：

- Prompt、Tool catalog、loop control 改动必须分别跑两个 engine；
- 不把 `agent_loop` 数字套给 `plan_execute`；
- 当前无 native 主数据集数字，不宣称其质量优劣；
- 报告中真实模型版本、release fingerprint、数据集、重复次数与 Native early-dispatch mode 应一起记录；mode 是 release 语义的一部分。

## 12. 有效性威胁

- 24 个案例规模小，覆盖不等于统计代表性；
- SUT 与 judge 默认同模型，存在同源偏差；
- 外部模型、Skill/A2A、网络状态会造成方差；
- capability preflight 只证明 endpoint 可达，不证明每次调用正确；
- TTFT/总时延包含 Runtime 排队、SSE poll、模型和下游，适合端到端观察，不是纯模型 benchmark；
- `pass` 公式不是全局 transport/reliability gate；
- ADK 两代引擎不具备 mid-turn deterministic replay，行为 harness 也不验证它。

高风险结论应增加 `--repeat`、人工 spot-check 和独立 judge/数据集；可靠性结论应回到 fault-injection pytest。
