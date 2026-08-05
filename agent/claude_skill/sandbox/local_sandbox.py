"""LocalSandbox：本地可跑沙箱（临时工作目录 + 子进程跑 shell/python）。

⚠️ demo 用途，非生产级隔离：仅做 工作目录限制 + 超时；生产应换 AgentBay 等真隔离沙箱。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
import stat
import sys
import tempfile
import uuid
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import Callable, Optional, TypeVar

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

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


async def _to_thread_cancel_safe(
    label: str,
    func: Callable[..., _T],
    *args: object,
) -> _T:
    """取消时等待不可中断的本地 I/O 收口，且不让清理异常覆盖取消。"""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception as exc:  # noqa: BLE001 - 保留原始取消语义
            logger.warning("%s cleanup failed after cancellation: %s", label, type(exc).__name__)
        raise


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
    create_task = asyncio.create_task(asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), stdout=PIPE, stderr=PIPE, start_new_session=True,
    ))
    try:
        proc = await asyncio.shield(create_task)
    except asyncio.CancelledError:
        try:
            proc = await asyncio.shield(create_task)
            terminate_task = asyncio.create_task(_terminate_process_group(proc))
            await asyncio.shield(terminate_task)
        except Exception as exc:  # noqa: BLE001 - 保留原始取消语义
            logger.warning("process cleanup failed after cancellation: %s", type(exc).__name__)
        raise
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await _terminate_process_group(proc)
        except Exception as exc:  # noqa: BLE001 - timeout 是对外主错误
            logger.warning("process cleanup failed after timeout: %s", type(exc).__name__)
        raise SandboxError(f"sandbox command timeout after {timeout}s")
    except asyncio.CancelledError:
        try:
            terminate_task = asyncio.create_task(_terminate_process_group(proc))
            await asyncio.shield(terminate_task)
        except Exception as exc:  # noqa: BLE001 - 保留原始取消语义
            logger.warning("process cleanup failed after cancellation: %s", type(exc).__name__)
        raise
    return proc.returncode or 0, out.decode("utf-8", "ignore"), err.decode("utf-8", "ignore")


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """终止 bash 及其派生进程；TERM 无效时升级为 KILL。"""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    # shell 主进程退出不代表同组脚本都退出；仍探测并清理整个进程组。
    try:
        os.killpg(proc.pid, 0)
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if proc.returncode is None:
        await proc.wait()


_STAGE_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    ".DS_Store",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


def _stage_directory_sync(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise SandboxError(f"skill package source is a symlink: {source}")
    source = source.resolve()
    if not source.is_dir():
        raise SandboxError(f"skill package source is not a regular directory: {source}")
    if target.exists():
        raise SandboxError(f"sandbox destination already exists: {target}")
    target.mkdir(parents=True)
    try:
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            if any(part in _STAGE_IGNORES for part in relative.parts):
                continue
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SandboxError(f"skill package contains symlink: {relative}")
            destination = target / relative
            if stat.S_ISDIR(mode):
                destination.mkdir(parents=True, exist_ok=True)
            elif stat.S_ISREG(mode):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
            else:
                raise SandboxError(f"skill package contains special file: {relative}")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _remove_tree_sync(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class LocalFileService(FileService):
    def __init__(self, workdir: Path) -> None:
        self._wd = workdir

    async def read(self, path: str) -> str:
        return await _to_thread_cancel_safe("read_file", self._read_sync, path)

    def _read_sync(self, path: str) -> str:
        target = _safe_path(self._wd, path)
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode):
            raise SandboxError(f"sandbox read target is not a regular file: {path}")
        return target.read_text(encoding="utf-8")

    async def write(self, path: str, content: str) -> None:
        await _to_thread_cancel_safe("write_file", self._write_sync, path, content)

    def _write_sync(self, path: str, content: str) -> None:
        p = _safe_path(self._wd, path)
        if p.exists() and not stat.S_ISREG(p.stat().st_mode):
            raise SandboxError(f"sandbox write target is not a regular file: {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    async def list_dir(self, path: str = ".") -> list[str]:
        return await _to_thread_cancel_safe("list_files", self._list_dir_sync, path)

    def _list_dir_sync(self, path: str) -> list[str]:
        target = _safe_path(self._wd, path)
        if not stat.S_ISDIR(target.stat().st_mode):
            raise SandboxError(f"sandbox list target is not a directory: {path}")
        return sorted(os.listdir(target))


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

    async def stage_directory(self, source: Path, destination: str) -> None:
        target = _safe_path(self._require_wd(), destination)
        await _to_thread_cancel_safe(
            "stage_directory",
            _stage_directory_sync,
            source,
            target,
        )

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
        workdir = self._workdir
        if workdir and workdir.exists():
            try:
                await _to_thread_cancel_safe(
                    "sandbox_close",
                    _remove_tree_sync,
                    workdir,
                )
            finally:
                self._workdir = None
        else:
            self._workdir = None
