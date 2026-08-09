"""通用可观测性：结构化 JSON 日志 + trace_id 全链路贯穿 + FastAPI 中间件。

替代原项目的 SLS / langfuse 内部基建，但保留生产级习惯：
- 每条日志一行 JSON，带 trace_id，便于检索与关联；
- 业务日志统一 `[Tag]` 前缀 + 结构化字段（对齐 albert-agent-2 的 SLS 监控习惯）。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any, Awaitable, Callable, Iterator

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# trace_id 经 contextvar 贯穿单次请求内的所有日志（含异步任务）
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


@contextlib.contextmanager
def use_trace_id(trace_id: str) -> Iterator[str]:
    """在一段作用域内绑定 trace_id，退出时**按 token 还原**。

    给没有 HTTP 中间件的常驻进程用（Worker 从持久化的 Run 上恢复 trace_id）。
    与裸 `set_trace_id` 的关键差别是必须还原：Worker 的每个 claim 通常跑在自己的
    `asyncio.create_task` 里（context 是副本，互不影响），但 `run_once()` 这类
    确定性入口是**直接 await** 的——那里裸 set 会把 trace_id 泄漏给调用方，
    并在连续多次调用间互相串味。
    """
    token = _trace_id_var.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id_var.reset(token)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


class _JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON，自动附加 trace_id 与结构化字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志器为结构化 JSON 输出（幂等）。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_kv(logger: logging.Logger, level: int, tag: str, msg: str, **fields: Any) -> None:
    """带 `[Tag]` 前缀 + 结构化字段的日志助手。

    用法：``log_kv(logger, logging.INFO, "QaRetrieve", "hit", doc_count=5, cost_ms=120)``
    """
    logger.log(level, f"[{tag}] {msg}", extra={"fields": fields})


class TraceMiddleware(BaseHTTPMiddleware):
    """为每个请求注入/透传 trace_id，并记录入口/出口与耗时。"""

    def __init__(self, app: Any, service: str) -> None:
        super().__init__(app)
        self._service = service
        self._logger = get_logger(f"{service}.access")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        set_trace_id(trace_id)
        start = time.perf_counter()
        log_kv(self._logger, logging.INFO, "Access", "request in",
               method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            cost_ms = int((time.perf_counter() - start) * 1000)
            log_kv(self._logger, logging.ERROR, "Access", "request error",
                   path=request.url.path, cost_ms=cost_ms)
            raise
        cost_ms = int((time.perf_counter() - start) * 1000)
        response.headers["x-trace-id"] = trace_id
        log_kv(self._logger, logging.INFO, "Access", "request out",
               path=request.url.path, status=response.status_code, cost_ms=cost_ms)
        return response
