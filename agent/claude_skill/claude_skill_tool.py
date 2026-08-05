"""把一个完整 Claude SKILL 包包装为主 Agent 的标准 Agent-as-Tool。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from google.adk.tools import BaseTool, ToolContext
from google.adk.tools._gemini_schema_util import _to_gemini_schema
from google.genai.types import FunctionDeclaration

from agent.claude_skill.catalog import ClaudeSkill
from agent.claude_skill.contracts import (
    SKILL_EXECUTION_FAILED,
    SKILL_INSTRUCTION_NOT_READ,
    SKILL_PACKAGE_INVALID,
    SKILL_SANDBOX_UNAVAILABLE,
    SKILL_TIMEOUT,
    SkillCallContext,
    SkillCallResult,
    SkillExecutionFailedError,
    SkillInstructionNotReadError,
    SkillPackageInvalidError,
    SkillRunProgress,
    SkillRuntimeConfig,
)
from agent.claude_skill.execution_coordinator import SkillExecutionCoordinator
from agent.claude_skill.sandbox.base import BaseSandbox, SandboxUnavailableError
from agent.claude_skill.sandbox.factory import build_sandbox
from agent.claude_skill.skill_runner import SkillUiEmitter, run_skill
from agent.skills.call_identity import resolve_skill_call_identity
from agent.skills.ui_event_queue import emit_skill_event
from agent.stream.event_converters import StreamEvent
from common.obs import get_logger, log_kv

logger = get_logger("agent.claude_skill")


async def _close_sandbox(sandbox: BaseSandbox) -> None:
    """清理不受调用总超时影响；新取消到达时也先等清理任务收口。"""
    close_task = asyncio.create_task(sandbox.close())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(close_task)
        except Exception as exc:  # noqa: BLE001 - 保留原始取消语义
            log_kv(
                logger,
                logging.WARNING,
                "ClaudeSkill",
                "sandbox close failed after cancellation",
                error=type(exc).__name__,
            )
        raise
    except Exception as exc:  # noqa: BLE001 - 清理失败不覆盖已有 ToolResult
        log_kv(
            logger,
            logging.WARNING,
            "ClaudeSkill",
            "sandbox close failed",
            error=type(exc).__name__,
        )


class ClaudeSkillTool(BaseTool):
    def __init__(
        self,
        *,
        skill: ClaudeSkill,
        llm: Any,
        sandbox_provider: str,
        coordinator: SkillExecutionCoordinator,
        runtime_config: SkillRuntimeConfig,
    ) -> None:
        super().__init__(
            name=skill.tool_name,
            description=f"{skill.name}：{skill.description}（完整 SKILL 包在独立沙箱中由子代理执行）",
        )
        self._skill = skill
        self._llm = llm
        self._provider = sandbox_provider
        self._coordinator = coordinator
        self._runtime_config = runtime_config

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=_to_gemini_schema({
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "交给该技能完成的自包含任务描述，保留必要输入与约束",
                    },
                },
                "required": ["query"],
            }),
        )

    def _detect_error_in_response(self, response: Any) -> Optional[str]:
        if isinstance(response, dict) and response.get("isError"):
            error = response.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"]
            return SKILL_EXECUTION_FAILED
        return None

    def _failure(
        self,
        *,
        call: SkillCallContext,
        progress: SkillRunProgress,
        summary: str,
        code: str,
        retryable: bool,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        return SkillCallResult.failure(
            skill_call_id=call.skill_call_id,
            skill_id=self._skill.skill_id,
            summary=summary,
            code=code,
            retryable=retryable,
            attempts=progress.attempts if attempts is None else attempts,
            max_chars=self._runtime_config.result_max_chars,
        ).to_dict()

    async def run_async(
        self,
        *,
        args: dict[str, Any],
        tool_context: Optional[ToolContext],
    ) -> dict[str, Any]:
        identity = resolve_skill_call_identity(
            tool_context,
            logger=logger,
            tag="ClaudeSkill",
            skill=self._skill.skill_id,
        )
        call = SkillCallContext(
            parent_invocation_id=identity.parent_invocation_id,
            skill_call_id=identity.skill_call_id,
        )
        raw_query = (args or {}).get("query")
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        progress = SkillRunProgress()
        sandbox = build_sandbox(self._provider)

        async def emit(event: StreamEvent) -> None:
            await emit_skill_event(event)

        emitter = SkillUiEmitter(
            ui_emit=emit,
            call=call,
            skill_id=self._skill.skill_id,
        )
        log_kv(
            logger,
            logging.INFO,
            "ClaudeSkill",
            "invoke",
            skill=self._skill.skill_id,
            skill_call_id=call.skill_call_id,
            parent_invocation_id=call.parent_invocation_id,
            provider=self._provider,
        )

        response: dict[str, Any]
        try:
            if not query:
                response = self._failure(
                    call=call,
                    progress=progress,
                    summary="技能任务 query 必须是非空字符串",
                    code=SKILL_EXECUTION_FAILED,
                    retryable=True,
                )
            else:
                async with asyncio.timeout(self._runtime_config.call_timeout_seconds):
                    await emitter.emit("status", {"phase": "queued"})
                    async with self._coordinator.acquire(
                        parent_invocation_id=call.parent_invocation_id,
                        parallel_safe=self._skill.parallel_safe,
                        exclusive_resources=self._skill.exclusive_resources,
                    ):
                        result = await run_skill(
                            skill=self._skill,
                            query=query,
                            sandbox=sandbox,
                            llm=self._llm,
                            emitter=emitter,
                            config=self._runtime_config,
                            progress=progress,
                        )
                        response = SkillCallResult.success(
                            skill_call_id=call.skill_call_id,
                            skill_id=self._skill.skill_id,
                            summary=result.summary,
                            attempts=result.attempts,
                            max_chars=self._runtime_config.result_max_chars,
                        ).to_dict()
        except asyncio.TimeoutError:
            response = self._failure(
                call=call,
                progress=progress,
                summary=f"技能调用超过 {self._runtime_config.call_timeout_seconds:g} 秒总时限",
                code=SKILL_TIMEOUT,
                retryable=True,
            )
        except SandboxUnavailableError as exc:
            response = self._failure(
                call=call,
                progress=progress,
                summary=str(exc),
                code=SKILL_SANDBOX_UNAVAILABLE,
                retryable=False,
            )
        except SkillPackageInvalidError as exc:
            response = self._failure(
                call=call,
                progress=progress,
                summary=str(exc),
                code=SKILL_PACKAGE_INVALID,
                retryable=False,
            )
        except SkillInstructionNotReadError as exc:
            response = self._failure(
                call=call,
                progress=progress,
                summary=str(exc),
                code=SKILL_INSTRUCTION_NOT_READ,
                retryable=False,
                attempts=exc.attempts,
            )
        except SkillExecutionFailedError as exc:
            response = self._failure(
                call=call,
                progress=progress,
                summary=str(exc),
                code=SKILL_EXECUTION_FAILED,
                retryable=True,
                attempts=exc.attempts,
            )
        except asyncio.CancelledError:
            log_kv(
                logger,
                logging.INFO,
                "ClaudeSkill",
                "cancelled",
                skill=self._skill.skill_id,
                skill_call_id=call.skill_call_id,
                parent_invocation_id=call.parent_invocation_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - Tool 边界降级，父 turn 继续
            log_kv(
                logger,
                logging.ERROR,
                "ClaudeSkill",
                "unexpected skill failure",
                skill=self._skill.skill_id,
                skill_call_id=call.skill_call_id,
                error=type(exc).__name__,
            )
            response = self._failure(
                call=call,
                progress=progress,
                summary=f"技能执行失败：{type(exc).__name__}",
                code=SKILL_EXECUTION_FAILED,
                retryable=True,
            )
        finally:
            await _close_sandbox(sandbox)

        if response.get("isError"):
            error = response.get("error") or {}
            await emitter.emit("status", {
                "phase": "failed",
                "errorCode": error.get("code"),
            })
            log_kv(
                logger,
                logging.WARNING,
                "ClaudeSkill",
                "invoke failed",
                skill=self._skill.skill_id,
                skill_call_id=call.skill_call_id,
                parent_invocation_id=call.parent_invocation_id,
                error_code=error.get("code"),
                attempts=response.get("meta", {}).get("attempts"),
            )
        return response
