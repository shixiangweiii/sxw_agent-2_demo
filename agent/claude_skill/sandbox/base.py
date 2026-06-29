"""沙箱 provider 抽象 + 核心服务接口（file/shell/code）。

精简复刻 albert-agent-2 `app/core/claude_skill/sandbox/base_sandbox.py`：
保留 EnumSandboxScene 三核心服务，裁剪 oss/computer/browser/artifact/context。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class EnumSandboxProvider(str, Enum):
    LOCAL = "local"
    AGENT_BAY = "agentbay"


class EnumSandboxScene(str, Enum):
    CODE = "code"
    FILE = "file"
    SHELL = "shell"


class SandboxError(Exception):
    """沙箱通用异常。"""


class SandboxUnavailableError(SandboxError):
    """沙箱不可用（如 AgentBay SDK 未装）。"""


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class CodeResult:
    ok: bool
    stdout: str
    stderr: str


class FileService(ABC):
    @abstractmethod
    async def read(self, path: str) -> str: ...

    @abstractmethod
    async def write(self, path: str, content: str) -> None: ...

    @abstractmethod
    async def list_dir(self, path: str = ".") -> list[str]: ...


class ShellService(ABC):
    @abstractmethod
    async def run(self, command: str, timeout: float = 30.0) -> ShellResult: ...


class CodeService(ABC):
    @abstractmethod
    async def run_python(self, code: str, timeout: float = 30.0) -> CodeResult: ...


class BaseSandbox(ABC):
    """沙箱基类：一个会话提供 file/shell/code 服务。"""

    @abstractmethod
    async def try_create(self) -> None: ...

    @abstractmethod
    def get_session_id(self) -> str: ...

    @abstractmethod
    def get_sandbox_provider(self) -> EnumSandboxProvider: ...

    @abstractmethod
    def file_service(self) -> FileService: ...

    @abstractmethod
    def shell_service(self) -> ShellService: ...

    @abstractmethod
    def code_service(self) -> CodeService: ...

    async def close(self) -> None:
        """释放资源。默认无操作。"""
