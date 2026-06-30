# 中文公开评测集调研

生成时间：2026-06-30

本调研围绕当前 `sxw_optimization_demo` 的真实评测需求展开：浏览器/agent 入口触发 `knowledge_search`，arag 做 hybrid retrieval，最终答案需要 citation 事件，并且新语料还要覆盖 Markdown 图片召回。

## 结论

公开中文数据集中，最接近 RAG 评测的是 CRUD-RAG；CMRC2018 与 DuReader 更适合作为阅读理解或检索能力参考；SuperCLUE-RAG 的任务设计很贴近 RAG，但公开仓库主要提供说明与申请方式，不适合作为本地可复现数据源直接接入。

因此本目录采用“双轨”：

- 下载公开数据样例，保存在 `public_datasets/`，用于后续对齐外部基准。
- 自生成 10 篇长文档和 36 条 QA case，作为当前 agent 基础问答质量的主评测集。

## 已下载文件

| 数据集 | 本地文件 | 用途 | 当前判断 |
|---|---|---|---|
| CMRC2018 trial | `public_datasets/cmrc2018/cmrc2018_trial.json` | SQuAD-style 中文抽取式 QA 样例 | 可做格式参考；不含图片、citation、跨文档 RAG 场景 |
| CRUD-RAG split | `public_datasets/crud_rag/split_merged.json` | 中文 RAG 任务数据，含新闻文本和多类 QA/摘要/幻觉任务 | 可作为后续外部 RAG benchmark；需要转换为 arag index payload 和 eval case |
| 数据集 README | `public_datasets/readmes/*.md` | 保留来源说明、任务介绍与许可信息 | 作为本调研依据 |

## 数据集评估

### SuperCLUE-RAG

- 来源：https://github.com/CLUEbenchmark/SuperCLUE-RAG
- 任务特点：中文原生 RAG 测评，覆盖无文档问答、单文档问答、多文档问答，并强调对比式评估。
- 优点：任务设计和本项目的 RAG 问答质量评估高度相关。
- 限制：仓库说明显示需要申请评测，未提供可直接下载并本地复现的完整题集。
- 本项目适配建议：借鉴其单文档/多文档/无文档三类设计，不直接作为主数据源。

### CRUD-RAG

- 来源：https://github.com/IAAR-Shanghai/CRUD_RAG
- 本地下载：`public_datasets/crud_rag/split_merged.json`
- 任务特点：面向 RAG 系统的中文 benchmark，下载文件包含 `event_summary`、`continuing_writing`、`hallu_modified`、`questanswer_1doc`、`questanswer_2docs`、`questanswer_3docs` 等字段。
- 优点：有真实新闻文本，包含单文档和多文档 QA，适合后续做外部 RAG 回归。
- 限制：语料偏新闻域；不含图片；schema 需要转换；数据量较大，第一次入库会产生较多 embedding 成本。
- 本项目适配建议：后续可抽样 30-100 条，转换成 arag `/v1/index` 文档和 `eval/harness` case，用于外部真实性回归。

### CMRC2018

- 来源：https://github.com/ymcui/cmrc2018
- 本地下载：`public_datasets/cmrc2018/cmrc2018_trial.json`
- 任务特点：中文机器阅读理解，SQuAD-style context/question/answers。
- 优点：体量小，格式清晰，适合做抽取式 QA 基线。
- 限制：单段上下文为主；没有检索库构建、citation、图片、跨文档整合等本项目重点能力。
- 本项目适配建议：可把 context 转成 arag 文档，把 question/answers 转成 eval case，但只能作为阅读理解补充，不宜作为主评测。

### DuReader / DuReader Retrieval / DuReader-vis

- 来源：https://github.com/baidu/DuReader
- 任务特点：DuReader 系列覆盖阅读理解、passage retrieval 和中文开放域 DocVQA。
- 优点：DuReader Retrieval 和 DuReader-vis 分别贴近检索与文档视觉问答，方向上有价值。
- 限制：下载路径分散，规模较大；与当前 agent/citation harness 仍需较多转换；DocVQA 图片链路和本项目 Markdown 图片 caption 机制不完全一致。
- 本项目适配建议：作为第二阶段外部 benchmark 候选，优先级低于 CRUD-RAG 抽样。

## 自生成语料为何更适合当前阶段

当前目标是“基础问答质量”的第一轮可控评测，而不是和公开榜单直接比较。自生成语料有几个优势：

- 可以保证每篇文档至少 5 个章节和 1500 字符以上，覆盖 chunk 切分。
- 每篇都有明确 `doc_id`、事实锚点、数字阈值和风险点，便于规则断言。
- 每篇都有 Markdown HTTP 图片链接和图片说明，符合 arag 当前 `extract_image_urls()` 的实现。
- 可以设计单文档、同主题多文档、跨主题综合、图片召回、无答案诚实性等 case。
- 生成的 `index_payload.json` 可直接 POST 到 arag `/v1/index`，无需额外转换。

## 后续建议

1. 先用 `generated_eval/cases_generated_rag.jsonl` 跑一次本地基础评测，定位 agent 是否稳定调用 `knowledge_search`、是否输出 citation、是否跨文档混淆。
2. 再从 CRUD-RAG 抽样构造外部真实新闻域评测，避免只在自生成语料上过拟合。
3. 如果要测图片召回质量，应记录图片 caption 是否成功进入 chunk；当前图片使用 `placehold.co` 的稳定 HTTP 图片 URL，适合低成本验证链路。
