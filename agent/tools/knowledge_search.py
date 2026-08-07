"""知识检索工具：httpx 调 arag /v1/retrieve；超时/失败降级为 chat-mode（不中断对话）。

这是 agent → arag 的微服务边界（含超时 + 降级），对应原项目 SmartSearchTool → albert-arag。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from agent.config import AgentSettings
from common.obs import get_logger, get_trace_id, log_kv
from common.trace import KIND_RETRIEVAL, start_span

logger = get_logger("agent.knowledge")


def build_knowledge_search_tool(settings: AgentSettings) -> Callable[..., Any]:
    base_url = settings.arag_base_url.rstrip("/")
    timeout = settings.arag_timeout_ms / 1000.0

    async def knowledge_search(query: str) -> dict[str, Any]:
        """检索企业知识库，返回与问题相关的资料片段；回答知识型问题前应先调用本工具。

        Args:
            query: 用户的知识型问题。
        """
        with start_span("retrieval.knowledge_search", KIND_RETRIEVAL) as span:
            span.set_payload("query", query)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{base_url}/v1/retrieve",
                        json={"query": query, "top_k": 6},
                        headers={"x-trace-id": get_trace_id()},   # 跨服务 trace 串联
                    )
                    resp.raise_for_status()
                    data = resp.json()
                # 给每条命中编号 n（从 1 开始）——这个编号就是模型正文里 [n] 标记的依据，
                # 也是 CitationInjector 建立"序号 → 文档"映射的键。
                chunks = data.get("chunks", [])
                hits = [
                    {"n": i + 1, "title": c.get("title", ""), "doc_id": c.get("doc_id", ""),
                     "content": c.get("content", "")}
                    for i, c in enumerate(chunks)
                ]
                # ★ 检索**质量**信号只进轨迹，绝不进上面喂给模型的 hits。
                #   arag 返回了 rewrites 与每条 chunk 的 score/source（向量/BM25/fused），
                #   有了它才分得清"召回本身差" vs "模型没用好召回"——这正是评测里
                #   引用类失败最难归因的一环。
                #   但改 hits = 改 prompt = 改模型行为，会让 eval/reports/ 的既有基线
                #   全部失去可比性，所以这些字段只落 span。
                span.set(
                    hit_count=len(hits),
                    doc_ids=[h["doc_id"] for h in hits] or None,
                    rewrites=data.get("rewrites") or None,
                    sources=[c.get("source") for c in chunks] or None,
                    scores=[round(float(c["score"]), 4) for c in chunks
                            if isinstance(c.get("score"), (int, float))] or None,
                    arag_cost_ms=data.get("cost_ms"),
                    degraded=False,
                )
                log_kv(logger, logging.INFO, "QaRetrieve", "hit", count=len(hits))
                if not hits:
                    return {"hits": [], "count": 0,
                            "note": "知识库未检索到相关资料。请明确告知未找到相关资料，再据常识谨慎作答；"
                                    "不要编造引用，也不要给出资料未提供的具体来源性事实（算法名/参数/函数名）。"}
                return {"hits": hits, "count": len(hits),
                        "note": "请基于以上资料回答，并在引用处用 [n] 标注（n 为资料序号）。"}
            # ★ 微服务边界的降级点：arag 挂了/超时不能让整轮对话失败。
            # 这里返回结构化结果而不是抛异常——属于"业务可预期失败"，
            # 模型收到 degraded 标记后会声明未访问知识库再作答，循环继续正常推进。
            except Exception as exc:  # noqa: BLE001 - 检索失败降级为纯对话，不抛错
                log_kv(logger, logging.WARNING, "QaRetrieve", "degraded, fallback to chat mode",
                       error=type(exc).__name__)
                # 降级是**预期内**的业务行为，不是 span 失败——状态保持 ok，
                # 靠 degraded 字段表达，否则评测会把每次演示降级都算成一次错误。
                span.set(degraded=True, hit_count=0, error_type=type(exc).__name__)
                return {"hits": [], "count": 0, "degraded": True,
                        "note": "知识检索暂不可用。请在回答开头显式声明『未能访问知识库，以下基于常识』，"
                                "再谨慎作答；不要编造引用，也不要把常识冒充为检索结果。"}

    return knowledge_search
