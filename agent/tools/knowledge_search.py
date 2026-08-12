"""知识检索工具：httpx 调 arag /v1/retrieve；超时/失败降级为 chat-mode（不中断对话）。

这是 agent → arag 的微服务边界（含超时 + 降级），对应原项目 SmartSearchTool → albert-arag。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

import httpx

from agent.config import AgentSettings
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    EvidenceItem,
    EvidenceSet,
    RetrievalStatus,
    ToolExecutionOutput,
    ToolResultEnvelope,
    ToolResultStatus,
    ms_to_rfc3339,
    utc_now_ms,
)
from agent.skills.request_context import get_request_context
from common.obs import get_logger, get_trace_id, log_kv
from common.trace import KIND_RETRIEVAL, start_span

logger = get_logger("agent.knowledge")


def build_knowledge_search_tool(settings: AgentSettings) -> Callable[..., Any]:
    base_url = settings.arag_base_url.rstrip("/")
    timeout = settings.arag_timeout_ms / 1000.0

    async def knowledge_search(query: str) -> ToolExecutionOutput:
        """检索企业知识库，返回与问题相关的资料片段；回答知识型问题前应先调用本工具。

        Args:
            query: 用户的知识型问题。
        """
        with start_span("retrieval.knowledge_search", KIND_RETRIEVAL) as span:
            span.set_payload("query", query)
            retrieval_request = _retrieval_request(query)
            request_context = get_request_context()
            # _retrieval_request validates the complete Runtime identity.
            assert request_context is not None
            tool_execution_id = request_context.tool_execution_id
            try:
                request_timeout = timeout
                if (
                    request_context is not None
                    and request_context.deadline_at_ms is not None
                ):
                    request_timeout = max(
                        0.001,
                        min(
                            timeout,
                            (request_context.deadline_at_ms - utc_now_ms()) / 1000,
                        ),
                    )
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    resp = await client.post(
                        f"{base_url}/v1/retrieve",
                        json=retrieval_request,
                        headers={"x-trace-id": get_trace_id()},   # 跨服务 trace 串联
                    )
                    resp.raise_for_status()
                    data = resp.json()
                # 给每条命中编号 n（从 1 开始）——这个编号就是模型正文里 [n] 标记的依据，
                # Runtime 终态事务会用该序号确定性生成 citation。
                chunks = data.get("chunks", [])
                hits = [
                    {"n": i + 1, "title": c.get("title", ""), "doc_id": c.get("doc_id", ""),
                     "content": c.get("content", "")}
                    for i, c in enumerate(chunks)
                ]
                evidence_set = _evidence_from_retrieval(
                    query=query,
                    request=retrieval_request,
                    response=data,
                    chunks=chunks,
                    tool_execution_id=tool_execution_id,
                )
                retrieval_status = evidence_set.retrieval_status.value
                degraded = retrieval_status in {"DEGRADED", "ERROR"}
                # ★ 检索**质量**信号进入轨迹与完整 EvidenceSet Artifact，绝不进入
                #   上面喂给模型的有界 hits preview。
                #   arag 返回了 rewrites 与每条 chunk 的 score/source（向量/BM25/fused），
                #   有了它才分得清"召回本身差" vs "模型没用好召回"——这正是评测里
                #   引用类失败最难归因的一环。
                #   但改 hits = 改 prompt = 改模型行为，会让 eval/reports/ 的既有基线
                #   全部失去可比性，所以这些字段只落 span / EvidenceSet authority。
                span.set(
                    hit_count=len(hits),
                    doc_ids=[h["doc_id"] for h in hits] or None,
                    rewrites=data.get("rewrites") or None,
                    sources=[c.get("source") for c in chunks] or None,
                    scores=[round(float(c["score"]), 4) for c in chunks
                            if isinstance(c.get("score"), (int, float))] or None,
                    arag_cost_ms=data.get("cost_ms"),
                    retrieval_status=retrieval_status,
                    degraded=degraded,
                )
                log_kv(logger, logging.INFO, "QaRetrieve", "hit", count=len(hits))
                if not hits:
                    if retrieval_status == "ERROR":
                        note = (
                            "知识检索发生错误。请在回答开头显式声明『未能访问知识库，以下基于常识』，"
                            "再谨慎作答；不要编造引用。"
                        )
                    elif retrieval_status == "DENIED":
                        note = "当前请求无权访问指定知识库。请明确说明资料访问被拒绝，不要编造引用。"
                    elif retrieval_status == "DEGRADED":
                        note = "知识检索处于降级状态且未命中资料。请明确说明降级，不要编造引用。"
                    else:
                        note = (
                            "知识库未检索到相关资料。请明确告知未找到相关资料，再据常识谨慎作答；"
                            "不要编造引用，也不要给出资料未提供的具体来源性事实（算法名/参数/函数名）。"
                        )
                    preview = {
                        "hits": [], "count": 0, "degraded": degraded, "note": note,
                    }
                else:
                    preview = {
                        "hits": hits,
                        "count": len(hits),
                        "degraded": degraded,
                        "note": "请基于以上资料回答，并在引用处用 [n] 标注（n 为资料序号）。",
                    }
                return ToolExecutionOutput(
                    result=ToolResultEnvelope(
                        status=ToolResultStatus.SUCCESS,
                        preview=preview,
                    ),
                    evidence=evidence_set,
                )
            except RuntimeFault:
                raise
            # ★ 微服务边界的降级点：arag 挂了/超时不能让整轮对话失败。
            # 这里返回结构化结果而不是抛异常——属于"业务可预期失败"，
            # 模型收到 degraded 标记后会声明未访问知识库再作答，循环继续正常推进。
            except Exception as exc:  # noqa: BLE001 - 检索失败降级为纯对话，不抛错
                log_kv(logger, logging.WARNING, "QaRetrieve", "degraded, fallback to chat mode",
                       error=type(exc).__name__)
                # 降级是**预期内**的业务行为，不是 span 失败——状态保持 ok，
                # 靠 degraded 字段表达，否则评测会把每次演示降级都算成一次错误。
                span.set(degraded=True, hit_count=0, error_type=type(exc).__name__)
                # A transport/unhandled ARAG failure is still a committed
                # retrieval fact.  Preserve an explicit ERROR EvidenceSet so
                # Broker/citation/audit never misclassify the fallback as a
                # healthy zero-hit MISS merely because there are no chunks.
                evidence_set = _evidence_from_retrieval(
                    query=query,
                    request=retrieval_request,
                    response={
                        "status": "ERROR",
                        "query_id": retrieval_request["query_id"],
                        "rewrites": [query],
                        "cost_ms": None,
                        "degraded_reasons": [type(exc).__name__],
                    },
                    chunks=[],
                    tool_execution_id=tool_execution_id,
                )
                return ToolExecutionOutput(
                    result=ToolResultEnvelope(
                        status=ToolResultStatus.SUCCESS,
                        preview={
                            "hits": [],
                            "count": 0,
                            "degraded": True,
                            "note": "知识检索暂不可用。请在回答开头显式声明『未能访问知识库，以下基于常识』，"
                                    "再谨慎作答；不要编造引用，也不要把常识冒充为检索结果。",
                        },
                    ),
                    evidence=evidence_set,
                )

    return knowledge_search


def _retrieval_request(query: str) -> dict[str, Any]:
    """Build the lossless Runtime → ARAG retrieval context.

    The Broker supplies both the stable execution identity and its independent
    idempotency key before invoking the function. ``query_id`` deliberately
    derives from the idempotency key so a safe READ_ONLY retry is stable, while
    Evidence authority uses the explicit ToolExecution id.
    """
    context = get_request_context()
    if context is None:
        raise RuntimeFault(
            "EVIDENCE_CONTRACT_INVALID",
            "knowledge_search requires a Runtime ToolExecution context",
            500,
        )
    if (
        not context.run_id
        or not context.activity_id
        or not context.idempotency_key
        or not context.tool_execution_id
    ):
        raise RuntimeFault(
            "EVIDENCE_CONTRACT_INVALID",
            "knowledge_search context lacks stable Runtime identities",
            500,
        )
    identity = context.idempotency_key
    query_id = "qry_" + hashlib.sha256(
        f"{identity}:{query}".encode("utf-8")
    ).hexdigest()
    return {
        "query": query,
        "top_k": 6,
        "query_id": query_id,
        "run_id": context.run_id or None,
        "activity_id": context.activity_id or None,
        "principal_id": context.user_id,
        "idempotency_key": context.idempotency_key,
        "scope": "public",
        "datasets": ["default"],
        "deadline_at": ms_to_rfc3339(context.deadline_at_ms),
    }


def _evidence_from_retrieval(
    *,
    query: str,
    request: dict[str, Any],
    response: dict[str, Any],
    chunks: list[dict[str, Any]],
    tool_execution_id: str,
) -> EvidenceSet:
    """Validate ARAG provenance as-is; missing facts are never synthesized."""

    try:
        evidence = tuple(
            EvidenceItem.model_validate(dict(chunk, n=index), strict=True)
            for index, chunk in enumerate(chunks, start=1)
        )
        return EvidenceSet(
            query=query,
            query_id=response["query_id"],
            run_id=request["run_id"],
            activity_id=request["activity_id"],
            principal_id=request["principal_id"],
            dataset_scope=tuple(request["datasets"]),
            scope=request["scope"],
            retrieval_status=RetrievalStatus(response["status"]),
            rewrites=tuple(response["rewrites"]),
            cost_ms=response.get("cost_ms"),
            degraded_reasons=tuple(response["degraded_reasons"]),
            tool_execution_id=tool_execution_id,
            evidence=evidence,
            retrieved_at=ms_to_rfc3339(utc_now_ms()),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeFault(
            "EVIDENCE_CONTRACT_INVALID",
            "ARAG response does not satisfy the current EvidenceSet contract",
            500,
            {"reason": str(exc)},
        ) from exc
