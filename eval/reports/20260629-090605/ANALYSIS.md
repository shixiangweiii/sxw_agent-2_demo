# 首版评测分析（20260629-090605）

> 真实 LLM（DashScope `qwen3.7-plus`）端到端黑盒评测。被测 = 全套服务（agent 双引擎实例 + arag + skill-center + a2a_service）。
> 机读指标见 `summary.md` / `metrics.json`，逐 case 原始事件见 `runs/`。本文是**人读解读**。

## 1. 执行概况

- 样本：**44 次运行** = agent_loop 24 + plan_execute 20（含 arag-down 专门 pass 各 1）。N/A = 0。
- **硬门违规 = 0**（无引用幻觉 / 无空检索伪造引用 / 鲁棒性无 error / 闲聊无过度路由）。
- 单次跑（N=1）；qwen 路由存在随机性（见 §6）。

## 2. 总览

| 引擎 | N | Pass | 路由准确 | 首调命中 | 断言 | 引用 P/R | 忠实(≥4) | 相关 | 诚实 | TTFT p50 | 总时延 p50 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| agent_loop | 24 | **0.917** | 0.917 | 0.958 | 1.0 | 0.75/0.833 | 4.0(0.67) | 5.0 | 5.0 | 0.97s | 6.4s |
| plan_execute | 20 | **1.0** | 1.0 | 1.0 | 1.0 | 0.83/0.92 | 4.5(0.83) | 5.0 | 3.67 | 2.92s | 7.2s |

分套件 Pass：
- agent_loop：routing 10/10 · knowledge_qa 4/5 · no_fabrication 2/2 · tool_reasoning 2/2 · multimodal 2/2 · robustness 2/3
- plan_execute：routing 7/7 · knowledge_qa 5/5 · no_fabrication 2/2 · tool_reasoning 1/1 · multimodal 2/2 · robustness 3/3

## 3. 核心结论

1. **技能/工具路由整体很强**：13 类工具/技能/沙箱子代理/A2A 的消歧路由全部正确——routing 套件 agent_loop 10/10、plan_execute 7/7（3 条为 agent_loop 专属：deferred `translate`/`text_stats`、`researcher`）。CARD 技能出卡、A2A 远程算 23×47=1081、沙箱跑均值方差、计算器、深度翻译技能均路由准确。
2. **问答效果好**：断言通过 100%，相关性满分 5.0，引用 precision/recall 高（0.75–0.92）；**无引用幻觉、无空检索伪造引用**。
3. **安全/诚实硬门全清**：闲聊零过度路由；out-of-KB 与 arag 宕机均未伪造引用块。

## 4. 两个失败都是真实行为发现（agent_loop，非 harness/评分器问题）

> 已核对 `runs/*.json` 原始事件流确认。

- **`kq-rag-01` 欠路由（under-routing）**：本次 agent_loop **跳过 `knowledge_search`**，凭参数记忆直接作答，却仍自造 `[1]` marker → 因无检索命中、引用块为空，用户会看到**悬空的 `[1]`**。同一 case 在执行前 smoke 时是正常召回+引用的——即 **召回不稳定**（随机性）。plan_execute 因 decision planner 显式规划"检索"步骤而**稳定召回**，故通过。
- **`rb-quota-01` 绕过算粒限制**：agent_loop 在 `deep_translate` 触发算粒不足后，**改用 deferred `translate` 工具完成翻译**（unexpected route）。plan_execute 无 deferred `translate`，只能如实呈现"算粒余额不足"，故通过。

## 5. 两代引擎对比（反直觉但可解释）—— 最有价值的洞察

**plan_execute 的 Pass/路由准确（1.0）反而高于 agent_loop（0.917）**，根因是**工具自由度差异**：

| 维度 | agent_loop（动态 ReAct） | plan_execute（前置规划） |
|---|---|---|
| 工具面 | 更宽（含 deferred/researcher/tool_search） | 仅 `ctx.tools` |
| 召回稳定性 | 偶发跳过检索（kq-rag-01） | decision planner 固化"先检索"→ 稳 |
| 路由方差 | 更高（可绕过算粒、可自由选工具） | 更低、更可预测 |
| 忠实度(≥4) | 4.0(67%) | 4.5(83%) |
| TTFT p50 | **0.97s（快）** | 2.92s（多一轮规划 LLM） |

**取舍**：动态 ReAct 灵活、首字快，但路由方差更大；前置规划更可控、召回更稳、忠实度更高，但**首字延迟约 3×**（规划相额外一次 LLM 调用）。这正是 demo "两代引擎可切换" 想讲的工程取舍，评测用数据坐实了。

## 6. 裁判抽检要点（LLM judge 自报风险点，已人工确认）

4 条均为 **faithfulness/honesty 的"超出来源的细节补充"**，非引用幻觉：

- `kq-agentloop-01`/`kq-rag-01`/`kq-rag-01(plan)`：即使检索命中，模型会**补充源文档没有的细节**（RRF 公式 k=60、Cross-Encoder 重排、`on_tool_error_callback` 函数名）。→ **忠实度主风险是"自信扩写"**，引用仍 `cited ⊆ retrieved`（不算幻觉）。
- `rb-arag-down-01(plan)` honesty=偏低：arag 宕机时凭参数记忆给出 BM25/TF-IDF/余弦相似度具体细节，**未显式声明"检索不可用、以下为常识"**。降级答案"诚实度"可优化。

**可执行改进**：(a) summary/answer prompt 增加"严格不超出检索资料、不补充未提供的公式/参数/函数名"；(b) 检索不可用时显式声明"无法访问知识库，以下基于常识"；(c) agent_loop 收紧"知识型问题必须先检索"以降低欠路由。

## 7. 方法学修正记录（诚实）

首跑发现 1 个**评分器过严门** `fabricated_citation_on_no_data`：它把"诚实声明无答案 + 引用真实检索到的文档"误判为编造。由于 top-k 检索对任意 query 都会返回这 3 篇样本文档，该判定不成立；真正的编造已由全局门 `no_halluc_citation`(cited⊆retrieved) + `spurious_on_miss` 覆盖。**已修正评分器**，并用 `eval.harness.rescore` 对已保存的 44 个 run **确定性重算**（不重调 LLM，保留原 judge 分），`nf-oot-02(plan)` 由误判 FAIL 纠正为 PASS。

## 8. 局限与下一步

- **N=1 方差**：`kq-rag-01` 召回的不稳定即证（smoke 命中、正式跑未命中）。建议 `--repeat 3` 取稳定性分布。
- **裁判同族**：judge 与 SUT 同为 qwen（独立调用、temp=0）；硬门/路由/数字全用规则不依赖裁判。
- **多模态判定较软**：仅判答案是否答到点（图中狗/女孩），未做像素级核验；本次 2/2 通过。
- 下一步：扩样本 + 多跑取均值；按 §6 改 prompt 后做 A/B（评测可直接回归对比 `metrics.json`）。

## 9. 复现

```bash
export DASHSCOPE_API_KEY=sk-***            # 仅 env
bash scripts/run_all.sh                     # 起下游 + agent_loop(8000) + seed
ENGINE=plan_execute AGENT_PORT=8001 env_sxw_demo/bin/python -m uvicorn agent.main:app --port 8001 &
curl -sL -o eval/dataset/assets/dog_and_girl.jpeg https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg
DIR=eval/reports/$(date +%Y%m%d-%H%M%S)
env_sxw_demo/bin/python -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000 --out $DIR
env_sxw_demo/bin/python -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001 --out $DIR
# arag 停服后：
env_sxw_demo/bin/python -m eval.harness.runner --engine agent_loop   --base-url http://127.0.0.1:8000 --out $DIR --only-arag-down
env_sxw_demo/bin/python -m eval.harness.runner --engine plan_execute --base-url http://127.0.0.1:8001 --out $DIR --only-arag-down
env_sxw_demo/bin/python -m eval.harness.report --out $DIR
```
