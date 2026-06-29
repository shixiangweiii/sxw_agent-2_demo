"""skill-center 线协议契约（agent 客户端 与 skill-center 服务共享）。

精简复刻 albert-agent-2 `app/infra/boundary/albert_skill_center/models.py` 的核心 DTO；
JSON 字段用 camelCase alias，与真实技能中心一致。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# 算粒不足类错误码（命中即视为配额超限）
QUOTA_ERROR_CODES = {"BENEFIT_NOT_ENOUGH", "BENEFIT_EXCEED", "USER_NOT_PERMISSION", "ABILITY_AI_CREDITS_LIMIT"}


class SkillResultDataType(str, Enum):
    TEXT = "TEXT"
    CARD = "CARD"
    JSON = "JSON"
    IMAGE = "IMAGE"
    FILE = "FILE"
    CHART = "CHART"


# ---------- 执行上下文 ----------
class SessionInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    open_conversation_id: Optional[str] = Field(default=None, alias="openConversationId")
    history: Optional[list[dict[str, Any]]] = None
    run_id: Optional[str] = Field(default=None, alias="runId")


class UserInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_id: Optional[str] = Field(default=None, alias="userId")
    corp_id: Optional[str] = Field(default=None, alias="corpId")
    user_name: Optional[str] = Field(default=None, alias="userName")
    uid: Optional[int] = None
    org_id: Optional[int] = Field(default=None, alias="orgId")


class InputInfo(BaseModel):
    text: Optional[str] = None
    pictures: Optional[list[Optional[str]]] = None


class AgentInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    agent_code: Optional[str] = Field(default=None, alias="agentCode")
    agent_name: Optional[str] = Field(default=None, alias="agentName")


class InvocationInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invocation_source: Optional[str] = Field(default=None, alias="invocationSource")


class Attributes(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_info: Optional[SessionInfo] = Field(default=None, alias="sessionInfo")
    user_info: Optional[UserInfo] = Field(default=None, alias="userInfo")
    input_info: Optional[InputInfo] = Field(default=None, alias="inputInfo")
    agent_info: Optional[AgentInfo] = Field(default=None, alias="agentInfo")
    invocation_info: Optional[InvocationInfo] = Field(default=None, alias="invocationInfo")


class SkillToolExecuteContext(BaseModel):
    source: Optional[str] = Field(default="AI_AGENT")
    attributes: Optional[Attributes] = None


# ---------- 执行请求 ----------
class SkillToolExecuteRequestDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    tenant_id: str = Field(alias="tenantId")
    skill_id: str = Field(alias="skillId")
    tool_name: str = Field(alias="toolName")
    arguments: Optional[dict[str, Any]] = None
    meta: Optional[dict[str, Any]] = None
    context: Optional[SkillToolExecuteContext] = None


# ---------- 同步结果（MCP 形态）----------
class SkillToolExecuteResultDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    content: Optional[list[dict[str, Any]]] = None
    structured_content: Optional[dict[str, Any]] = Field(default=None, alias="structuredContent")
    is_error: Optional[bool] = Field(default=False, alias="isError")
    error_message: Optional[str] = Field(default=None, alias="errorMessage")


# ---------- 流式结果（富信封）----------
class SkillResultDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    success: Optional[bool] = True
    request_id: Optional[str] = Field(default=None, alias="requestId")
    error_message: Optional[str] = Field(default=None, alias="errorMsg")
    error_code: Optional[str] = Field(default=None, alias="errorCode")
    seq: Optional[int] = None
    data_type: Optional[SkillResultDataType] = Field(default=None, alias="dataType")
    eof: Optional[bool] = False
    data: Optional[Any] = None
    is_display: Optional[bool] = Field(default=False, alias="isDisplay")
    is_thinking: Optional[bool] = Field(default=False, alias="isThinking")
    is_partial: Optional[bool] = Field(default=True, alias="isPartial")
    skip_summarization: Optional[bool] = Field(default=False, alias="skipSummarization")
    is_append_session: Optional[bool] = Field(default=True, alias="isAppendSession")
    custom_metadata: Optional[dict[str, Any]] = Field(default=None, alias="customMetadata")


# ---------- 技能目录 ----------
class SkillToolDef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    description: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")


class SkillDef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    skill_id: str = Field(alias="skillId")
    name: str
    description: str
    tools: list[SkillToolDef] = Field(default_factory=list)


class SkillListRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    tenant_id: str = Field(alias="tenantId")
    snapshot_tag: Optional[str] = Field(default="PUBLISHED", alias="snapshotTag")


class SkillListResult(BaseModel):
    skills: list[SkillDef] = Field(default_factory=list)
