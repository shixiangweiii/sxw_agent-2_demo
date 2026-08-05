"""知识检索工具：httpx 调 arag /v1/retrieve；超时/失败降级为 chat-mode（不中断对话）。

这是 agent → arag 的微服务边界（含超时 + 降级），对应原项目 SmartSearchTool → albert-arag。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from agent.config import AgentSettings
from common.obs import get_logger, get_trace_id, log_kv

logger = get_logger("agent.knowledge")


def build_knowledge_search_tool(settings: AgentSettings) -> Callable[..., Any]:
    base_url = settings.arag_base_url.rstrip("/")
    timeout = settings.arag_timeout_ms / 1000.0

    async def knowledge_search(query: str) -> dict[str, Any]:
        """检索企业知识库，返回与问题相关的资料片段；回答知识型问题前应先调用本工具。

        Args:
            query: 用户的知识型问题。
        """
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
            return {"hits": [], "count": 0, "degraded": True,
                    "note": "知识检索暂不可用。请在回答开头显式声明『未能访问知识库，以下基于常识』，"
                            "再谨慎作答；不要编造引用，也不要把常识冒充为检索结果。"}

    return knowledge_search
