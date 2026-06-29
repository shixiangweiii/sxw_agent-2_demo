# 02 · 技术栈与配置

## 1. 技术栈（对齐原项目）

| 层 | 选型 | 对应原项目 |
|---|---|---|
| 语言 | Python 3.11（兼容 3.10） | albert-agent-2 / lippi-arag |
| Agent 框架 | **Google ADK**（`google-adk`，PyPI 公版） | `from google.adk.*` |
| 模型适配 | **LiteLlm**（ADK `google.adk.models.lite_llm.LiteLlm` 子类） | `app/.../litellm/qwen_lite_llm.py` |
| Web / 流式 | **FastAPI** + `uvicorn` + `StreamingResponse`(SSE) | `app/lumi/api/lumi_chat.py` |
| RAG 框架 | 自研组件（chunker/retriever/reranker…）+ `jieba` 分词 | `lippi-arag/app/components/*` |
| 向量计算 | `numpy`（本地余弦） | 生产可换 pgvector / Milvus |
| 全文检索 | `rank_bm25` + `jieba` | 生产可换 Elasticsearch |
| 配置 | `pydantic-settings`（env 驱动） | — |
| HTTP 客户端 | `httpx`（agent→arag） | — |

> **ADK 公版校验（M0 必做）**：确认公版 `google-adk` 暴露所需类——
> `BasePlugin` / `BaseLlmRequestProcessor` / `LiteLlm` / `AgentTool` / `BaseSessionService` / `BaseArtifactService` / `types.Content` / `Event`。
> 若某类在公版路径不同，按公版 API 适配（demo 是干净重写，不依赖内部 fork）。

---

## 2. LLM 配置（已验证可用，见 `05`）

| 用途 | 模型 | 端点 | 验证 |
|---|---|---|---|
| 推理 + 多模态视觉 | `qwen3.7-plus` | DashScope compatible-mode | ✅ chat / vision / function-calling |
| 文本嵌入 | `text-embedding-v3`（1024 维） | 同上 | ✅ |

- **端点（非密）**：`https://dashscope.aliyuncs.com/compatible-mode/v1`（OpenAI 兼容）
- **API Key（密，绝不入库）**：运行时以环境变量 `DASHSCOPE_API_KEY` 注入；仓库内一律用占位符 `sk-***`。

### LiteLlm 接法（OpenAI 兼容）

```python
# agent/llm/hardened_litellm.py（要点）
from google.adk.models.lite_llm import LiteLlm

def build_llm() -> LiteLlm:
    # litellm 走 openai 兼容 provider：model 前缀 "openai/"，api_base + api_key 由 env 提供
    return HardenedLiteLlm(
        model="openai/qwen3.7-plus",
        api_base=settings.llm_base_url,     # https://dashscope.aliyuncs.com/compatible-mode/v1
        api_key=settings.llm_api_key,       # 来自 env，不落盘
    )
```

> 嵌入侧用 `openai` SDK 直连同端点（`base_url`+`api_key`），`model="text-embedding-v3"`。

---

## 3. 环境变量清单

| 变量 | 示例/默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | `sk-***` | **运行时注入，不入库** |
| `LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `LLM_MODEL` | `qwen3.7-plus` | 推理 + 视觉 |
| `EMBEDDING_MODEL` | `text-embedding-v3` | 1024 维 |
| `ENGINE` | `agent_loop` | `plan_execute` \| `agent_loop` |
| `AGENT_PORT` | `8000` | agent 服务端口 |
| `ARAG_PORT` | `8100` | arag 服务端口 |
| `ARAG_BASE_URL` | `http://127.0.0.1:8100` | agent→arag 调用地址 |
| `ARAG_TIMEOUT_MS` | `8000` | 检索超时（超时降级 chat-mode） |
| `VECTOR_BACKEND` | `local` | `local`(numpy + 本地持久化) \| `pgvector`… |
| `EMBEDDING_STORAGE_DIR` | `local_storage/embedding` | local 向量与 chunk 元数据目录 |
| `FULLTEXT_BACKEND` | `local` | `local`(bm25) \| `es`… |
| `GRAPH_BACKEND` | `local` | `local`(memory) \| `neo4j`（仅端口，未接流） |
| `MAX_LOOP_ITERS` | `8` | agent-loop 最大轮次 |
| `LOG_LEVEL` | `INFO` | 结构化日志级别 |

---

## 4. `.env.example` 规范（实现时生成）

```dotenv
# === LLM (DashScope, OpenAI-compatible) ===
DASHSCOPE_API_KEY=sk-***          # 运行时填入真实 key，切勿提交
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-plus
EMBEDDING_MODEL=text-embedding-v3

# === Engine ===
ENGINE=agent_loop                 # plan_execute | agent_loop
MAX_LOOP_ITERS=8

# === Services ===
AGENT_PORT=8000
ARAG_PORT=8100
ARAG_BASE_URL=http://127.0.0.1:8100
ARAG_TIMEOUT_MS=8000

# === Storage backends (ports) ===
VECTOR_BACKEND=local
EMBEDDING_STORAGE_DIR=local_storage/embedding
FULLTEXT_BACKEND=local
GRAPH_BACKEND=local

LOG_LEVEL=INFO
```

> `.gitignore` 必含 `.env`；仓库只保留 `.env.example`（占位 key）。

---

## 5. 依赖清单（`pyproject.toml` 草案）

```
google-adk            # ADK 运行时（Runner/LlmAgent/Plugin/RequestProcessor/Artifact/Session）
litellm               # 模型适配（ADK LiteLlm 依赖）
fastapi
uvicorn[standard]
httpx                 # agent→arag
pydantic>=2
pydantic-settings
openai                # 嵌入直连
numpy                 # 本地向量
rank_bm25             # 本地全文
jieba                 # 中文分词
python-multipart      # multipart 上传（多模态图片/文件）
tiktoken              # token 计数（message budget）
```
> 安装源：公网 PyPI（原项目用阿里内网源 `artlab.alibaba-inc.com`，demo 不依赖内网）。
