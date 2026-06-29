# A/B 回归：§6 Prompt 改进（baseline-r3 vs improved-r3，repeat=3）

> 对比 `eval/reports/baseline-r3/`（原 prompt）与 `eval/reports/improved-r3/`（§6 改进 prompt），
> 两者均**真实 LLM、知识库已入库（seeded）、每用例重复 3 次**。

## 0. 诚实记录：一次过程错误 + 纠正

首次跑 r3 时，我在 arag 重启后**忘记重新入库样本知识**，导致两轮 r3 都命中**空索引**（检索恒返回 0 条）→ 知识类指标全废（citation 0.0、faithfulness "资料为空"）。
排查 `runs/*.json` 的 `tool_result.hits=0` 定位到根因，**重新入库 + 校验 chunks 非空后重跑**，并固化纪律：**每次重启 arag 必须立即 `/v1/index/sample`**。本文所有数据来自重跑后的有效结果。
（这条"空索引假象"本身也是 eval 的价值：黑盒指标骤降时，先核验依赖服务状态，再下结论。）

## 1. §6 改了什么

| 项 | 改动 | 落点 |
|---|---|---|
| (a) 忠实度 | "严格基于检索资料，不得补充资料未提供的公式/参数/函数名/数字" | 两代引擎指令 |
| (b) 降级诚实 | "检索无资料/不可用时，开头显式声明『未能检索到/未能访问知识库，以下基于常识』" | 两代引擎指令 + `knowledge_search` 工具 note |
| (c) 防欠路由 | "知识型/事实型问题必须先检索，不得凭记忆直接作答" | agent_loop 指令 + decision_planner"第一步检索知识库" |

## 2. 总览对比（baseline-r3 → improved-r3）

| 引擎 | 指标 | baseline | improved | Δ |
|---|---|--:|--:|:--:|
| **plan_execute** | knowledge_qa pass | 15/15 | 15/15 | = |
| | faithfulness 均(≥4) | 4.5(0.83) | **5.0(1.0)** | ⬆ |
| | honesty 均(≥4) | 4.11(0.78) | **5.0(1.0)** | ⬆⬆ |
| | citation P/R | 0.83/0.94 | **0.92**/0.94 | ⬆ |
| | route 准确 | 0.967 | 0.95 | ≈ |
| **agent_loop** | knowledge_qa pass | **15/15** | 9/15 | ⬇⬇ |
| | faithfulness 均(≥4) | 4.5(0.83) | 3.5(0.67) | ⬇ |
| | citation P/R | 0.97/1.0 | 0.64/0.67 | ⬇⬇ |
| | honesty 均(≥4) | 5.0(1.0) | 4.56(0.89) | ⬇ |
| | route 准确 | 0.972 | 0.833 | ⬇ |

## 3. 决定性结论：**同一 prompt 改动对两代引擎效果相反（引擎特异的 prompt 敏感性）**

- **plan_execute：净增益，应采用。** 最大亮点是 §6(b) 降级诚实——`rb-arag-down` honesty **2.33→5.0**（baseline 在 arag 宕机时凭记忆给出 BM25/稠密检索等具体术语却不声明无依据；improved 明确声明"未能访问知识库"）。忠实度 `kq-rag` **3.0→5.0**、`kq-litellm` 4.0→5.0、citation `kq-litellm` 0.83→1.0、`kq-adk` 0.5→0.83。plan_execute 的"先规划"结构（decision_planner 固化"第一步检索知识库"）与强约束**协同**。
- **agent_loop：净回退，不应采用。** 加重的"必须检索/严格忠实"8 条指令反而让 `kq-rag`、`kq-multidoc` 在 3/3 重复中**跳过检索**（retr 1.0→0.0）→ 忠实度/引用塌陷（faith 4.5→3.5、citeP 0.97→0.64、KQ 15→9）。自由式 ReAct 对长指令更敏感，约束堆叠产生了反效果。
- **重要澄清**：有效 baseline（seeded）的 agent_loop **检索本来就很稳**（各 KQ 用例 retr=1.0），并非首版 repeat-1 担心的"系统性欠路由"——那次 `kq-rag` 单次跳过只是 N=1 噪声。**repeat-3 才看清真相**。

## 4. 据数据做的"选择性上线"决定（最终仓库状态）

| 文件 | 最终状态 | 依据 |
|---|---|---|
| `agent/engine/plan_execute/execution_planner.py` | **采用 improved** | plan_execute 忠实度/降级诚实净增益 |
| `agent/engine/plan_execute/decision_planner.py` | **采用 improved**（"第一步检索知识库"）| 同上 |
| `agent/tools/knowledge_search.py`（降级/空 note）| **采用 improved** | 降级诚实护栏，低风险；shared |
| `agent/engine/agent_loop/agent_loop_engine.py` | **回退 baseline** | improved 使 agent_loop KQ 15→9、faith 4.5→3.5 |

> 这是 A/B 的核心价值：**把"看起来合理"的 prompt 改动用数据验证后选择性上线**——只上对 plan_execute 验证有效的部分，回退对 agent_loop 验证有害的部分。
> 诚实边界：最终 agent_loop（baseline 指令）+ shared 工具 note(improved) 这一**组合未单独再测**；逻辑自洽（工具 note 只补充降级声明、不强加检索约束），建议后续做一轮确认跑。

## 5. 路由稳定性（repeat=3）

- **稳定**：绝大多数 routing 用例 3/3 路由一致（calculator/math_expert/query_weather/deep_translate/claude_skill/researcher/tool_search 等消歧路由稳定）。
- **不稳定（噪声为主，两版都有）**：`r-calc-01` 偶发不调 calculator 直接心算；`r-weather-card-01` 路由对（query_weather）但终文不含"杭州"（受 skipSummarization 直呈语义影响，属断言口径问题非路由问题）；`rb-quota-01` agent_loop 偶发用 deferred `translate` 绕过算粒。
- **agent_loop 知识检索敏感**：improved 版 `kq-rag`/`kq-multidoc` 稳定跳过（0/3），baseline 版稳定检索（3/3）——即 §3 的回退依据。

## 6. 局限与下一步

- N=3 仍有方差；总体 pass 率被非知识类噪声用例（r-calc/r-weather-card/rb-quota）干扰，**应以分维度指标（检索率/citation/faithfulness/honesty）而非单一 pass 率判读**（本文即如此）。
- 断言口径优化：weather-card 用例应断言 `skill_event(CARD)` 内容而非终文含城市名（受 skipSummarization 直呈影响）。
- 下一步：(1) 对 agent_loop 试**更轻的单条**忠实度约束（避免长指令堆叠）再 A/B；(2) 验证 §4 的 agent_loop 组合；(3) 关键 suite 提到 N≥5 降方差。

## 7. 复现

```bash
# 两版各自：seeded 主 pass + arag-down，均 --repeat 3（详见 eval/README §9 + run_eval.sh）
# 改 prompt → 重启对应 agent 实例 → 跑 → eval.harness.report；务必每次重启 arag 后 /v1/index/sample
```
