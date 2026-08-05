"""沙箱工具集：把沙箱 file/shell/code 能力包装成 ADK FunctionTool（绑定到一个 sandbox 实例）。

复刻 `app/core/claude_skill/toolset/skill_remote_sandbox_toolset.py`（精简）。
"""
from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.claude_skill.sandbox.base import BaseSandbox

_OUT_LIMIT = 4000
_ERR_LIMIT = 2000


def _normalize_sandbox_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return normalized.removeprefix("./")


@dataclass
class SkillToolsetState:
    """一次子会话的工具使用状态，用于强制 instruction-first 契约。"""

    required_instruction_path: str
    successful_reads: set[str] = field(default_factory=set)
    instruction_first_violation: bool = False
    instruction_ready: bool = False
    _activation_scheduled: bool = False

    @property
    def instruction_read(self) -> bool:
        return self.required_instruction_path in self.successful_reads

    @property
    def instruction_compliant(self) -> bool:
        return self.instruction_read and not self.instruction_first_violation

    def record_successful_read(self, path: str) -> None:
        self.successful_reads.add(path)
        if path != self.required_instruction_path or self._activation_scheduled:
            return
        self._activation_scheduled = True
        # ADK 会把同一轮多个 function call 先全部 create_task 再 gather。
        # call_soon 让已排队的 sibling tool task 先经过 guard，避免它们因
        # read_file 恰好先完成而被误放行；下一 LLM 轮开始前 instruction_ready 生效。
        asyncio.get_running_loop().call_soon(self._activate_instruction)

    def _activate_instruction(self) -> None:
        self.instruction_ready = True

    def guard_instruction_first(self, tool_name: str) -> dict[str, Any] | None:
        if self.instruction_ready:
            return None
        self.instruction_first_violation = True
        return {
            "error": "SKILL_INSTRUCTION_NOT_READ",
            "message": (
                f"必须首先调用 read_file({self.required_instruction_path!r})，"
                f"当前不可调用 {tool_name}"
            ),
        }


def build_sandbox_tools(
    sandbox: BaseSandbox,
    state: SkillToolsetState,
) -> list[Callable[..., Any]]:
    async def read_file(path: str) -> dict[str, Any]:
        """读取沙箱工作目录中的文件内容。

        Args:
            path: 相对工作目录的文件路径。
        """
        normalized = _normalize_sandbox_path(path)
        if not state.instruction_ready and normalized != state.required_instruction_path:
            return state.guard_instruction_first("read_file") or {}
        try:
            content = await sandbox.file_service().read(normalized)
            state.record_successful_read(normalized)
            return {"path": normalized, "content": content}
        except Exception as exc:  # noqa: BLE001 - 工具错误结构化返回
            return {"path": path, "error": str(exc)}

    async def write_file(path: str, content: str) -> dict[str, Any]:
        """把内容写入沙箱工作目录中的文件。

        Args:
            path: 相对工作目录的文件路径。
            content: 要写入的文本内容。
        """
        if guard := state.guard_instruction_first("write_file"):
            return guard
        try:
            await sandbox.file_service().write(path, content)
            return {"path": path, "ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"path": path, "error": str(exc)}

    async def list_files(path: str = ".") -> dict[str, Any]:
        """列出沙箱工作目录下的文件。

        Args:
            path: 相对工作目录的路径，默认当前目录。
        """
        if guard := state.guard_instruction_first("list_files"):
            return guard
        try:
            return {"path": path, "entries": await sandbox.file_service().list_dir(path)}
        except Exception as exc:  # noqa: BLE001
            return {"path": path, "error": str(exc)}

    async def run_shell(command: str) -> dict[str, Any]:
        """在沙箱中执行一条 shell 命令并返回输出。

        Args:
            command: 要执行的 shell 命令。
        """
        if guard := state.guard_instruction_first("run_shell"):
            return guard
        try:
            r = await sandbox.shell_service().run(command)
            return {"exitCode": r.exit_code, "stdout": r.stdout[:_OUT_LIMIT], "stderr": r.stderr[:_ERR_LIMIT]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def run_python(code: str) -> dict[str, Any]:
        """在沙箱中执行 Python 代码（环境含 numpy/pandas）。务必用 print 输出结果。

        Args:
            code: 要执行的 Python 源码。
        """
        if guard := state.guard_instruction_first("run_python"):
            return guard
        try:
            r = await sandbox.code_service().run_python(code)
            return {"ok": r.ok, "stdout": r.stdout[:_OUT_LIMIT], "stderr": r.stderr[:_ERR_LIMIT]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    return [read_file, write_file, list_files, run_shell, run_python]
