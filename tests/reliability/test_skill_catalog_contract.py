from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import agent.skills.catalog as catalog_module
from agent.config import AgentSettings
from common.skill_contract import SkillListResult


def _valid_skill() -> dict[str, Any]:
    return {
        "skillId": "skill-current",
        "name": "Current skill",
        "description": "Strict current catalog entry",
        "tools": [{
            "name": "current_tool",
            "description": "Current tool",
            "inputSchema": {"type": "object", "properties": {}},
        }],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"skills": [_valid_skill() | {"tools": None}]},
        {"skills": [{key: value for key, value in _valid_skill().items() if key != "tools"}]},
        {"skills": [_valid_skill() | {"unexpected": True}]},
        {
            "skills": [
                _valid_skill(),
                _valid_skill() | {
                    "tools": [_valid_skill()["tools"][0] | {"unexpected": True}],
                },
            ],
        },
        {"skills": [_valid_skill() | {"name": 123}]},
        {"skills": [], "unexpected": True},
    ],
)
def test_successful_skill_catalog_dto_rejects_missing_extra_or_coerced_fields(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        SkillListResult.model_validate(payload)


def test_successful_skill_catalog_accepts_explicit_empty_or_strict_entries() -> None:
    assert SkillListResult.model_validate({"skills": []}).skills == []
    result = SkillListResult.model_validate({"skills": [_valid_skill()]})
    assert result.skills[0].tools[0].name == "current_tool"


class _CatalogResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _CatalogClient:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        failure: httpx.HTTPError | None = None,
    ) -> None:
        self._payload = payload
        self._failure = failure

    async def __aenter__(self) -> _CatalogClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> _CatalogResponse:
        if self._failure is not None:
            raise self._failure
        assert self._payload is not None
        return _CatalogResponse(self._payload)


@pytest.mark.asyncio
async def test_answered_skill_catalog_with_one_malformed_entry_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = _valid_skill()
    malformed["tools"] = [malformed["tools"][0] | {"undeclared": "field"}]
    client = _CatalogClient(payload={
        "success": True,
        "result": {"skills": [_valid_skill(), malformed]},
    })
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", lambda **_kwargs: client)

    with pytest.raises(ValidationError):
        await catalog_module.load_skill_tools(AgentSettings(_env_file=None))


@pytest.mark.asyncio
async def test_skill_catalog_transport_failure_remains_best_effort_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://skill-center.test/catalog")
    client = _CatalogClient(failure=httpx.ConnectError("down", request=request))
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", lambda **_kwargs: client)

    assert await catalog_module.load_skill_tools(AgentSettings(_env_file=None)) == []
