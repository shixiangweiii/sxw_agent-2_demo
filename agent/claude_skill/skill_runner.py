"""在独立 ADK 子 Runner 中执行一个完整 Claude SKILL 包。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.claude_skill.catalog import ClaudeSkill
from agent.claude_skill.contracts import (
    SkillCallContext,
    SkillExecutionFailedError,
    SkillInstructionNotReadError,
    SkillPackageInvalidError,
    SkillRunProgress,
    SkillRunResult,
    SkillRuntimeConfig,
)
from agent.claude_skill.sandbox.base import (
    BaseSandbox,
    SandboxError,
    SandboxUnavailableError,
)
from agent.claude_skill.toolset import SkillToolsetState, build_sandbox_tools
from agent.plugins.tool_args_guard_plugin import ToolArgsGuardPlugin
from agent.stream.event_converters import StreamEvent, adk_event_to_stream_events
from common.obs import get_logger, log_kv

logger = get_logger("agent.claude_skill")

_APP = "claude-skill"
_SYSTEM_INSTRUCTION = """你是单技能执行代理。一个完整 SKILL 包已经放入本次独立沙箱。
必须把用户消息中指定的 SKILL.md 作为第一个工具读取动作；成功读取前不得调用其他工具。
完整遵循 SKILL.md，并按需读取包内 scripts、references 和资源文件。所有路径均相对沙箱工作目录。
工具失败时基于返回信息修正；不要假设沙箱文件会在本次调用结束后继续存在。
完成任务后只输出面向父 Agent 的简洁、高信号终局总结，不复述内部推理或工具流水。
"""


async def _await_cleanup(
    awaitable: Awaitable[Any],
    *,
    label: str,
    suppress_errors: bool,
) -> None:
    """等待 Runner 清理；既有异常（尤其取消）不被清理失败覆盖。"""
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception as exc:  # noqa: BLE001 - 保留原始取消语义
            log_kv(
                logger,
                logging.WARNING,
                "ClaudeSkill",
                f"{label} failed after cancellation",
                error=type(exc).__name__,
            )
        raise
    except Exception as exc:
        if not suppress_errors:
            raise
        log_kv(
            logger,
            logging.WARNING,
            "ClaudeSkill",
            f"{label} failed while preserving primary error",
            error=type(exc).__name__,
        )


@dataclass
class SkillUiEmitter:
    ui_emit: Callable[[StreamEvent], Awaitable[None]]
    call: SkillCallContext
    skill_id: str
    seq: int = 0

    async def emit(self, sub_event: str, data: dict[str, Any] | None = None) -> None:
        self.seq += 1
        payload = dict(data or {})
        payload.update({
            "parentInvocationId": self.call.parent_invocation_id,
            "skillCallId": self.call.skill_call_id,
            "skill": self.skill_id,
            "seq": self.seq,
            "subEvent": sub_event,
        })
        await self.ui_emit(StreamEvent("skill_event", payload))


def _terminal_text(event: Any, agent_name: str) -> str | None:
    """只接受子 Agent 最后一轮、非 thought、无工具调用的聚合 model 文本。"""
    content = getattr(event, "content", None)
    if (
        getattr(event, "author", None) != agent_name
        or bool(getattr(event, "partial", False))
        or content is None
        or getattr(content, "role", None) != "model"
        or not event.is_final_response()
        or event.get_function_calls()
        or event.get_function_responses()
    ):
        return None
    text = "".join(
        part.text
        for part in (getattr(content, "parts", None) or [])
        if getattr(part, "text", None) and not bool(getattr(part, "thought", False))
    ).strip()
    return text or None


async def _run_attempt(
    *,
    skill: ClaudeSkill,
    query: str,
    sandbox: BaseSandbox,
    llm: Any,
    config: SkillRuntimeConfig,
    instruction_path: str,
    emitter: SkillUiEmitter,
    state: SkillToolsetState,
) -> str | None:
    agent_name = f"skill_{skill.skill_id}".replace("-", "_")
    agent = LlmAgent(
        name=agent_name,
        model=llm,
        instruction=_SYSTEM_INSTRUCTION,
        tools=build_sandbox_tools(sandbox, state),
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text=(
            f"技能根目录：skills/{skill.skill_id}\n"
            f"必须首先读取：{instruction_path}\n\n"
            f"待完成任务：\n{query}"
        ))],
    )
    final_text: str | None = None
    runner = InMemoryRunner(
        agent=agent,
        app_name=_APP,
        plugins=[ToolArgsGuardPlugin()],
    )
    runner_failed = False
    try:
        session = await runner.session_service.create_session(
            app_name=_APP,
            user_id="skill",
        )
        events = runner.run_async(
            user_id="skill",
            session_id=session.id,
            new_message=message,
            run_config=RunConfig(
                streaming_mode=StreamingMode.SSE,
                max_llm_calls=config.max_llm_calls,
            ),
        )
        events_failed = False
        try:
            async for event in events:
                candidate = _terminal_text(event, agent_name)
                if candidate:
                    final_text = candidate
                for stream_event in adk_event_to_stream_events(event):
                    # 子代理正文（含终局聚合文本）不进入 UI；主 Agent 是唯一最终答复者。
                    if stream_event.event in {"tool_call", "tool_result"}:
                        await emitter.emit(stream_event.event, stream_event.data)
        except BaseException:
            events_failed = True
            raise
        finally:
            await _await_cleanup(
                events.aclose(),
                label="runner event stream close",
                suppress_errors=events_failed,
            )
    except BaseException:
        runner_failed = True
        raise
    finally:
        await _await_cleanup(
            runner.close(),
            label="runner close",
            suppress_errors=runner_failed,
        )
    return final_text


async def run_skill(
    *,
    skill: ClaudeSkill,
    query: str,
    sandbox: BaseSandbox,
    llm: Any,
    emitter: SkillUiEmitter,
    config: SkillRuntimeConfig,
    progress: SkillRunProgress,
) -> SkillRunResult:
    """装载整包并运行；instruction 未读只在全局调用超时内新会话重试一次。"""
    await sandbox.try_create()
    instruction_path = f"skills/{skill.skill_id}/SKILL.md"
    try:
        await sandbox.stage_directory(skill.root_dir, f"skills/{skill.skill_id}")
    except SandboxUnavailableError:
        raise
    except (SandboxError, OSError) as exc:
        raise SkillPackageInvalidError(f"技能包装载失败：{exc}") from exc

    log_kv(
        logger,
        logging.INFO,
        "ClaudeSkill",
        "sandbox ready",
        skill=skill.skill_id,
        skill_call_id=emitter.call.skill_call_id,
        parent_invocation_id=emitter.call.parent_invocation_id,
        sandbox_session_id=sandbox.get_session_id(),
        provider=sandbox.get_sandbox_provider().value,
    )
    await emitter.emit("status", {
        "phase": "started",
        "sandboxSessionId": sandbox.get_session_id(),
    })

    for attempt in (1, 2):
        progress.attempts = attempt
        tool_state = SkillToolsetState(required_instruction_path=instruction_path)
        try:
            final_text = await _run_attempt(
                skill=skill,
                query=query,
                sandbox=sandbox,
                llm=llm,
                config=config,
                instruction_path=instruction_path,
                emitter=emitter,
                state=tool_state,
            )
        except asyncio.CancelledError:
            raise
        except LlmCallsLimitExceededError as exc:
            if tool_state.instruction_compliant:
                raise SkillExecutionFailedError(
                    f"Skill Agent 达到 {config.max_llm_calls} 次模型调用上限",
                    attempts=attempt,
                ) from exc
            if attempt == 1:
                await emitter.emit("status", {
                    "phase": "retry_instruction_read",
                    "attempt": attempt,
                    "reason": "llm_call_limit",
                })
                continue
            raise SkillInstructionNotReadError(
                f"Skill Agent 两次均未先读取 {instruction_path}",
                attempts=attempt,
            ) from exc
        except Exception as exc:
            raise SkillExecutionFailedError(
                f"Skill Agent 执行失败：{type(exc).__name__}",
                attempts=attempt,
            ) from exc

        if tool_state.instruction_compliant:
            if not final_text:
                raise SkillExecutionFailedError(
                    "Skill Agent 未返回有效终局文本",
                    attempts=attempt,
                )
            await emitter.emit("status", {"phase": "completed", "attempt": attempt})
            log_kv(
                logger,
                logging.INFO,
                "ClaudeSkill",
                "run done",
                skill=skill.skill_id,
                skill_call_id=emitter.call.skill_call_id,
                parent_invocation_id=emitter.call.parent_invocation_id,
                sandbox_session_id=sandbox.get_session_id(),
                provider=sandbox.get_sandbox_provider().value,
                attempts=attempt,
                answer_len=len(final_text),
            )
            return SkillRunResult(summary=final_text, attempts=attempt)

        if attempt == 1:
            await emitter.emit("status", {
                "phase": "retry_instruction_read",
                "attempt": attempt,
            })

    raise SkillInstructionNotReadError(
        f"Skill Agent 两次均未先读取 {instruction_path}",
        attempts=progress.attempts,
    )
