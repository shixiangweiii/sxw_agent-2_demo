"""沙箱工具集：把沙箱 file/shell/code 能力包装成 ADK FunctionTool（绑定到一个 sandbox 实例）。

复刻 `app/core/claude_skill/toolset/skill_remote_sandbox_toolset.py`（精简）。
"""
from __future__ import annotations

from typing import Any, Callable

from agent.claude_skill.sandbox.base import BaseSandbox

_OUT_LIMIT = 4000
_ERR_LIMIT = 2000


def build_sandbox_tools(sandbox: BaseSandbox) -> list[Callable[..., Any]]:
    async def read_file(path: str) -> dict[str, Any]:
        """读取沙箱工作目录中的文件内容。

        Args:
            path: 相对工作目录的文件路径。
        """
        try:
            return {"path": path, "content": await sandbox.file_service().read(path)}
        except Exception as exc:  # noqa: BLE001 - 工具错误结构化返回
            return {"path": path, "error": str(exc)}

    async def write_file(path: str, content: str) -> dict[str, Any]:
        """把内容写入沙箱工作目录中的文件。

        Args:
            path: 相对工作目录的文件路径。
            content: 要写入的文本内容。
        """
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
        try:
            return {"path": path, "entries": await sandbox.file_service().list_dir(path)}
        except Exception as exc:  # noqa: BLE001
            return {"path": path, "error": str(exc)}

    async def run_shell(command: str) -> dict[str, Any]:
        """在沙箱中执行一条 shell 命令并返回输出。

        Args:
            command: 要执行的 shell 命令。
        """
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
        try:
            r = await sandbox.code_service().run_python(code)
            return {"ok": r.ok, "stdout": r.stdout[:_OUT_LIMIT], "stderr": r.stderr[:_ERR_LIMIT]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    return [read_file, write_file, list_files, run_shell, run_python]
