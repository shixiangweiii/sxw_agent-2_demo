# Agent 两引擎同卷对比评测报告

- 生成时间：2026-06-30T11:08:31
- 考卷：`eval/eval_docs/first_eval/first_eval_exam.json`
- 语料：`eval/eval_docs/generated_eval/index_payload.json`
- 运行目录：`eval/eval_docs/first_eval/runs/engine_compare_20260630-105422`
- 对比对象：`agent_loop` vs `plan_execute`
- 评分口径：复用首次评测的确定性规则；不使用 LLM-as-judge。

## 结论

- 严格同卷总分：`agent_loop 95.325/100`，`plan_execute 91.981/100`，`agent_loop` 领先 `3.344` 分。
- 两个引擎在简单题上完全持平，说明单跳事实检索不是差异来源。
- 中等题差异很小，主要来自多文档 citation 覆盖。
- 复杂题拉开差距：`agent_loop` 更容易在多跳题中保留完整答案要点和工具路线，`plan_execute` 在 C01/C08 上答案覆盖明显弱一些。
- C06 两个引擎都只调用了 `calculator`，没有先用 `knowledge_search` 绑定证据，是共同短板。

## 公平性说明

这份考卷是“同卷严格对比”，但它不是纯粹的编排算法隔离实验，因为当前代码里两个引擎的工具面不完全一致。
`tool_search`、`text_stats` 等是 `agent_loop` 专属工具，C07 明确要求这类外部工具能力；因此 C07 的严格分数同时反映了工具暴露差异和推理编排差异。
去掉 C07 后做可比子集归一化：`agent_loop 95.07/100`，`plan_execute 92.28/100`，差距 `2.79` 分。

## 总分与难度分布

| 指标 | agent_loop | plan_execute | 差值(agent_loop - plan_execute) |
|---|---:|---:|---:|
| 总分 | 95.325 | 91.981 | 3.344 |
| simple | 16.000/16.00 | 16.000/16.00 | 0.000 |
| medium | 41.700/42.00 | 41.550/42.00 | 0.150 |
| complex | 37.625/42.00 | 34.431/42.00 | 3.194 |

## 组件扣分对比

| 组件 | agent_loop 扣分题 | plan_execute 扣分题 |
|---|---|---|
| route | C06 | C06, C07 |
| retrieval | C06 | C06 |
| citation | M11, M12, C01, C02, C03, C06, C08 | M11, M12, M13, C01, C02, C03, C04, C06, C08 |
| answer | C08 | C01, C08 |
| safety_finish | 无 | 无 |

## 逐题差异

| ID | 难度 | 类型 | agent_loop | plan_execute | 差值 | agent_loop 工具 | plan_execute 工具 |
|---|---|---|---:|---:|---:|---|---|
| S01 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S02 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S03 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S04 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S05 | simple | image_recall | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S06 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S07 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| S08 | simple | single_fact | 2.000 | 2.000 | 0.000 | knowledge_search | knowledge_search |
| M01 | medium | single_doc_calc | 3.000 | 3.000 | 0.000 | knowledge_search, calculator | knowledge_search, calculator |
| M02 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M03 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M04 | medium | single_doc_calc | 3.000 | 3.000 | 0.000 | knowledge_search, calculator | knowledge_search, calculator |
| M05 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M06 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M07 | medium | single_doc_calc | 3.000 | 3.000 | 0.000 | calculator, knowledge_search | knowledge_search, calculator |
| M08 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M09 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M10 | medium | same_doc_reasoning | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| M11 | medium | two_doc_compare | 2.850 | 2.850 | 0.000 | knowledge_search, knowledge_search | knowledge_search, knowledge_search |
| M12 | medium | two_doc_compare | 2.850 | 2.850 | 0.000 | knowledge_search, knowledge_search | knowledge_search, knowledge_search |
| M13 | medium | image_recall_compare | 3.000 | 2.850 | 0.150 | knowledge_search | knowledge_search, knowledge_search |
| M14 | medium | same_doc_causal | 3.000 | 3.000 | 0.000 | knowledge_search | knowledge_search |
| C01 | complex | cross_theme_risk | 4.900 | 3.413 | 1.487 | knowledge_search, knowledge_search, knowledge_search | knowledge_search, knowledge_search, knowledge_search |
| C02 | complex | multi_doc_calc | 4.987 | 4.987 | 0.000 | knowledge_search, knowledge_search, calculator, calculator | knowledge_search, knowledge_search, calculator, calculator |
| C03 | complex | three_doc_synthesis | 4.900 | 4.900 | 0.000 | knowledge_search, knowledge_search, knowledge_search | knowledge_search, knowledge_search, knowledge_search |
| C04 | complex | multi_image_recall | 5.250 | 4.856 | 0.394 | knowledge_search | knowledge_search, knowledge_search, knowledge_search |
| C05 | complex | reverse_contradiction | 5.250 | 5.250 | 0.000 | knowledge_search | knowledge_search |
| C06 | complex | multi_doc_percent_calc | 2.888 | 2.888 | 0.000 | calculator | calculator |
| C07 | complex | knowledge_plus_text_stats | 5.250 | 4.550 | 0.700 | knowledge_search, tool_search, text_stats | knowledge_search, claude_skill_data_analysis |
| C08 | complex | negative_reverse | 4.200 | 3.587 | 0.613 | knowledge_search, knowledge_search | knowledge_search, knowledge_search |

## 关键题目观察

### C01：差值 1.487

- agent_loop：4.900/5.25，组件 {'route': 1.0, 'retrieval': 1.0, 'citation': 0.6667, 'answer': 1.0, 'safety_finish': 1.0}。
- plan_execute：3.413/5.25，组件 {'route': 1.0, 'retrieval': 1.0, 'citation': 0.4167, 'answer': 0.3333, 'safety_finish': 1.0}。
- 答案缺失：agent_loop=无；plan_execute=[{'label': '制造风险', 'any_of': ['工程变更单', '替代料']}, {'label': '低碳风险', 'any_of': ['消防', '实验室关键负荷']}]。
- 工具路线：agent_loop=['knowledge_search', 'knowledge_search', 'knowledge_search']；plan_execute=['knowledge_search', 'knowledge_search', 'knowledge_search']。

### C07：差值 0.700

- agent_loop：5.250/5.25，组件 {'route': 1.0, 'retrieval': 1.0, 'citation': 1.0, 'answer': 1.0, 'safety_finish': 1.0}。
- plan_execute：4.550/5.25，组件 {'route': 0.3333, 'retrieval': 1.0, 'citation': 1.0, 'answer': 1.0, 'safety_finish': 1.0}。
- 工具路线：agent_loop=['knowledge_search', 'tool_search', 'text_stats']；plan_execute=['knowledge_search', 'claude_skill_data_analysis']。

### C08：差值 0.613

- agent_loop：4.200/5.25，组件 {'route': 1.0, 'retrieval': 1.0, 'citation': 0.5833, 'answer': 0.6667, 'safety_finish': 1.0}。
- plan_execute：3.587/5.25，组件 {'route': 1.0, 'retrieval': 1.0, 'citation': 0.0, 'answer': 0.6667, 'safety_finish': 1.0}。
- 答案缺失：agent_loop=[{'label': '未提供合同', 'any_of': ['没有合同金额', '未提供合同金额', '未提到合同金额', '未找到合同金额']}]；plan_execute=[{'label': '未提供合同', 'any_of': ['没有合同金额', '未提供合同金额', '未提到合同金额', '未找到合同金额']}]。
- 工具路线：agent_loop=['knowledge_search', 'knowledge_search']；plan_execute=['knowledge_search', 'knowledge_search']。

## 建议

1. 若目标是评测“推理编排”而不是“工具暴露能力”，下一版应增加 shared-tool 子卷，或给 `plan_execute` 补齐 `agent_loop` 的 `tool_search/text_stats/update_task_plan/researcher` 等工具面。
2. C06 是两个引擎共同问题：对带数字的知识题，应在计算前强制检索证据，避免模型把用户输入数字当作充分上下文。
3. 复杂题主要优化 citation 覆盖：当问题要求跨文档对比时，可以要求最终答案至少引用所有命中的 gold doc，或者在工具层返回更结构化的 doc coverage 提示。
4. `plan_execute` 在 C01/C08 的答案覆盖偏弱，建议检查 execution planner 在多步计划中是否把前序检索证据完整传递到最终回答阶段。
