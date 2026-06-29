"""LocalSandbox：本地可跑沙箱（临时工作目录 + 子进程跑 shell/python）。

⚠️ demo 用途，非生产级隔离：仅做 工作目录限制 + 超时；生产应换 AgentBay 等真隔离沙箱。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import Optional

from agent.claude_skill.sandbox.base import (
    BaseSandbox,
    CodeResult,
    CodeService,
    EnumSandboxProvider,
    FileService,
    SandboxError,
    ShellResult,
    ShellService,
)


def _safe_path(workdir: Path, path: str) -> Path:
    # 用 relative_to 做父子路径校验，而非字符串 startswith：
    # 后者会被同前缀 sibling 目录（如 workdir 同级的 `<workdir>_evil`）绕过。
    root = workdir.resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SandboxError(f"path escapes sandbox workdir: {path}") from exc
    return resolved


async def _run(args: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, cwd=str(cwd), stdout=PIPE, stderr=PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SandboxError(f"sandbox command timeout after {timeout}s")
    return proc.returncode or 0, out.decode("utf-8", "ignore"), err.decode("utf-8", "ignore")


class LocalFileService(FileService):
    def __init__(self, workdir: Path) -> None:
        self._wd = workdir

    async def read(self, path: str) -> str:
        return _safe_path(self._wd, path).read_text(encoding="utf-8")

    async def write(self, path: str, content: str) -> None:
        p = _safe_path(self._wd, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    async def list_dir(self, path: str = ".") -> list[str]:
        return sorted(os.listdir(_safe_path(self._wd, path)))


class LocalShellService(ShellService):
    def __init__(self, workdir: Path) -> None:
        self._wd = workdir

    async def run(self, command: str, timeout: float = 30.0) -> ShellResult:
        rc, out, err = await _run(["/bin/bash", "-lc", command], self._wd, timeout)
        return ShellResult(exit_code=rc, stdout=out, stderr=err)


class LocalCodeService(CodeService):
    def __init__(self, workdir: Path) -> None:
        self._wd = workdir

    async def run_python(self, code: str, timeout: float = 30.0) -> CodeResult:
        rc, out, err = await _run([sys.executable, "-c", code], self._wd, timeout)
        return CodeResult(ok=(rc == 0), stdout=out, stderr=err)


class LocalSandbox(BaseSandbox):
    def __init__(self) -> None:
        self._session_id = "local-" + uuid.uuid4().hex[:10]
        self._workdir: Optional[Path] = None

    async def try_create(self) -> None:
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="sxw_sandbox_"))

    def _require_wd(self) -> Path:
        if self._workdir is None:
            raise SandboxError("sandbox not created; call try_create() first")
        return self._workdir

    def get_session_id(self) -> str:
        return self._session_id

    def get_sandbox_provider(self) -> EnumSandboxProvider:
        return EnumSandboxProvider.LOCAL

    def file_service(self) -> FileService:
        return LocalFileService(self._require_wd())

    def shell_service(self) -> ShellService:
        return LocalShellService(self._require_wd())

    def code_service(self) -> CodeService:
        return LocalCodeService(self._require_wd())

    async def close(self) -> None:
        if self._workdir and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
