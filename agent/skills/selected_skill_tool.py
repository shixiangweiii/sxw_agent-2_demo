"""SelectedSkillTool：把 skill-center 的一个技能工具包装成 ADK 工具。

复刻 `BaseSelectedTool`（精简）：`_get_declaration`(input schema) + `run_async`（建上下文+DTO
→ 流式调用 → 展示帧入 UI 队列 + 聚合文本返回 LLM；算粒错误降级）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.tools import BaseTool, ToolContext
from google.adk.tools._gemini_schema_util import _to_gemini_schema
from google.genai.types import FunctionDeclaration

from agent.skills.args_coercion import coerce_args_by_schema
from agent.skills.call_identity import resolve_skill_call_identity
from agent.skills.client import SKILL_EXECUTION_ERROR, SkillCenterClient, SkillQuotaError
from agent.skills.request_context import get_request_context
from agent.skills.result_parser import extract_error, extract_text, to_skill_event
from agent.skills.ui_event_queue import emit_skill_event
from common.obs import get_logger, log_kv
from common.skill_contract import (
    AgentInfo,
    Attributes,
    InputInfo,
    InvocationInfo,
    SessionInfo,
    SkillResultDataType,
    SkillToolExecuteContext,
    SkillToolExecuteRequestDTO,
    UserInfo,
)

logger = get_logger("agent.skill")


class SelectedSkillTool(BaseTool):
    def __init__(
        self, *, skill_id: str, tool_name: str, description: str,
        input_schema: Optional[dict[str, Any]], client: SkillCenterClient,
    ) -> None:
        super().__init__(name=tool_name, description=description)
        self._skill_id = skill_id
        self._tool_name = tool_name
        self._client = client
        self._raw_schema = input_schema  # 保留原始 schema 供执行前参数 coercion
        self._input_schema = _to_gemini_schema(input_schema) if input_schema else None

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(name=self.name, description=self.description, parameters=self._input_schema)

    def _build_context(self) -> Optional[SkillToolExecuteContext]:
        rc = get_request_context()
        if rc is None:
            return None
        return SkillToolExecuteContext(attributes=Attributes(
            agentInfo=AgentInfo(agentCode=rc.agent_uuid),
            invocationInfo=InvocationInfo(invocationSource="AI_AGENT"),
            inputInfo=InputInfo(text=rc.text),
            sessionInfo=SessionInfo(sessionId=rc.session_id),
            userInfo=UserInfo(userId=rc.user_id),
        ))

    async def run_async(self, *, args: dict[str, Any], tool_context: Optional[ToolContext]) -> Any:
        rc = get_request_context()
        identity = resolve_skill_call_identity(
            tool_context,
            logger=logger,
            tag="SkillInvoke",
            skill=self._skill_id,
        )
        skill_call_id = identity.skill_call_id
        parent_invocation_id = identity.parent_invocation_id
        agent_uuid = rc.agent_uuid if rc else "demo-agent"
        user = rc.user if rc and rc.user else {"userId": "demo-user"}
        request = SkillToolExecuteRequestDTO(
            tenantId=agent_uuid, skillId=self._skill_id, toolName=self._tool_name,
            arguments=coerce_args_by_schema(args or {}, self._raw_schema),
            context=self._build_context(),
        )
        log_kv(
            logger,
            logging.INFO,
            "SkillInvoke",
            "start",
            skill=self._skill_id,
            tool=self._tool_name,
            skill_call_id=skill_call_id,
            parent_invocation_id=parent_invocation_id,
        )

        text_parts: list[str] = []
        card: Any = None
        skip_summarization = False
        error_msg: Optional[str] = None
        error_code: Optional[str] = None
        try:
            async for result in self._client.execute_tool_by_sse(request, user):
                frame_error = extract_error(result)
                if frame_error and error_msg is None:
                    # failure-sticky：首个失败最接近根因，后续成功/失败帧均不覆盖。
                    error_msg = frame_error
                    error_code = result.error_code or SKILL_EXECUTION_ERROR
                event = to_skill_event(
                    self._tool_name,
                    result,
                    skill_call_id=skill_call_id,
                    parent_invocation_id=parent_invocation_id,
                )
                if event is not None:
                    await emit_skill_event(event)
                text_parts.append(extract_text(result))
                if result.skip_summarization:
                    skip_summarization = True
                if result.data_type == SkillResultDataType.CARD:
                    card = result.data
        except SkillQuotaError as exc:
            return {"isError": True, "errorCode": exc.error_code,
                    "content": "技能算粒余额不足，请联系管理员。"}

        aggregated = "".join(text_parts).strip()
        out: dict[str, Any] = {"skill": self._tool_name, "skipSummarization": skip_summarization}
        if error_msg:
            # 终止错误优先于此前已收到的卡片/文本，避免截断流被误判为成功。
            out["skipSummarization"] = False
            out["isError"] = True
            out["errorCode"] = error_code
            out["content"] = error_msg
        elif skip_summarization:
            # 单技能直呈语义：结果已通过 skill_event 卡片直接展示给用户，
            # function_response 显式抑制父 LLM 二次总结（软控制，不依赖引擎层硬收口）。
            out["content"] = (
                "[系统] 技能结果已通过卡片直接展示给用户，请勿复述或总结其内容；"
                "如无额外补充可直接简短收尾。"
            )
        elif card is not None:
            out["card"] = card
            out["content"] = aggregated or "已生成卡片并直接展示给用户。"
        elif aggregated:
            out["content"] = aggregated
        else:
            out["content"] = "技能执行完成。"
        log_kv(logger, logging.INFO, "SkillInvoke", "done",
               skill=self._skill_id, answer_len=len(aggregated), error=bool(error_msg),
               error_code=error_code, skill_call_id=skill_call_id,
               parent_invocation_id=parent_invocation_id)
        return out
