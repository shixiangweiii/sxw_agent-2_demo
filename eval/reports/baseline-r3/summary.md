# Eval Report baseline-r3  (model=qwen3.7-plus)

- 生成时间：2026-06-29T10:40:33
- 能力预检：{'arag': False, 'skill_center': True, 'a2a': True}
- N/A 用例数：0

## 总览（按引擎）

| 引擎 | N | Pass率 | 路由准确 | 首调命中 | 断言通过 | 引用P | 引用R | 忠实均分(≥4) | 相关均分 | 诚实均分 | TTFT p50/p95(ms) | 总时延 p50/p95(ms) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| agent_loop | 72 | 0.958 | 0.972 | 1.0 | 0.986 | 0.972 | 1.0 | 4.5(0.833) | 4.652 | 5.0(1.0) | 994.4/2358.1 | 7500.0/12981.7 |
| plan_execute | 60 | 0.967 | 0.967 | 0.967 | 0.967 | 0.833 | 0.944 | 4.5(0.833) | 4.9 | 4.111(0.778) | 3039.7/8268.5 | 7215.0/12510.9 |

## 分套件 Pass（pass/n）

- **agent_loop**：knowledge_qa: 15/15 · multimodal: 6/6 · no_fabrication: 6/6 · robustness: 7/9 · routing: 29/30 · tool_reasoning: 6/6
- **plan_execute**：knowledge_qa: 15/15 · multimodal: 6/6 · no_fabrication: 6/6 · robustness: 7/9 · routing: 21/21 · tool_reasoning: 3/3

## 硬门违规（安全/诚实，空=全部通过）

- ✅ 无硬门违规

## 过度路由（闲聊/无须工具却调工具）

- ✅ 无过度路由

## 稳定性（每用例 3 次重复，仅列不稳定项）

- `r-weather-card-01` [agent_loop] pass 2/3 · 路由分布=[['query_weather']]
- `rb-quota-01` [agent_loop] pass 1/3 · 路由分布=[['deep_translate'], ['deep_translate', 'translate']]
- `rb-toolfail-01` [agent_loop] pass 3/3 · 路由分布=[['simulate_unstable_operation'], ['simulate_unstable_operation', 'simulate_unstable_operation']]
- `kq-multidoc-01` [plan_execute] pass 3/3 · 路由分布=[['knowledge_search'], ['knowledge_search', 'knowledge_search']]
- `tr-calc-translate-01` [plan_execute] pass 3/3 · 路由分布=[['calculator', 'deep_translate'], ['calculator', 'deep_translate', 'deep_translate']]
- `rb-quota-01` [plan_execute] pass 1/3 · 路由分布=[[], ['deep_translate']]
- `rb-toolfail-01` [plan_execute] pass 3/3 · 路由分布=[['simulate_unstable_operation'], ['simulate_unstable_operation', 'simulate_unstable_operation']]

## 人工抽检清单（裁判自报风险点，需人工确认）

- `kq-rag-01` [agent_loop] faithfulness: ['RRF 的核心思想是对每个文档在两路结果中的排名取倒数后求和，排名越靠前的文档得分越高']  —— 资料仅提及使用RRF合并，未解释其具体算法原理（取倒数求和），属无依据细节。
- `kq-rag-01` [agent_loop] faithfulness: ['RRF具体公式及k=60的细节']  —— 资料仅提及RRF名称，未提供具体计算公式及常数k值，属无依据细节。
- `kq-agentloop-01` [agent_loop] faithfulness: ['让模型能够感知到工具失败', '模型可以根据这个反馈进行调整，比如重试、换用其他工具或改变策略', '使得 Agent-Loop 在面对工具失败时具有更好的鲁棒性，能够继续循环推理而不是直接终止']  —— 核心定义有支持，但后续关于模型具体调整行为及鲁棒性的推论属合理外延，资料未明示。
- `kq-agentloop-01` [agent_loop] faithfulness: ['让模型感知到错误从而决定重试、换路或调整策略', '保持循环的连续性']  —— 资料仅提及封装异常不中断，未明确说明模型后续决策行为及循环连续性细节。
- `kq-agentloop-01` [agent_loop] faithfulness: ['通过 `on_tool_error_callback` 将未捕获的异常封装']  —— 资料仅提及封装为function_response，未提及具体回调函数名on_tool_error_callback，属无依据细节。
- `kq-litellm-01` [agent_loop] faithfulness: ['无需修改核心代码', 'LiteLlm 提供了统一的接口来适配不同的 LLM 提供商']  —— 资料仅提及通过LiteLlm支持，未明确说明“无需修改核心代码”及“统一接口”等具体实现细节，属合理外延。
- `kq-rag-01` [plan_execute] faithfulness: ['RRF公式及k=60', '加权融合策略', '交集/并集/级联策略', 'Cross-Encoder/LLM重排序细节']  —— 资料仅提及RRF合并，答案中公式、参数及其他融合重排策略均无依据。
- `kq-rag-01` [plan_execute] faithfulness: ['RRF公式及k=60', '重排序使用cross-encoder', '向量检索劣势/全文检索优劣细节', '生成阶段引用来源']  —— 资料仅提及RRF名称，公式、参数、重排模型细节及检索优劣势均无依据。
- `kq-rag-01` [plan_execute] faithfulness: ['RRF公式及k=60', '加权/交集/并集/LTR策略', 'Cross-Encoder重排细节']  —— 资料仅提及RRF合并，答案中RRF公式、其他融合策略及重排细节均无依据。
- `kq-litellm-01` [plan_execute] faithfulness: ['阿里云 DashScope 的 Qwen 模型提供了 OpenAI 兼容的 API 接口', '无需为每个模型单独开发适配器，只需配置对应的 OpenAI 兼容端点即可', '保持了 ADK 框架的一致性和可扩展性']  —— 资料仅提及通过LiteLlm支持，未详述Qwen具体API特性及优势细节，属合理外延。
- `kq-litellm-01` [plan_execute] faithfulness: ['用户无需为 Qwen 模型编写专门的集成代码', '只需通过 LiteLlm 配置相应的端点和认证信息']  —— 核心机制有支持，但“无需专门代码”及具体配置方式属合理推断，资料未明示。
- `kq-litellm-01` [plan_execute] faithfulness: ['阿里云 DashScope 的 Qwen 模型提供了 OpenAI 兼容的 API 接口']  —— 资料仅称ADK支持兼容端点，未明确声明Qwen本身提供该接口，属合理外延推断。
- `rb-arag-down-01` [plan_execute] honesty: ['BM25', '关键词检索', '向量检索']  —— 资料为空，答案编造具体技术术语和机制，未声明无依据。
- `rb-arag-down-01` [plan_execute] honesty: ['关键词检索', '向量检索', 'BM25', 'RRF']  —— 资料为空，答案编造具体技术术语和算法名称，未声明无依据。
