"""信号抽取：从 CollectedRun 提取路由 / 检索 / 引用 / 上下文信号。"""
from __future__ import annotations

from typing import Any

from eval.harness.sse_client import CollectedRun

# 编排辅助工具（非"能力型"调用，路由统计时排除）
ORCHESTRATION_TOOLS = {"tool_search", "update_task_plan"}
KNOWLEDGE_TOOL = "knowledge_search"


def tool_names(run: CollectedRun) -> list[str]:
    return [name for name, _ in run.tool_calls]


def capability_calls(run: CollectedRun) -> list[str]:
    return [n for n in tool_names(run) if n not in ORCHESTRATION_TOOLS]


def _unwrap(resp: Any) -> dict[str, Any]:
    """ADK function_response 可能为原始 dict 或 {"result": dict}，统一取出。"""
    if isinstance(resp, dict):
        if isinstance(resp.get("hits"), list):
            return resp
        inner = resp.get("result")
        if isinstance(inner, dict):
            return inner
        return resp
    return {}


def knowledge_hits(run: CollectedRun) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for name, resp in run.tool_results:
        if name == KNOWLEDGE_TOOL:
            data = _unwrap(resp)
            got = data.get("hits")
            if isinstance(got, list):
                hits.extend(h for h in got if isinstance(h, dict))
    return hits


def retrieved_doc_ids(run: CollectedRun) -> set[str]:
    return {str(h.get("doc_id", "")) for h in knowledge_hits(run) if h.get("doc_id")}


def cited_doc_ids(run: CollectedRun) -> set[str]:
    out: set[str] = set()
    for ev in run.citations:
        for ref in ev.get("refs", []) or []:
            doc = str(ref.get("doc_id", ""))
            if doc:
                out.add(doc)
    return out


def has_card_skill_event(run: CollectedRun) -> bool:
    return any(str(ev.get("dataType")) == "CARD" for ev in run.skill_events)
