from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import agent.api.documents as documents_api
import agent.tools.knowledge_search as knowledge_search_module


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "http://arag/v1/index")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream failed", request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeAragClient:
    response = _FakeResponse(202, {"accepted_docs": 1, "job_ids": ["ijob-1"]})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return self.response

    async def get(self, *args, **kwargs):
        return _FakeResponse(200, {"job_id": "ijob-1", "state": "ACTIVATED"})


class _FailingAragClient(_FakeAragClient):
    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("arag unavailable")


@pytest.mark.asyncio
async def test_agent_document_proxy_preserves_202_and_job_poll_contract(monkeypatch) -> None:
    real_async_client = httpx.AsyncClient
    _FakeAragClient.response = _FakeResponse(
        202, {"accepted_docs": 1, "job_ids": ["ijob-1"]}
    )
    monkeypatch.setattr(documents_api.httpx, "AsyncClient", _FakeAragClient)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        arag_base_url="http://arag", arag_timeout_ms=1000,
    )
    app.include_router(documents_api.router)
    async with real_async_client(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        accepted = await client.post(
            "/api/v1/documents/index",
            json={"documents": [{"doc_id": "web:file.txt", "content": "body"}]},
        )
        assert accepted.status_code == 202
        assert accepted.json()["job_ids"] == ["ijob-1"]
        activated = await client.get("/api/v1/documents/index/jobs/ijob-1")
        assert activated.status_code == 200
        assert activated.json()["state"] == "ACTIVATED"


@pytest.mark.asyncio
async def test_agent_document_proxy_rejects_malformed_job_response(monkeypatch) -> None:
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(documents_api.httpx, "AsyncClient", _FakeAragClient)
    _FakeAragClient.response = _FakeResponse(202, {"accepted_docs": 1, "job_ids": []})
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        arag_base_url="http://arag", arag_timeout_ms=1000,
    )
    app.include_router(documents_api.router)
    async with real_async_client(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        response = await client.post(
            "/api/v1/documents/index",
            json={"documents": [{"doc_id": "web:file.txt", "content": "body"}]},
        )
        assert response.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retrieval_status", "expected_text", "expected_degraded"),
    [
        ("ERROR", "未能访问知识库", True),
        ("DEGRADED", "降级", True),
        ("DENIED", "访问被拒绝", False),
        ("MISS", "未找到相关资料", False),
    ],
)
async def test_knowledge_tool_preserves_retrieval_status_semantics(
    monkeypatch, retrieval_status, expected_text, expected_degraded
) -> None:
    real_response = _FakeAragClient.response
    _FakeAragClient.response = _FakeResponse(200, {
        "status": retrieval_status,
        "query_id": "qry-1",
        "rewrites": ["q"],
        "chunks": [],
        "cost_ms": 1,
    })
    monkeypatch.setattr(knowledge_search_module.httpx, "AsyncClient", _FakeAragClient)
    try:
        tool = knowledge_search_module.build_knowledge_search_tool(
            SimpleNamespace(arag_base_url="http://arag", arag_timeout_ms=1000)
        )
        result = await tool("q")
    finally:
        _FakeAragClient.response = real_response
    assert result["degraded"] is expected_degraded
    assert expected_text in result["note"]
    assert result["__evidence_set__"]["retrieval_status"] == retrieval_status


@pytest.mark.asyncio
async def test_knowledge_transport_failure_commits_error_evidence_semantics(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        knowledge_search_module.httpx, "AsyncClient", _FailingAragClient,
    )
    tool = knowledge_search_module.build_knowledge_search_tool(
        SimpleNamespace(arag_base_url="http://arag", arag_timeout_ms=1000)
    )

    result = await tool("q")

    assert result["degraded"] is True
    assert result["__evidence_set__"]["retrieval_status"] == "ERROR"
    assert result["__evidence_set__"]["degraded_reasons"] == ["ConnectError"]
    assert "未能访问知识库" in result["note"]
