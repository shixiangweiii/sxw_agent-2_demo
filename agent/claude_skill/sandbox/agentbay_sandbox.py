"""AgentbaySandbox：阿里云 AgentBay 云沙箱适配器（桩）。

真实实现调用 wuying-agentbay-sdk 创建远程会话并提供 file/shell/code 等服务（见原项目
`app/core/claude_skill/sandbox/agentbay/*`）。demo 未装该 SDK，故所有操作抛
SandboxUnavailableError——用于演示 provider 抽象与可切换性（mirror 存储端口的 AgentBay TODO）。
"""
from __future__ import annotations

from pathlib import Path

from agent.claude_skill.sandbox.base import (
    BaseSandbox,
    CodeService,
    EnumSandboxProvider,
    FileService,
    SandboxUnavailableError,
    ShellService,
)

_MSG = "AgentBay 云沙箱未启用（demo 未装 wuying-agentbay-sdk）；请用 SANDBOX_PROVIDER=local"


class AgentbaySandbox(BaseSandbox):
    def __init__(self) -> None:
        self._session_id = "agentbay-stub"

    async def try_create(self) -> None:
        raise SandboxUnavailableError(_MSG)

    async def stage_directory(self, source: Path, destination: str) -> None:
        raise SandboxUnavailableError(_MSG)

    def get_session_id(self) -> str:
        return self._session_id

    def get_sandbox_provider(self) -> EnumSandboxProvider:
        return EnumSandboxProvider.AGENT_BAY

    def file_service(self) -> FileService:
        raise SandboxUnavailableError(_MSG)

    def shell_service(self) -> ShellService:
        raise SandboxUnavailableError(_MSG)

    def code_service(self) -> CodeService:
        raise SandboxUnavailableError(_MSG)
