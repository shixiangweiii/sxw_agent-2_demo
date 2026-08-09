from __future__ import annotations

from agent.skills.request_context import (
    SkillRequestContext,
    reset_request_context,
    set_request_context,
)
from agent.tools.knowledge_search import _retrieval_request


def test_retrieval_context_is_stable_and_carries_runtime_budget() -> None:
    token = set_request_context(SkillRequestContext(
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id="attempt-local",
        run_id="run_123",
        activity_id="act_tool_123",
        deadline_at_ms=1_800_000_000_000,
        idempotency_key="tool_execution_123",
    ))
    try:
        first = _retrieval_request("可靠性架构")
        replay = _retrieval_request("可靠性架构")
    finally:
        reset_request_context(token)

    assert replay == first
    assert first == {
        "query": "可靠性架构",
        "top_k": 6,
        "query_id": first["query_id"],
        "run_id": "run_123",
        "activity_id": "act_tool_123",
        "principal_id": "demo-user",
        "scope": "public",
        "datasets": ["default"],
        "deadline_at": "2027-01-15T08:00:00Z",
    }
    assert first["query_id"].startswith("qry_")


def test_retrieval_context_has_safe_non_runtime_defaults() -> None:
    assert _retrieval_request("hello") == {
        "query": "hello",
        "top_k": 6,
        "scope": "public",
        "datasets": ["default"],
    }
