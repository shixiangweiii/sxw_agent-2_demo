# RAG 模块对 lippi-arag 的参考覆盖与生产级能力引入评估

> 评估日期：2026-08-06
> 当前项目：`sxw_agent-2_demo@bcd91cb`
> 参考项目：`/Users/shixiangweii/PycharmProjects/arag_learn_proj/lippi-arag`
> 文档性质：只读代码审计与演进建议，不代表下述能力已经实现
> 结论摘要：当前项目已复刻约 75%～85% 的 RAG 架构形状，但综合生产能力深度约为 30%～40%。有必要继续吸收 `lippi-arag` 的生产经验，重点应放在检索正确性、知识库隔离、文档生命周期、分支级降级、可观察性和专项评测；不建议整体搬运尚未接线的 `sxw_agentic_arag`、GraphRAG 或强企业基础设施耦合代码。

## 1. 背景与评估目标

当前项目的 `arag/` 是从生产项目主链路中抽取、简化而来的本地 RAG 样板，已经具备独立服务、混合召回、查询改写、RRF 融合、多模态 caption、本地持久化、Agent 工具接入、引用生成和降级等能力。

随着参考项目 `lippi-arag` 持续演进，需要重新回答以下问题：

1. 当前项目具体参考了 `lippi-arag` 的哪些架构和功能？
2. “参考了多少”应如何按架构形状、核心功能和生产深度分别衡量？
3. `lippi-arag` 当前工作树与最新生产 release 中，哪些能力已经成熟并值得吸收？
4. 哪些代码属于实验、半成品、内部基础设施或历史治理，不适合搬运？
5. 下一阶段继续建设 RAG 时，应该优先补正确性和可靠性，还是增加 Agentic、Graph 等能力？

本次只进行代码、文档、Git 历史和现有评测产物的只读审计，不进行任何代码修改。

## 2. 评估范围与快照口径

### 2.1 当前项目快照

```text
仓库：sxw_agent-2_demo
分支：main
HEAD：bcd91cb
时间：2026-08-06 16:04:13 +0800
```

重点检查范围：

- `arag/api/`：索引、检索和独立 RAG API；
- `arag/components/`：rewrite、embedding、retriever、RRF、filter、generator、chunker；
- `arag/processor/`：文档和图片处理；
- `arag/store/`：VectorStore、FullTextIndex、GraphStore 端口及本地实现；
- `agent/tools/knowledge_search.py`：Agent 到 ARAG 的调用和降级；
- `agent/citation/`：引用编号、注册和 SSE 输出；
- `web/app.js`：文件解析与上传；
- `eval/`：RAG、引用和两代引擎评测现状。

### 2.2 参考项目当前工作树

用户给出的参考目录当前检出：

```text
仓库：lippi-arag
分支：sxw_learn
HEAD：ae7a3b68
时间：2026-06-22 14:57:36 +0800
提交：agentic arag 改造 to #000000
```

工作区存在 `AGENTS.md` 修改和未跟踪的说明文档，但 ARAG 运行时代码没有未提交修改。

该工作树包含一个新建的 `sxw_agentic_arag/` 目录。审计结果表明，它属于未接入生产主链路的 greenfield 实验实现，不能因为目录名称较新就直接视为“最新生产 ARAG”。

### 2.3 本地最新生产 release 引用

为避免只按当前 checkout 判断“最新”，同时检查了本地已有、按提交日期最新的生产 release 引用：

```text
ref：origin/releases/20260710142711604_r_release_283624_lippi-arag-code
commit：aaa197e6
时间：2026-07-28 19:37:51 +0800
相对 origin/main：24 commits ahead
```

该 release 相对 `origin/main` 主要变更为：

- rewrite 的 lexeme 索引命中检查从逐实例改为按物理表批量执行；
- 多年份问题强制按年份拆分为独立 query；
- generator、persona 和 thinking 参数增强；
- 补充相关单元测试。

它没有引入新的 RAG 主架构，也没有合入当前 `sxw_learn` 工作树里的 `sxw_agentic_arag/`。

因此本文采用以下口径：

1. 以 `lippi-arag` 默认 `AlbertChatPipeline` 作为成熟生产主链路；
2. 以最新 release 的 rewrite 和测试增强作为后续补充；
3. 将 `sxw_agentic_arag`、Graph、ES 等按实际接线、测试和完整度单独评级，不把它们自动算作生产能力。

## 3. 核心结论

### 3.1 一句话定位

当前 RAG 不是单文件演示，而是一个：

> 生产架构形状较完整、核心 happy path 可运行、适合学习和面试讲解的本地样板；但尚不具备真实生产所需的数据隔离、索引生命周期、并发安全、可靠拒答、分支容错和专项质量门禁。

### 3.2 覆盖率结论

“参考了多少”不能只用单一百分比回答。按不同维度评估如下：

| 维度 | 参考覆盖估计 | 结论 |
|---|---:|---|
| 服务边界、组件分层、Store 抽象 | 75%～85% | 主要架构盒子和调用方向已保留 |
| RAG happy path | 45%～55% | 改写、双路召回、融合、过滤、返回均可运行 |
| 生产检索质量策略 | 30%～40% | 缺真实语义 reranker、可靠 miss gate 和复杂 rewrite |
| 入库、切分和多模态 | 25%～35% | 有基本链路，但版面、表格、页码和图片锚点损失明显 |
| 存储和文档生命周期 | 20%～30% | 有本地持久化，无完整 replace/delete/version/事务语义 |
| 可靠性、可观察性和测试 | 20%～30% | 有 trace 和服务边界降级，缺分支级降级与检索专项测试 |
| 多知识库隔离和安全治理 | 0%～10% | 当前基本是全局索引 |
| 综合生产能力深度 | **30%～40%** | 完整样板，不是生产等价实现 |

如果只按代码量衡量，当前 `arag/` 约 1,176 行 Python，而参考仓库相关 components、services、processors、middleware 和 tests 约数万行，比例大约只有 2%～3%。但该数字受到企业治理、基础设施适配和测试代码影响，不能用于判断核心设计是否已经复刻。

### 3.3 是否有必要继续搬运

结论是：**有必要，但应选择性吸收设计，不能继续按目录或文件整块复制。**

最值得吸收的不是更多“功能名词”，而是：

- 检索结果的可靠性和零结果判定；
- 真实语义 rerank 及其降级；
- 多知识库和 metadata scope；
- 文档 replace/delete/version 和增量 embedding；
- 每个召回分支的 timeout、隔离和 partial degradation；
- 分阶段指标、召回质量评测和回归门禁；
- 可稳定跨多次检索的 citation identity 和 provenance。

## 4. 当前项目 RAG 基线

### 4.1 当前服务与组件结构

当前 `arag/` 约 1,176 行 Python，分层如下：

```text
arag/
├── api/
│   ├── index.py
│   └── retrieve.py
├── components/
│   ├── chunker.py
│   ├── embedding.py
│   ├── filter.py
│   ├── generator.py
│   ├── reranker.py
│   ├── retriever.py
│   └── rewrite.py
├── processor/
│   ├── document.py
│   └── image.py
├── store/
│   ├── base.py
│   ├── factory.py
│   ├── fulltext_index.py
│   ├── graph_store.py
│   └── vector_store.py
├── context.py
├── config.py
├── schemas.py
└── main.py
```

仓库自身明确留下了若干来源说明：

- `arag/__init__.py`：`lippi-arag 精简复刻`；
- `arag/store/factory.py`：mirror `lippi-arag StoreFactory`；
- `arag/processor/image.py`：对应参考项目的 OCR / LLM caption 处理；
- `agent/tools/knowledge_search.py`：对应生产 SmartSearchTool 到 ARAG 的调用；
- `agent/citation/citation_injector.py`：生产 ID 引用协议的精简版。

这说明当前实现不是偶然使用了相似技术，而是有意识保留了参考项目的组件边界、服务边界和主链路形状。

### 4.2 当前入库链路

```text
Web 上传 txt/md/pdf/docx
  -> 浏览器端 PDF.js / Mammoth 提取文本
  -> agent POST /api/v1/documents/index
  -> arag POST /v1/index
  -> to_document
  -> Markdown HTTP(S) 图片 URL caption
  -> 字符预算 chunk
  -> embedding
  -> LocalVectorStore 持久化
  -> LocalBM25Index 重建
```

已具备：

- 文档 DTO 和批量索引 API；
- 段落感知、句子回退和 overlap 的字符切分；
- Markdown 图片 URL 的视觉 caption；
- embedding 批处理；
- chunk id upsert；
- chunks JSON、vectors NPY 和 manifest 持久化；
- ARAG 重启后加载向量并重建 BM25。

主要简化：

- PDF、DOCX 解析发生在浏览器，不在 ARAG 服务端；
- PDF 仅拼接 text item，DOCX 仅提取 raw text；
- 表格、版面、页码、段落位置和扫描件 OCR 信息丢失；
- 只识别 Markdown 中的 HTTP(S) 图片 URL；
- 图片 caption 统一追加到文档末尾，图片与原段落的对应关系丢失；
- 没有异步任务状态、进度、失败重试和文档级索引生命周期。

### 4.3 当前检索链路

```text
用户问题
  -> Agent 决定调用 knowledge_search
  -> POST arag /v1/retrieve
  -> LLM query rewrite
  -> 原问题 + 改写问题 embedding
  -> 每个 query 执行 numpy cosine 向量召回
  -> 每个 query 执行 BM25 召回
  -> 两路分别去重
  -> 等权 RRF 融合
  -> 轻量过滤
  -> top-k chunks
  -> Agent 生成答案
  -> CitationInjector 将正文 [n] 转为 citation SSE
```

当前主链路实现位置：

- `arag/components/retriever.py:42`：完整召回编排；
- `arag/components/rewrite.py:17`：query rewrite 和失败回退；
- `arag/store/vector_store.py:189`：numpy cosine 搜索；
- `arag/store/fulltext_index.py:34`：BM25 搜索；
- `arag/components/reranker.py:13`：RRF；
- `arag/components/filter.py:9`：低价值过滤；
- `agent/tools/knowledge_search.py:18`：Agent 到 ARAG 的 HTTP 调用；
- `agent/citation/citation_injector.py:19`：引用注入。

### 4.4 当前降级与可观察性

已有能力：

- Agent 到 ARAG 透传 `x-trace-id`；
- ARAG 不可用或超时时，`knowledge_search` 返回 degraded 信息而不中断 Agent turn；
- 有 `[QaRetrieve]`、`[Index]`、`[Access]` 等结构化日志；
- API 层记录整体索引和检索耗时；
- `/healthz` 可返回当前配置的 backend。

缺少能力：

- rewrite、embedding、vector、BM25、RRF、filter 分阶段耗时；
- 各分支候选数、命中率、分数分布和 fallback 指标；
- token、模型调用成本和索引规模指标；
- embedding、vector 和 BM25 之间的局部故障隔离；
- 模型和依赖真实可用性的 readiness 检查；
- query 和文档日志脱敏策略。

## 5. lippi-arag 成熟生产主链路

### 5.1 默认 AlbertChatPipeline

参考项目真正成熟且已接入默认问答 API 的主链路是 `AlbertChatPipeline`，核心流程位于：

```text
app/services/albert_pipeline_chat_runner.py
```

其主要流程为：

```text
术语处理
  -> query rewrite
  -> FAQ 精确命中
  -> vector + PostgreSQL full-text 候选召回
  -> 召回统计
  -> 去重
  -> 低价值过滤
  -> 外部语义 rerank
  -> 阈值/保底/source-aware 过滤
  -> token budget
  -> 答案生成与引用占位处理
```

与当前项目相比，生产主链路的主要优势不在于多了一个流程图节点，而在于每个节点内部有更细的策略、数据范围和降级语义。

### 5.2 生产主问答并不使用 RRF 作为最终 reranker

这是本次对照中需要特别澄清的一点。

当前项目的 `arag/components/reranker.py` 实际是 RRF 融合。参考项目也有 RRF，但它主要出现在独立的：

```text
POST /v1/search/chunk
app/api/search.py
```

默认 `AlbertChatPipeline` 使用的是：

- vector + PostgreSQL FTS 构造候选池；
- OpenAI-compatible `/rerank` 语义重排；
- rerank 阈值、topN、保底文档、全文召回保护；
- source-aware 合并和 token budget。

因此更准确的判断是：

> 当前项目已经复刻了“多路召回后融合”的阶段，但没有复刻默认生产问答链路的真实语义 reranker 和结果治理。

参考位置：

- `app/components/retriever/albert_pgvector_retriever.py:62-211`；
- `app/components/reranker/arag_reranker.py:96-382`；
- `app/api/search.py:192-219`。

### 5.3 生产 query rewrite

生产 rewrite 不只是让 LLM 返回几个改写问题，还包括：

- 结合历史进行指代消解和追问补全；
- 返回结构化 `queries + keywords`；
- 最多生成约 5 个 query；
- 单独生成全文检索 tsquery；
- JSON 失败时回退原 query；
- tsquery AST 解析和规范化；
- jieba 重分词；
- AND 数量预算；
- 数据库 tokenizer 归一；
- 根据实际知识库索引命中情况过滤 lexeme；
- 依赖失败时 fail-open；
- 最新 release 中按物理表批量执行 lexeme 命中检查；
- 最新 release 中强制将多年份问题逐年拆分。

主要实现位于：

```text
app/components/rewrite/common_rewriter.py
app/components/prompt/rewriter_prompt.py
```

其中 AST、数据库 lexeme 命中和物理表批量查询与 PostgreSQL 和实例分表强耦合，不适合直接照搬；结构化输出、规则 fallback、多年份拆分和 fail-open 则具有通用价值。

### 5.4 生产入库链路

`lippi-arag` 的主入库链路包含：

- URL 下载或纯文本输入；
- 同步处理或 MQ 异步处理；
- 文档状态、进度和错误记录；
- Markdown 图片和链接占位抽取；
- Markdown heading-aware 切分；
- HTML/table-aware 切分；
- token chunker；
- 目录标签和关键词提取；
- 图片 AI 分析和 OCR；
- 图片 chunk；
- chunk_count 和可选文档摘要；
- 相同 chunk 文本的增量 embedding 复用；
- 文档原文、摘要、chunk 和状态分别持久化。

主要实现位于：

```text
app/processor/document/doc_processor.py
app/processor/document/async_document_processor.py
app/components/chunker/markdown_to_entries.py
app/components/chunker/table_chunker.py
```

需要保持边界诚实：参考项目主链路也没有完整的 PDF/Word 二进制版面解析；URL 下载主要按文本读取。不能把其文档处理描述成完整的 MinerU 或 Unstructured 文档理解平台。

### 5.5 生产存储与数据范围

参考项目的主存储基于 PG/ADB，具备：

- vector、全文检索字段和 ANN/HNSW 索引；
- chunk upsert；
- instance、org、biz、doc 等 scope；
- auth metadata；
- document 原文、摘要和 chunk_count 独立存储；
- 实例到专属/共享物理表的映射；
- enable、disable、delete 和 search-by-doc 等生命周期接口。

主要实现位于：

```text
app/components/store/albert_pgvector_store.py
app/components/store/document_store.py
app/components/store/base.py
app/store/instance_meta.py
app/models/request_context.py
```

当前项目已经复刻 Store 端口和 Factory 的形状，但没有复刻上述数据范围、生命周期和事务语义。

### 5.6 可选但已经真实接线的能力

#### 5.6.1 Index-first

Index-first 按灰度开关和文档数阈值启用：

1. 先用文档标题、摘要和 chunk_count 构建目录；
2. 让 LLM 选择候选文档；
3. 校验模型返回的文档 ID，避免幻觉 ID；
4. 小文档整篇返回；
5. 大文档在文档范围内做向量检索；
6. 目录构建或 embedding 失败时回退默认 retriever。

实现位于：

```text
app/components/retriever/index_first_retriever.py
app/components/retriever/index_md_builder.py
```

这是后续很值得借鉴的一项，但它需要文档摘要、chunk_count、scope filter 和检索评测作为前置条件。

#### 5.6.2 FAQ exact hit

FAQ 使用 query embedding 和较高相似度阈值进行精确短路，适合高频标准问答。它有真实接线，但是否适合当前项目取决于是否要演示 FAQ 产品形态。

#### 5.6.3 Batch retrieve

参考项目的 batch retrieve 支持：

- multi-query；
- multi-KB；
- 独立 keyword queries；
- canonical rerank query；
- 每个检索分支的 timeout；
- 部分分支失败时继续返回；
- FAQ 配额、去重和 rerank。

它的分支隔离和 partial degradation 设计比具体的 multi-KB 业务参数更值得迁移。

### 5.7 参考项目也不是所有代码都可直接复制

生产仓库代表真实工程经验，但并不意味着每个实现都应原样搬入：

- 默认主 pipeline 的 external rerank 异常缺少完整 fallback；
- 多 rewrite query 的 embedding `asyncio.gather` 中，单支失败仍可能影响整次检索；
- MQ consumer 捕获异常后仍可能确认消费成功，broker 不一定重试；
- 部分接口鉴权边界不统一；
- 可见硬编码密钥、内网 endpoint、宽松 CORS 和完整 prompt 日志等安全反例；
- ES backend 的 delete、enable、embedding reuse 等能力不完整；
- legacy/test indexing API 不适合作为新项目基础。

因此后续原则应是：

> 搬设计意图、失败语义和评测方法，不复制内部依赖、密钥、历史兼容和已知缺陷。

## 6. 当前能力映射

| 能力 | 当前项目 | lippi-arag 默认生产主链路 | 覆盖判断 |
|---|---|---|---|
| 独立 RAG 服务 | FastAPI，index/retrieve/rag | 独立服务，多组 API | 已覆盖核心形状 |
| Agent 到 RAG 边界 | HTTP 工具调用、trace、8 秒超时、失败降级 | SmartSearch/服务编排、完整 context | 已覆盖核心形状 |
| Query rewrite | 原 query + 最多 2 条改写，失败回退 | queries + keywords + tsquery + AST/lexeme 规则 | 轻量覆盖 |
| 向量召回 | numpy cosine，本地持久化 | PG/ADB vector、scope filter、ANN | 算法形状覆盖，工程能力差距大 |
| 稀疏召回 | jieba + BM25 | PostgreSQL FTS、tsquery、放宽回退 | 目标一致，实现不同 |
| 混合融合 | 等权 RRF | 默认主链路为候选池 + 语义 rerank；独立 search 有 RRF | 部分覆盖 |
| 真实 reranker | 无 | 外部语义 rerank、阈值、保底、source-aware | 未覆盖 |
| 低价值过滤 | `min_score=0` 的轻量过滤 | 多阶段阈值、来源保护、token budget | 近似占位 |
| FAQ exact hit | 无 | 已接线 | 未覆盖，可选 |
| Index-first | 无 | 已接线、可回退 | 未覆盖，条件性高价值 |
| 文本 chunk | 字符和段落感知 | Markdown/table/token-aware | 部分覆盖 |
| 图片处理 | URL 图片 caption，追加文末 | 图片占位、OCR/VL、图片 chunk | 轻量覆盖 |
| 增量 embedding | chunk id upsert，但不按文本复用 | 相同文本向量复用 | 未等价覆盖 |
| 文档生命周期 | 无完整 delete/replace/version | document/chunk/status 生命周期 | 主要未覆盖 |
| 持久化 | JSON + NPY + manifest | PG/ADB、连接池、事务、scope | 端口形状覆盖 |
| 多租户/多知识库 | 无有效 filter | instance/org/biz/doc scope | 未覆盖 |
| 分支级降级 | Agent 边界整体降级 | 部分 API 有 per-branch timeout 和 partial result | 主要未覆盖 |
| 可观察性 | trace 和整体耗时日志 | 阶段 trace、source/count/rate/token/fallback 指标 | 轻量覆盖 |
| Citation | `[n]` 到 citation SSE | 生产 ID 协议和流式占位处理 | 协议形状覆盖 |
| 检索专项测试 | 基本没有 | 较多组件和集成测试，但评测体系也不完美 | 主要未覆盖 |
| Agentic RAG | Agent Runtime 在 RAG 外部编排 | 新 greenfield 目录未接主链路 | 不应按生产能力对比 |
| GraphRAG | GraphStore 占位、不参与检索 | 无成熟 Graph 检索主链路 | 双方均未形成能力 |

## 7. 当前项目的关键生产缺口

### 7.1 没有可靠的“无相关结果”判定

当前向量索引只要非空就返回 top-k；RRF 分数始终为正；过滤默认 `min_score=0`。因此无关 query 仍可能得到若干文档。

现有评测代码甚至明确记录：当前样本库下，任意 query 都可能返回三篇样本文档。这意味着：

- “检索到了文档”不等于“检索到了相关文档”；
- Agent 可能基于低相关内容生成貌似有引用的答案；
- 当前缺少 negative query 和 reject/miss 指标；
- 在没有可靠 relevance gate 前，继续增加 rewrite 或召回分支可能扩大噪声。

这是当前检索质量上最高优先级的问题。

### 7.2 缺少真实语义 reranker

当前名为 `reranker.py` 的组件实际只做 RRF。RRF 能融合排名，但不能判断文本是否真正回答了问题，也不能直接校准不同 query 和不同召回通道的语义相关度。

生产 reranker 带来的主要价值包括：

- 重新判断候选 chunk 与原问题的语义相关度；
- 为 miss gate 提供更可解释的分数基础；
- 保护全文命中和特殊来源；
- 控制 topN 和 token budget；
- 在失败时降级回 RRF 或原始 cosine 排名。

迁移时应保留 provider adapter，不能把某个内部 rerank endpoint 写死到主流程。

### 7.3 当前是全局知识索引

Web 上传文档时虽然传入 `user_id/session_id` metadata，但检索请求只有：

```text
query
top_k
use_rewrite
```

Retriever 没有 metadata filter，因此不同用户或会话上传的内容会进入同一个可被全局检索的索引。

这不仅是功能缺失，也是实际数据隔离问题。即使本项目不接企业 EAM，也至少需要建立通用的：

```text
tenant_id / user_id
kb_id
doc_id
include/exclude metadata filters
```

在没有可信认证前，只能诚实描述为“逻辑 namespace”，不能描述为安全 ACL。

### 7.4 文档更新不是完整 replace

当前 chunk id 使用：

```text
doc_id#index
```

相同文档重新入库时会覆盖相同编号，但如果新版本 chunk 数量变少，旧版本尾部 chunk 不会自动删除。

潜在后果：

- 检索返回文档已删除的旧内容；
- 同一个 doc_id 对应多个版本的混合数据；
- 无法确认索引是否与原文一致；
- 重复入库只能算部分 upsert，不能算完整幂等 replace。

应补齐：

- delete-by-doc；
- replace transaction；
- content hash 和 document version；
- embedding model、dimension 和 schema version；
- stale chunk 清理；
- 索引状态和失败原因。

### 7.5 本地持久化缺少并发和事务语义

当前持久化依次写入多个临时文件并 replace：

```text
manifest.json
chunks.json
vectors.npy
```

优点是比直接覆盖单文件更安全，但仍存在：

- 没有进程内或跨进程写锁；
- 三个文件不是一个原子快照；
- 中途崩溃可能出现跨版本组合；
- async API 中执行同步 numpy、BM25 和文件 I/O；
- 换 embedding model 时 manifest 信息不足；
- 并发索引与检索的可见性没有明确定义。

当前 local backend 可以继续保留，但需要把它提升为“可靠的本地后端”，不必为了生产感直接切到 PostgreSQL。

### 7.6 ARAG 内部容错不足

当前已有：

- rewrite 失败回原 query；
- 单张图片 caption 失败后跳过；
- Agent 调 ARAG 失败后降级到普通对话。

但 ARAG 内部仍缺少：

- embedding 失败后继续 BM25；
- BM25 失败后继续 vector；
- reranker 失败后回退 RRF/cosine；
- 单条 rewrite query 失败不影响其他 query；
- 每个分支独立 timeout；
- request-wide budget；
- 并发和限流；
- 故障类型和 fallback reason 指标。

只在微服务边界做整体降级，会把“一个分支失败”扩大成“整个知识能力不可用”。

### 7.7 Citation identity 在多次检索时可能冲突

每次 `knowledge_search` 都从 `[1]` 开始给文档编号，CitationInjector 使用：

```text
dict[int, document]
```

注册引用。若一个 Agent turn 内调用多次知识检索，后一次的 `[1]` 可能覆盖前一次 `[1]` 的映射，最终正文中的引用会被解析到错误文档。

这是从当前控制流可以直接推导出的风险，尤其影响：

- 多跳问答；
- 分阶段研究；
- Agent 根据上一轮结果继续检索；
- 跨文档综合回答。

此外，Agent 层当前主动丢弃了部分 ARAG 字段，只保留 title、doc_id 和 content，没有完整保留：

- chunk_id；
- score；
- source/召回通道；
- page/section；
- URL；
- metadata；
- evidence span。

### 7.8 文档结构与多模态 provenance 不足

当前浏览器端解析使服务端接收到的是已扁平化文本：

- PDF 页码和版面丢失；
- DOCX 标题、列表、表格和图片关系丢失；
- 扫描 PDF 无 OCR；
- 图片 caption 与原段落解绑；
- citation 无法定位到页、章节或图片。

继续吸收生产能力时，应优先保留结构和 provenance，而不是只提高“支持的文件扩展名”数量。

### 7.9 API 治理和健康检查较弱

当前缺少：

- query 长度和 `top_k` 范围；
- 文档数量、体积和 doc_id 格式校验；
- 索引幂等键；
- 请求级限流和并发限制；
- readiness；
- 索引容量和 embedding 维度检查；
- 依赖模型实际可用性检查。

Web UI 的五文档、二十万字符限制可以绕过，不能视为服务端治理。

### 7.10 测试和评测还不足以支撑策略演进

当前仓库没有 RAG 组件级单元测试，主要依赖 `py_compile` 和真实 LLM 黑盒评测。

现有历史受控评测包含 10 篇文档、84 个 chunks、30 道题，报告约 95.15 分，但已经暴露：

- 跨文档题 citation coverage 不足；
- 个别知识问题没有调用 `knowledge_search`；
- 缺少独立 retriever 指标；
- 无关 query 仍可能返回样本文档。

尚未系统覆盖：

- Recall@K、HitRate、MRR、nDCG；
- zero-result precision 和 false positive rate；
- rewrite 前后增益；
- vector-only、BM25-only、RRF、reranker 的消融；
- 文档缩短后重入库；
- delete、restart、损坏快照和 embedding 模型变化；
- 并发索引与检索；
- 多次 knowledge_search 的 citation 冲突；
- 分支超时和故障注入；
- Markdown 表格、页码、图片锚点和扫描件。

在这些基线建立前，很难判断新增 Index-first、复杂 rewrite 或更多召回通道是否真的有收益。

## 8. 对 sxw_agentic_arag 与 Graph 的判断

### 8.1 sxw_agentic_arag 不是当前生产主线

参考工作树里的 `sxw_agentic_arag/` 具有 state、budget、planner、evidence 等 Agentic RAG 形状，但审计发现：

- 没有注册到 `main.py`；
- 没有接入默认 API 或 `QaSkillService`；
- runner 最终仍只调用一次现有 retrieval；
- planner 构造了 candidate strategies，但 runner 没有按 plan 执行多策略；
- budget 和 second-pass 等配置没有形成闭环；
- evidence 只要存在任意 chunks，就可能把所有 subquestions 标记为 covered；
- `WithinDocAdapter` 仍是 `NotImplemented`；
- 没有对应测试；
- 最新生产 release 没有包含该目录。

因此不能把它作为“生产 Agentic ARAG”直接搬入当前项目。

更重要的是，当前项目已经在 RAG 外部拥有两代引擎、工具循环、计划续推和子代理机制。再引入一个独立 RAG Agent Loop 会产生：

- 谁负责规划的职责重叠；
- 两层循环和两套 budget；
- 调试和 trace 边界复杂；
- 工具选择与检索策略重复；
- 评测归因困难。

当前更合理的方向是让 RAG 暴露可靠、可组合的检索能力，而不是在 RAG 服务内再复制一套通用 Agent Runtime。

### 8.2 GraphStore 不应成为下一阶段目标

当前项目的 GraphStore 只在 Context 中装配，不参与索引或检索。参考项目同样没有成熟接入的 Graph 检索主链路，文档中的 GraphRAG 更多是后续研究方向。

因此：

- GraphStore 不应计入已参考的生产能力；
- 不建议为了功能列表完整而先接 Neo4j/Nebula；
- 只有在明确存在实体关系、跨文档多跳问题，并有对应 benchmark 时，才应单独立项。

## 9. 建议继续吸收的能力

### 9.1 P0：先建立评测和正确性门禁

#### 9.1.1 独立 retrieval benchmark

建议先建立不经过 Agent 生成的检索评测集：

- query；
- relevant doc/chunk ids；
- hard negatives；
- should_retrieve / should_reject；
- single-doc / multi-doc / temporal / terminology / table / image 等标签。

核心指标：

- Recall@K；
- HitRate@K；
- MRR；
- nDCG；
- zero-result precision；
- false positive rate；
- P50/P95 延迟；
- 每阶段耗时和 fallback rate。

#### 9.1.2 组件和生命周期测试

优先覆盖：

- chunker 边界和 overlap；
- RRF、去重和 filter；
- 文档 replace/delete；
- 持久化 restart、损坏文件、dimension/model 变化；
- 多次知识检索 citation identity；
- vector/BM25/reranker 分支故障；
- 并发索引和检索。

这一阶段不追求新增功能，目标是让后续每项迁移都有可比较的 baseline。

### 9.2 P0：补可靠 relevance gate 和语义 reranker

建议吸收 `lippi-arag` 的设计意图：

- 召回候选与最终相关性判断分离；
- 使用可插拔的 reranker adapter；
- 基于评测校准阈值，不直接复制生产阈值；
- 支持 source reservation 和 token budget；
- reranker 不可用时回退 RRF/cosine；
- 返回零个结果是合法状态；
- 记录 raw score、rerank score、filter reason 和 fallback reason。

预期收益：直接降低无关文档被交给 Agent 的概率，是质量收益最高的一项。

### 9.3 P0：建立通用知识范围和 metadata filter

建议建立与内部 EAM 无关的最小数据模型：

```text
tenant_id 或 owner_id
kb_id
doc_id
chunk_id
metadata
```

索引、vector、BM25、delete、retrieve 和 citation 都必须使用同一个 scope。

验收重点：

- 不同 `kb_id` 不可串库；
- replace/delete 只影响目标知识库；
- vector 和 BM25 应用相同 filter；
- restart 后 scope 不丢失；
- 没有可信认证时文档明确标注为逻辑隔离。

### 9.4 P0：补齐文档生命周期和本地持久化正确性

建议优先实现：

- document manifest；
- content hash；
- document version；
- embedding provider/model/dimension；
- replace/delete-by-doc；
- stale chunk 清理；
- 相同文本 embedding 复用；
- 原子快照或明确的 commit marker；
- 写锁和读写可见性；
- 索引状态、进度和失败原因。

这部分不需要立即切换到 PG。可靠的 local backend 更符合当前项目定位，也更容易讲清端口和实现的边界。

### 9.5 P0：分支级 timeout、partial degradation 与指标

建议将检索拆成可观察的独立阶段：

```text
rewrite
embedding
vector recall
sparse recall
dedup/fusion
rerank
filter
context assembly
```

要求：

- 多 query 和多召回分支并发；
- 每个分支有独立 timeout；
- gather 使用明确的 partial failure 语义；
- vector 失败继续 sparse；
- sparse 失败继续 vector；
- rerank 失败回 RRF/cosine；
- request-wide budget 防止所有降级叠加后超时；
- 日志和指标记录候选数、耗时、fallback 和最终来源。

### 9.6 P0：稳定 citation identity 和 provenance

建议把引用 ID 从“每次工具调用内的 `[1]`”提升为 invocation 级稳定标识，至少保留：

```text
citation_id
retrieval_call_id
kb_id
doc_id
chunk_id
title
page/section
url
score
source
metadata
```

模型仍可输出简洁的 `[n]`，但 `[n]` 必须由一次 Agent invocation 中的统一 registry 分配，不能由每次工具调用从 1 重置。

## 10. 建议在 P0 后评估的能力

### 10.1 P1：结构化 rewrite 与全文查询 fallback

推荐吸收：

- `queries + keywords` 结构化输出；
- 历史对话只用于指代消解和追问补全；
- 相对日期和多年份展开；
- JSON/parser 失败回原 query；
- 全文查询过严时放宽 AND；
- 规则预算和 fail-open；
- rewrite 前后效果消融。

不建议直接搬：

- PostgreSQL 专用 AST 细节；
- instance 到物理表的批量 lexeme SQL；
- 内部分词器和表路由配置。

应根据 BM25 backend 设计等价、轻量的通用实现。

### 10.2 P1：Markdown、表格和 provenance-aware chunking

推荐顺序：

1. 保留 heading path；
2. 表格按表头和行切分；
3. chunk 保留 page/section/source span；
4. 图片 caption 放回原位置；
5. 再考虑 OCR 和更多文档格式。

重点不是文件格式数量，而是回答中的引用能否回到原文位置。

### 10.3 P1：Index-first

Index-first 对较大、结构化知识库有明显价值，也适合作为“检索前规划”的面试亮点。但前置条件包括：

- 稳定的 document store；
- title、summary、chunk_count；
- kb/doc scope；
- within-doc search；
- hallucinated doc id 校验；
- 默认 retriever fallback；
- 与全库检索的 A/B benchmark。

如果知识库仍只有少量样本文档，Index-first 的复杂度大于实际收益。

### 10.4 P1/P2：异步入库任务

当文档解析、OCR、摘要和 embedding 已经明显超过单次 HTTP 请求的可靠时间后，再引入：

- index task id；
- queued/running/completed/failed；
- progress；
- retry；
- cancel；
- dead-letter 或失败恢复。

不需要一开始就复制生产 MQ，可以先用进程内任务或本地持久化队列验证状态机。

### 10.5 按场景决定：FAQ exact hit

FAQ 对标准问答、高频短问题和客服语料有价值。如果项目没有明确 FAQ 数据模型和评测集，不应仅为了对齐生产功能而加入。

## 11. 不建议搬运的内容

### 11.1 当前不建议搬运

- 整个 `sxw_agentic_arag/` greenfield runtime；
- 未接线的 Graph producer、Neo4j/Nebula 或 GraphRAG；
- 未完整实现的 ES backend；
- legacy/test indexing API；
- Experience retrieval 和强业务化 doc-route；
- EAM ACL、Nacos/Diamond、内部灰度、信用体系等企业治理实现；
- 内部 MQ、SLS、Langfuse endpoint 的具体接入代码；
- 生产仓库中的硬编码 key、内网 URL、宽松 CORS 和完整 prompt 日志；
- 未经本项目 benchmark 验证的 prompt、阈值、topN 和权重。

### 11.2 暂不需要为了“生产感”切换存储后端

PostgreSQL/pgvector 确实能提供事务、并发、filter 和索引能力，但当前项目的核心目标不是展示数据库部署。

建议先让 Store 端口支持完整语义，并让 local backend 达到：

- scope filter；
- replace/delete；
- 原子快照；
- 并发安全；
- embedding manifest；
- 可测试的生命周期。

只有当项目明确要展示大规模、多实例服务或真实数据库运维时，再增加 PG adapter。

## 12. 推荐演进路线

### 阶段 0：建立真实基线

目标：知道当前 retriever 好在哪里、错在哪里。

建议交付：

- 检索标注集；
- negative/reject queries；
- Recall@K、MRR、nDCG、false positive rate；
- chunk、RRF、filter、persistence、citation 单元测试；
- vector-only、BM25-only、RRF baseline。

完成标准：任何 rewrite、rerank 或 chunker 改动都能给出可复现的前后对比。

### 阶段 1：正确性和隔离

目标：先保证不会串库、不会残留旧数据、不会产生错误引用。

建议交付：

- `kb_id` 和 metadata filter；
- document replace/delete/version/hash；
- stale chunk 清理；
- embedding manifest；
- invocation 级 citation registry；
- 本地持久化写锁和快照一致性。

完成标准：更新、删除、重启、并发和多次检索场景都有自动测试。

### 阶段 2：检索质量和可靠性

目标：允许正确返回零结果，并在部分依赖失败时保留知识能力。

建议交付：

- 可插拔语义 reranker；
- relevance/miss gate；
- reranker 失败回 RRF；
- vector/BM25 partial degradation；
- per-branch timeout 和 request budget；
- 分阶段指标与召回来源分析。

完成标准：相关性指标提升、无关 query 误召回下降，并通过故障注入测试。

### 阶段 3：结构化入库和高级检索

目标：提升大文档、多文档、表格和多模态问题的召回与引用质量。

候选交付：

- 结构化 rewrite；
- Markdown/table-aware chunk；
- page/section/image provenance；
- 增量 embedding；
- 文档摘要和 chunk_count；
- Index-first A/B 实验；
- 异步入库状态机。

是否进入此阶段，应由阶段 0～2 的评测结果决定。

## 13. 优先级、收益与成本概览

| 能力 | 价值 | 成本 | 建议 |
|---|---:|---:|---|
| Retrieval benchmark 与组件测试 | 极高 | 中 | 立即建设 |
| Reliable miss gate | 极高 | 中 | 立即建设 |
| 语义 reranker + RRF fallback | 高 | 中 | P0 |
| kb/metadata scope | 极高 | 中 | P0 |
| replace/delete/version/hash | 极高 | 中 | P0 |
| Citation 全局 ID/provenance | 高 | 低～中 | P0 |
| 分支 timeout/partial degradation | 高 | 中 | P0 |
| 分阶段指标 | 高 | 低～中 | P0 |
| 结构化 rewrite | 中～高 | 中 | P1，需 A/B |
| Markdown/table-aware chunk | 高 | 中～高 | P1 |
| 增量 embedding | 中～高 | 中 | P1 |
| Index-first | 高但依赖场景 | 中～高 | P1/P2，需大知识库 |
| 异步入库任务 | 中 | 中～高 | 文档处理变重后再做 |
| FAQ exact hit | 场景相关 | 中 | 有明确 FAQ 场景再做 |
| PostgreSQL adapter | 场景相关 | 高 | 需要多实例/规模化时再做 |
| sxw_agentic_arag 整体搬运 | 当前较低 | 高 | 不建议 |
| GraphRAG | 当前较低 | 很高 | 不建议 |

## 14. 预期能力提升

如果仅完成阶段 0～2，不引入 GraphRAG 或新的 Agent Loop，预计当前项目可以从：

```text
架构形状覆盖：75%～85%
生产能力深度：30%～40%
```

提升到大约：

```text
架构形状覆盖：85%～90%
生产能力深度：55%～65%
```

这里的提升主要来自：

- 数据和引用正确性；
- 可返回零结果；
- 多知识库隔离；
- 文档更新、删除和恢复；
- 召回分支故障降级；
- 可量化的检索质量门禁。

这些能力比增加 Agentic/Graph 标签更能支撑“生产级 RAG”叙事。

## 15. 最终建议

### 15.1 总体判断

建议继续从 `lippi-arag` 吸收能力，但下一阶段主题应明确为：

> **RAG 正确性、隔离、生命周期、降级和可评测性。**

而不是：

> 再增加一套 RAG Agent Loop、GraphStore 或更多尚无评测依据的召回策略。

### 15.2 推荐决策

1. 当前 RAG 架构不需要推倒重来；现有服务和组件边界可以保留。
2. 先建立 retrieval benchmark，再决定 rewrite、rerank、chunk 等策略。
3. 优先解决零结果、跨库隔离、旧 chunk、持久化一致性和引用冲突。
4. 选择性迁移 production reranker、分支降级、指标和生命周期设计。
5. 在知识库规模和评测证明收益后，再引入 Index-first。
6. 暂不搬运 `sxw_agentic_arag`、GraphRAG、企业内部治理和半成品 backend。
7. 后续每项能力都应同步更新 README、RUNBOOK、评测资料和能力边界说明。

## 16. 关键证据索引

### 16.1 当前项目

| 主题 | 文件 |
|---|---|
| ARAG 服务入口 | `arag/main.py` |
| Context 和 Store 装配 | `arag/context.py` |
| 索引流程 | `arag/api/index.py` |
| 检索 API | `arag/api/retrieve.py` |
| 混合召回编排 | `arag/components/retriever.py` |
| Query rewrite | `arag/components/rewrite.py` |
| RRF | `arag/components/reranker.py` |
| 低价值过滤 | `arag/components/filter.py` |
| LocalVectorStore | `arag/store/vector_store.py` |
| LocalBM25Index | `arag/store/fulltext_index.py` |
| GraphStore 占位 | `arag/store/graph_store.py` |
| 文档处理 | `arag/processor/document.py` |
| 图片 caption | `arag/processor/image.py` |
| Agent 检索工具 | `agent/tools/knowledge_search.py` |
| CitationInjector | `agent/citation/citation_injector.py` |
| Web 文档解析 | `web/app.js` |
| 评测规则 | `eval/harness/scorers/rule_scorer.py` |
| 历史受控评测 | `eval/eval_docs/first_eval/runs/20260630-103347-final/report.md` |

### 16.2 lippi-arag

参考根目录：

```text
/Users/shixiangweii/PycharmProjects/arag_learn_proj/lippi-arag
```

| 主题 | 文件 |
|---|---|
| 默认 Albert API | `app/api/albert_rag.py` |
| 默认问答 Pipeline | `app/services/albert_pipeline_chat_runner.py` |
| 向量 + FTS Retriever | `app/components/retriever/albert_pgvector_retriever.py` |
| 语义 Reranker | `app/components/reranker/arag_reranker.py` |
| 独立 Search 与 RRF | `app/api/search.py` |
| Batch Retrieve | `app/api/retrieve.py` |
| 通用 Rewrite | `app/components/rewrite/common_rewriter.py` |
| Rewrite Prompt | `app/components/prompt/rewriter_prompt.py` |
| PG/ADB Store | `app/components/store/albert_pgvector_store.py` |
| Document Store | `app/components/store/document_store.py` |
| Request Scope | `app/models/request_context.py` |
| 文档主处理器 | `app/processor/document/doc_processor.py` |
| 异步文档处理 | `app/processor/document/async_document_processor.py` |
| Markdown Chunker | `app/components/chunker/markdown_to_entries.py` |
| Table Chunker | `app/components/chunker/table_chunker.py` |
| Index-first | `app/components/retriever/index_first_retriever.py` |
| 文档目录构建 | `app/components/retriever/index_md_builder.py` |
| Greenfield Agentic Runner | `sxw_agentic_arag/runner.py` |
| Agentic Planner | `sxw_agentic_arag/planner.py` |
| Agentic Evidence | `sxw_agentic_arag/evidence.py` |
| 未实现 WithinDocAdapter | `sxw_agentic_arag/adapters/index_adapter.py` |

---

本文为只读评估结论。文档中提到的缺口、优先级和预期覆盖率均为后续方案输入，不表示相关代码已经完成。
