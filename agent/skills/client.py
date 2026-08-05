"""SkillCenterClient：agent → skill-center 调用客户端（同步 execute + 流式 execute-streaming）。

复刻 albert-agent-2 `AlbertSkillClient.execute_tool` / `execute_tool_by_sse`：
构造 header（base64 用户 + trace）+ DTO → 调技能中心 → NDJSON 流解析 SkillResultDTO，
处理 eof / 算粒 errorCode / 空结果兜底。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, AsyncGenerator, Optional

import httpx

from agent.config import AgentSettings
from agent.skills.stream_processor import SSEStreamProcessor
from common.obs import get_logger, get_trace_id, log_kv
from common.skill_contract import (
    QUOTA_ERROR_CODES,
    SkillResultDTO,
    SkillToolExecuteRequestDTO,
    SkillToolExecuteResultDTO,
)

logger = get_logger("agent.skill")


class SkillQuotaError(Exception):
    """技能算粒不足（命中配额错误码）。"""

    def __init__(self, error_code: Optional[str], error_message: Optional[str]) -> None:
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"{error_code}: {error_message}")


class SkillStreamProtocolError(ValueError):
    """skill-center 返回了无法解析或违反 DTO 契约的帧。"""


class SkillCenterClient:
    def __init__(self, settings: AgentSettings) -> None:
        self._base_url = settings.skill_center_base_url.rstrip("/")
        self._timeout = settings.skill_center_timeout_ms / 1000.0
        self._stream_timeout = settings.skill_center_stream_timeout_ms / 1000.0

    def _headers(self, tenant_id: str, user: dict[str, Any]) -> dict[str, str]:
        user_payload = base64.b64encode(json.dumps(user, ensure_ascii=False).encode()).decode()
        return {
            "X-User-Payload": user_payload,
            "X-Trace-Id": get_trace_id(),
            "X-Deap-AgentUUid": tenant_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _parse_line(line: str) -> Optional[SkillResultDTO]:
        line = line.strip()
        if not line:
            return None
        try:
            return SkillResultDTO(**json.loads(line))
        except Exception as exc:  # noqa: BLE001 - 统一转为不含原始数据的协议错误
            log_kv(logger, logging.WARNING, "SkillInvoke", "bad stream frame",
                   error=type(exc).__name__)
            raise SkillStreamProtocolError("技能流包含非法帧") from exc

    @staticmethod
    def _stream_error(error_code: str, error_message: str) -> SkillResultDTO:
        return SkillResultDTO(
            success=False,
            errorCode=error_code,
            errorMsg=error_message,
            eof=True,
            isPartial=False,
        )

    async def execute_tool(
        self, request: SkillToolExecuteRequestDTO, user: dict[str, Any],
    ) -> SkillToolExecuteResultDTO:
        url = f"{self._base_url}/api/v1/skills/runtime/execute"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, json=request.model_dump(by_alias=True),
                    headers=self._headers(request.tenant_id, user),
                )
                resp.raise_for_status()
                data = resp.json()
            if not data.get("success"):
                return SkillToolExecuteResultDTO(isError=True, errorMessage=f"{request.tool_name}调用失败")
            return SkillToolExecuteResultDTO(**data["result"])
        except Exception as exc:  # noqa: BLE001 - 失败降级为结构化错误，不抛
            log_kv(logger, logging.WARNING, "SkillInvoke", "execute failed",
                   skill=request.skill_id, error=type(exc).__name__)
            return SkillToolExecuteResultDTO(isError=True, errorMessage=f"{request.tool_name}调用异常")

    async def execute_tool_by_sse(
        self, request: SkillToolExecuteRequestDTO, user: dict[str, Any],
    ) -> AsyncGenerator[SkillResultDTO, None]:
        url = f"{self._base_url}/api/v1/skills/runtime/execute-streaming"
        processor = SSEStreamProcessor()
        timeout = httpx.Timeout(self._stream_timeout, connect=10.0)
        seen_frame = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", url, json=request.model_dump(by_alias=True),
                    headers=self._headers(request.tenant_id, user),
                ) as resp:
                    if resp.status_code != 200:
                        yield SkillResultDTO(success=False, errorMsg="技能执行异常", eof=True, isPartial=False)
                        return
                    async for chunk in resp.aiter_bytes(256):
                        for line in processor.process_chunk(chunk):
                            result = self._parse_line(line)
                            if result is None:
                                continue
                            self._raise_if_quota(request, result)
                            if result.eof:
                                yield result
                                return
                            seen_frame = True
                            yield result
                    for line in processor.finalize():
                        result = self._parse_line(line)
                        if result is None:
                            continue
                        self._raise_if_quota(request, result)
                        if result.eof:
                            yield result
                            return
                        seen_frame = True
                        yield result
                    if not seen_frame:
                        log_kv(logger, logging.WARNING, "SkillInvoke", "empty stream, no result",
                               skill=request.skill_id)
                        yield self._stream_error("SKILL_STREAM_EMPTY", "工具执行无结果")
                    else:
                        log_kv(logger, logging.WARNING, "SkillInvoke", "stream ended without eof",
                               skill=request.skill_id)
                        yield self._stream_error("SKILL_STREAM_INCOMPLETE", "技能流未正常结束")
        except SkillQuotaError:
            raise
        except SkillStreamProtocolError:
            yield self._stream_error("SKILL_PROTOCOL_ERROR", "技能流协议错误")
        except Exception as exc:  # noqa: BLE001 - 流异常降级为单帧错误，不中断 turn
            log_kv(logger, logging.WARNING, "SkillInvoke", "stream failed",
                   skill=request.skill_id, error=type(exc).__name__)
            if seen_frame:
                yield self._stream_error("SKILL_STREAM_INCOMPLETE", "技能流未正常结束")
            else:
                yield SkillResultDTO(success=False, errorMsg="技能执行异常", eof=True, isPartial=False)

    @staticmethod
    def _raise_if_quota(request: SkillToolExecuteRequestDTO, result: SkillResultDTO) -> None:
        if result.eof and result.error_code and result.error_code in QUOTA_ERROR_CODES:
            log_kv(logger, logging.WARNING, "QuotaDeny", "skill quota exceeded",
                   skill=request.skill_id, code=result.error_code)
            raise SkillQuotaError(result.error_code, result.error_message)
